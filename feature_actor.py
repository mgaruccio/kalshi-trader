"""Climate feature accumulation and danger monitoring.

FeatureActor sits between raw climate data and the trading strategy.
It accumulates ClimateEvent data per-city and continuously checks exit rules
(emitting DangerAlert).

ML scoring (ModelSignal emission) is handled by the standalone evaluator.py
which reads the DB and publishes to Redis.

In live_mode, timer-based pollers fetch real-time data from
kalshi_weather_ml and publish ClimateEvents on the same bus path
used by backtest replay.
"""
import logging
import os
import sys
from datetime import timedelta

# Add kalshi-weather package to path
_KW_ROOT = os.environ.get("KALSHI_WEATHER_ROOT", "/home/mike/code/altmarkets/kalshi-weather")
sys.path.insert(0, f"{_KW_ROOT}/src")

from nautilus_trader.common.actor import Actor
from nautilus_trader.common.config import ActorConfig
from nautilus_trader.model.data import DataType
from data_types import ClimateEvent, DangerAlert
from exit_rules import CityFeatureState, check_exit_rules, should_exit
from shared_features import build_extra_features

log = logging.getLogger(__name__)

# Optional live dependencies -- may not be installed in backtest-only environments
try:
    from kalshi_weather_ml.observations import get_current_temp
except ImportError:
    get_current_temp = None

try:
    from kalshi_weather_ml.forecasts import (
        get_forecast,
        get_weather_features,
        PRIMARY_MODEL,
        CONSENSUS_MODEL,
    )
except ImportError:
    get_forecast = None
    get_weather_features = None
    PRIMARY_MODEL = "ecmwf_ifs025"
    CONSENSUS_MODEL = "gfs_seamless"

try:
    from kalshi_weather_ml.data_sources.sst import COASTAL_CITIES, get_current_sst
except ImportError:
    COASTAL_CITIES: set = set()
    get_current_sst = None

try:
    from kalshi_weather_ml.markets import parse_ticker
except ImportError:
    parse_ticker = None



class FeatureActorConfig(ActorConfig):
    danger_check_enabled: bool = True  # check exit rules on each event
    live_mode: bool = False            # enables live pollers in Phase 3


class FeatureActor(Actor):
    """Bridges climate data sources to trading strategy signals.

    Responsibilities:
    - Accumulate ClimateEvent data per (city, date) bucket
    - Check exit rules and publish DangerAlert on each event
    - Poll live data (METAR, forecasts, SST) when live_mode=True

    NOT responsible for:
    - ML model loading
    - ModelSignal emission (handled by evaluator.py via Redis)
    """

    def __init__(self, config: FeatureActorConfig):
        super().__init__(config)
        self._cfg = config
        self._city_features: dict[tuple[str, str], CityFeatureState] = {}
        self._positions: dict[str, dict] = {}  # ticker -> {side, threshold, city}
        self._events_received: int = 0
        self._instrument_provider = None  # set via set_instrument_provider()
        self._known_instruments: set[str] = set()  # instrument_id values we've seen

    def set_instrument_provider(self, provider):
        """Wire the instrument provider for periodic reload of new markets."""
        self._instrument_provider = provider

    def on_start(self):
        """Subscribe to climate events and optionally start live pollers."""
        # Subscribe via msgbus directly — subscribe_data() requires client_id
        # which doesn't exist for internal pub/sub in live TradingNode.
        # publish_data() publishes to "data.{DataType.topic}", so we match that.
        self.msgbus.subscribe(
            topic=f"data.{DataType(ClimateEvent).topic}",
            handler=self.handle_data,
        )

        # Seed known instruments so we can detect new ones on reload
        for inst in self.cache.instruments():
            self._known_instruments.add(inst.id.value)

        if self._cfg.live_mode:
            self._start_live_pollers()

        if self._instrument_provider is not None:
            self.clock.set_timer(
                "reload_instruments",
                interval=timedelta(minutes=30),
                callback=lambda _: self._reload_instruments(),
            )
            self.log.info("Instrument reload timer started (30m)")

        self.log.info(
            f"FeatureActor started (live={self._cfg.live_mode})"
        )

    def _active_dates_for_city(self, city: str) -> list[str]:
        """Return settlement dates with instruments in cache or positions for a city."""
        dates: set[str] = set()
        # From positions
        for ticker, info in self._positions.items():
            if info.get("city") != city:
                continue
            if parse_ticker:
                parsed = parse_ticker(ticker)
                if parsed:
                    dates.add(parsed["settlement_date"])
        # From cached instruments
        if parse_ticker:
            from kalshi_weather_ml.markets import SERIES_CONFIG
            series_to_city = {s: c for s, c in SERIES_CONFIG}
            for inst in self.cache.instruments():
                sym = inst.id.symbol.value
                if not (sym.endswith("-YES") or sym.endswith("-NO")):
                    continue
                ticker = sym.rsplit("-", 1)[0]
                parsed = parse_ticker(ticker)
                if parsed and series_to_city.get(parsed["series"]) == city:
                    dates.add(parsed["settlement_date"])
        return sorted(dates)

    def on_data(self, data):
        """Handle incoming ClimateEvent -- accumulate features, check danger.

        Date-specific events (date != "") route to exact (city, date) bucket.
        City-level events (date == "") fan out to all active (city, date) buckets.
        """
        if isinstance(data, ClimateEvent):
            self._events_received += 1

            if data.date:
                # Date-specific (forecast): exact (city, date) bucket
                key = (data.city, data.date)
                if key not in self._city_features:
                    # Seed from city-level fallback if it exists
                    fallback = self._city_features.get((data.city, ""))
                    state = CityFeatureState()
                    if fallback:
                        state.update("_fallback", fallback.snapshot())
                    self._city_features[key] = state
                self._city_features[key].update(data.source, data.features)
            else:
                # City-level (AFD, METAR, SST): fan out to all active dates
                active_dates = self._active_dates_for_city(data.city)
                for dt in active_dates:
                    key = (data.city, dt)
                    state = self._city_features.setdefault(key, CityFeatureState())
                    state.update(data.source, data.features)
                if not active_dates:
                    # No active dates yet -- store in fallback bucket
                    key = (data.city, "")
                    state = self._city_features.setdefault(key, CityFeatureState())
                    state.update(data.source, data.features)

            if self._cfg.danger_check_enabled and self._positions:
                self._check_danger(data.city)

    def _reload_instruments(self):
        """Reload instruments from Kalshi API and add new ones to cache.

        Called periodically so newly-opened markets (e.g. tomorrow's
        contracts that open at ~10am ET) are discovered without restart.
        """
        if self._instrument_provider is None:
            return

        try:
            self._instrument_provider.load_all(filters={"series_ticker": "KXHIGH"})
            new_count = 0
            for inst in self._instrument_provider.list_all():
                inst_key = inst.id.value
                if inst_key not in self._known_instruments:
                    self._known_instruments.add(inst_key)
                    # Add to cache if not already present
                    if self.cache.instrument(inst.id) is None:
                        self.cache.add_instrument(inst)
                        new_count += 1
            if new_count:
                self.log.info(f"Instrument reload: {new_count} new instruments added to cache")
        except Exception as e:
            self.log.warning(f"Instrument reload failed: {e}")

    def _check_danger(self, city: str):
        """Run exit rules for a city and publish DangerAlert if triggered."""
        # Find all (city, date) keys for this city
        relevant_keys = [(c, d) for (c, d) in self._city_features if c == city and d != ""]
        if not relevant_keys:
            # Try fallback
            if (city, "") in self._city_features:
                relevant_keys = [(city, "")]

        ts = self.clock.timestamp_ns()

        for key in relevant_keys:
            state = self._city_features[key]
            features = state.snapshot()
            _, sd = key

            # Filter positions to this settlement date
            filtered_positions: dict[str, dict] = {}
            for ticker, info in self._positions.items():
                if info.get("city") != city:
                    continue
                if sd == "":
                    filtered_positions[ticker] = info  # fallback: all tickers
                elif parse_ticker:
                    parsed = parse_ticker(ticker)
                    if parsed and parsed["settlement_date"] == sd:
                        filtered_positions[ticker] = info

            if not filtered_positions:
                continue

            alerts = check_exit_rules(city, features, filtered_positions)
            if not alerts:
                continue

            for alert_dict in alerts:
                # Determine composite alert level
                if should_exit(alerts):
                    level = "CRITICAL"
                elif alert_dict["alert_level"] == "CRITICAL":
                    level = "CRITICAL"
                else:
                    level = alert_dict["alert_level"]

                for ticker in alert_dict.get("tickers", []):
                    danger = DangerAlert(
                        ticker=ticker,
                        city=city,
                        alert_level=level,
                        rule_name=alert_dict["rule_name"],
                        reason=alert_dict["reason"],
                        features=alert_dict.get("features", {}),
                        ts_event=ts,
                        ts_init=ts,
                    )
                    self.publish_data(DataType(DangerAlert), danger)

    def update_positions(self, positions: dict[str, dict]):
        """Called by strategy when positions change.

        Args:
            positions: ticker -> {"side": "no", "threshold": 55.0, "city": "chicago"}
        """
        self._positions = positions

    # --- Live pollers (only active when live_mode=True) ---

    def _start_live_pollers(self):
        """Start timer-based pollers for each data source."""
        # METAR: every 10 min (critical for exit triggers)
        self.clock.set_timer(
            "poll_metar",
            interval=timedelta(minutes=10),
            callback=self._poll_metar,
        )
        # ECMWF/GFS forecasts: every 1 hour
        self.clock.set_timer(
            "poll_forecast",
            interval=timedelta(hours=1),
            callback=self._poll_forecasts,
        )
        # SST: every 1 hour
        self.clock.set_timer(
            "poll_sst",
            interval=timedelta(hours=1),
            callback=self._poll_sst,
        )
        self.log.info("Live pollers started: METAR(10m), forecast(1h), SST(1h)")

    def _poll_metar(self, event):
        """Poll real-time METAR observations for all cities with positions."""
        if not self._positions:
            return
        if get_current_temp is None:
            return

        cities = {info["city"] for info in self._positions.values()}
        ts = self.clock.timestamp_ns()

        for city in cities:
            try:
                temp = get_current_temp(city)
                if temp is not None:
                    evt = ClimateEvent(
                        source="metar_obs",
                        city=city,
                        features={"obs_temp": temp},
                        ts_event=ts,
                        ts_init=ts,
                    )
                    self.publish_data(DataType(ClimateEvent), evt)
                    # Also ingest locally (same path as backtest)
                    self.on_data(evt)
            except Exception as e:
                self.log.warning(f"METAR poll failed for {city}: {e}")

    def _poll_forecasts(self, event):
        """Poll ECMWF/GFS forecasts for cities with positions.

        Fetches per (city, settlement_date) so each date gets its own forecast.
        """
        if not self._positions:
            return
        if get_forecast is None:
            return

        # Build (city, settlement_date) pairs from positions
        city_dates: set[tuple[str, str]] = set()
        for ticker, info in self._positions.items():
            city = info.get("city", "")
            if parse_ticker and city:
                parsed = parse_ticker(ticker)
                if parsed:
                    city_dates.add((city, parsed["settlement_date"]))

        ts = self.clock.timestamp_ns()

        for city, sd in city_dates:
            try:
                ecmwf = get_forecast(city, sd, PRIMARY_MODEL)
                gfs = get_forecast(city, sd, CONSENSUS_MODEL)
                icon = get_forecast(city, sd, "icon_seamless")
                wx = get_weather_features(city, sd) if get_weather_features else {}

                features = build_extra_features(ecmwf, gfs, icon, city, wx)

                if features:
                    evt = ClimateEvent(
                        source="ecmwf_forecast",
                        city=city,
                        features=features,
                        ts_event=ts,
                        ts_init=ts,
                        date=sd,
                    )
                    self.publish_data(DataType(ClimateEvent), evt)
                    self.on_data(evt)
            except Exception as e:
                self.log.warning(f"Forecast poll failed for {city}/{sd}: {e}")

    def _poll_sst(self, event):
        """Poll SST data for coastal cities with positions."""
        if not self._positions:
            return
        if get_current_sst is None:
            return

        cities = {info["city"] for info in self._positions.values()}
        ts = self.clock.timestamp_ns()

        for city in cities:
            try:
                if city not in COASTAL_CITIES:
                    continue

                sst_data = get_current_sst(city)
                if sst_data:
                    evt = ClimateEvent(
                        source="ndbc_buoy_sst",
                        city=city,
                        features=sst_data,
                        ts_event=ts,
                        ts_init=ts,
                    )
                    self.publish_data(DataType(ClimateEvent), evt)
                    self.on_data(evt)
            except Exception as e:
                self.log.warning(f"SST poll failed for {city}: {e}")

    @property
    def poll_cities(self) -> set[str]:
        """Return set of cities currently being polled (from positions)."""
        if self._positions:
            return {info["city"] for info in self._positions.values()}
        return set()

    @property
    def events_received(self) -> int:
        return self._events_received

    @property
    def city_features(self) -> dict[tuple[str, str], CityFeatureState]:
        return self._city_features
