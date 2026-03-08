"""Climate feature accumulation and signal generation.

FeatureActor sits between raw climate data and the trading strategy.
It accumulates ClimateEvent data per-city, periodically runs the ML
ensemble (emitting ModelSignal), and continuously checks exit rules
(emitting DangerAlert).

In live_mode, timer-based pollers fetch real-time data from
kalshi_weather_ml and publish ClimateEvents on the same bus path
used by backtest replay.
"""
import logging
from datetime import timedelta

from nautilus_trader.common.actor import Actor
from nautilus_trader.common.config import ActorConfig
from nautilus_trader.model.data import DataType
from nautilus_trader.model.identifiers import ClientId

from data_types import ClimateEvent, ModelSignal, DangerAlert
from exit_rules import CityFeatureState, check_exit_rules, should_exit

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


class FeatureActorConfig(ActorConfig):
    model_cycle_seconds: int = 300    # 5 min between model runs
    danger_check_enabled: bool = True  # check exit rules on each event
    live_mode: bool = False            # enables live pollers in Phase 3


class FeatureActor(Actor):
    """Bridges climate data sources to trading strategy signals."""

    def __init__(self, config: FeatureActorConfig):
        super().__init__(config)
        self._cfg = config
        self._city_features: dict[str, CityFeatureState] = {}
        self._positions: dict[str, dict] = {}  # ticker -> {side, threshold, city}
        self._events_received: int = 0

    def on_start(self):
        """Subscribe to climate events and start model cycle timer."""
        self.subscribe_data(DataType(ClimateEvent))
        self.clock.set_timer(
            "model_cycle",
            interval=timedelta(seconds=self._cfg.model_cycle_seconds),
            callback=self._on_model_timer,
        )

        if self._cfg.live_mode:
            self._start_live_pollers()

        self.log.info(
            f"FeatureActor started (cycle={self._cfg.model_cycle_seconds}s, "
            f"live={self._cfg.live_mode})"
        )

    def on_data(self, data):
        """Handle incoming ClimateEvent -- accumulate features, check danger."""
        if isinstance(data, ClimateEvent):
            self._events_received += 1
            state = self._city_features.setdefault(data.city, CityFeatureState())
            state.update(data.source, data.features)

            if self._cfg.danger_check_enabled and self._positions:
                self._check_danger(data.city)

    def _on_model_timer(self, event):
        """Run ML ensemble on accumulated features for each city with positions.

        In the full implementation, this will:
        1. For each city with active positions, snapshot features
        2. Run the ensemble model (EMOS + NGBoost + DRN)
        3. Publish ModelSignal for each evaluated opportunity

        For now, publishes a ModelSignal with p_win from a placeholder
        so the strategy pipeline can be tested end-to-end.
        """
        if not self._positions:
            return

        # Group positions by city
        cities_with_positions: dict[str, list[str]] = {}
        for ticker, info in self._positions.items():
            city = info.get("city", "")
            cities_with_positions.setdefault(city, []).append(ticker)

        ts = self.clock.timestamp_ns()

        for city, tickers in cities_with_positions.items():
            state = self._city_features.get(city)
            if state is None:
                continue

            features = state.snapshot()
            if not features:
                continue

            for ticker in tickers:
                pos_info = self._positions[ticker]
                # Placeholder: real implementation imports and runs
                # kalshi_weather_ml models here
                signal = ModelSignal(
                    city=city,
                    ticker=ticker,
                    side=pos_info.get("side", "no"),
                    p_win=0.0,  # placeholder -- real model inference in Phase 3
                    model_scores={},
                    features_snapshot=features,
                    ts_event=ts,
                    ts_init=ts,
                )
                self.publish_data(DataType(ModelSignal), signal)

    def _check_danger(self, city: str):
        """Run exit rules for a city and publish DangerAlert if triggered."""
        state = self._city_features.get(city)
        if state is None:
            return

        features = state.snapshot()
        alerts = check_exit_rules(city, features, self._positions)

        if not alerts:
            return

        ts = self.clock.timestamp_ns()

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
        """Poll ECMWF/GFS forecasts for cities with positions."""
        if not self._positions:
            return
        if get_forecast is None:
            return

        from datetime import date

        cities = {info["city"] for info in self._positions.values()}
        today = date.today().isoformat()
        ts = self.clock.timestamp_ns()

        for city in cities:
            try:
                ecmwf = get_forecast(city, today, PRIMARY_MODEL)
                gfs = get_forecast(city, today, CONSENSUS_MODEL)
                wx = get_weather_features(city, today) if get_weather_features else {}

                features = {}
                if ecmwf is not None:
                    features["ecmwf_high"] = ecmwf
                if gfs is not None:
                    features["gfs_high"] = gfs
                if ecmwf is not None and gfs is not None:
                    features["forecast_high"] = max(ecmwf, gfs)
                features.update(wx)

                if features:
                    evt = ClimateEvent(
                        source="ecmwf_forecast",
                        city=city,
                        features=features,
                        ts_event=ts,
                        ts_init=ts,
                    )
                    self.publish_data(DataType(ClimateEvent), evt)
                    self.on_data(evt)
            except Exception as e:
                self.log.warning(f"Forecast poll failed for {city}: {e}")

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
    def city_features(self) -> dict[str, CityFeatureState]:
        return self._city_features
