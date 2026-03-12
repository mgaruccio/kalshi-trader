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
import os
import sys
from datetime import datetime, timedelta
from pathlib import Path

# Add kalshi-weather package to path
_KW_ROOT = os.environ.get("KALSHI_WEATHER_ROOT", "/home/mike/code/altmarkets/kalshi-weather")
sys.path.insert(0, f"{_KW_ROOT}/src")

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

try:
    from kalshi_weather_ml.strategy import score_opportunities, score_and_filter
    from kalshi_weather_ml.markets import parse_ticker
    from kalshi_weather_ml.config import load_config
except ImportError:
    score_opportunities = None
    score_and_filter = None
    parse_ticker = None
    load_config = None


_COAST_NORMAL = {
    "los_angeles": 240, "san_francisco": 270, "miami": 90,
    "new_york": 150, "boston": 90, "seattle": 270,
    "houston": 150, "new_orleans": 180, "philadelphia": 135,
    "washington_dc": 135,
}


def _build_extra_features(
    ecmwf: float | None,
    gfs: float | None,
    icon: float | None,
    city: str,
    date: str,
    wx: dict | None,
) -> dict:
    """Compute all features needed for spread models: forecasts + model spread + wind_dir_offshore.

    Mirrors quantdesk_adapter._compute_extra_features so the NT path
    produces the same feature set as the standalone trader.
    """
    import numpy as np

    features: dict[str, float] = {}

    if ecmwf is not None:
        features["ecmwf_high"] = ecmwf
    if gfs is not None:
        features["gfs_high"] = gfs
    if ecmwf is not None and gfs is not None:
        features["forecast_high"] = max(ecmwf, gfs)

    # Model spread (3-model when ICON available, 2-model fallback)
    temps = [t for t in (ecmwf, gfs, icon) if t is not None]
    if len(temps) >= 2:
        arr = np.array(temps)
        features["model_std"] = float(np.std(arr, ddof=0))
        features["model_range"] = float(np.ptp(arr))

    # Weather features from ECMWF forecast
    if wx:
        features.update(wx)

    # Derive wind_dir_offshore from wind_dir_afternoon + coast geometry
    coast_normal = _COAST_NORMAL.get(city)
    wind_dir = (wx or {}).get("wind_dir_afternoon")
    if coast_normal is not None and wind_dir is not None:
        angle_diff = (wind_dir - coast_normal) * np.pi / 180
        features["wind_dir_offshore"] = float(np.cos(angle_diff))
    else:
        features["wind_dir_offshore"] = 0.0

    return features


class FeatureActorConfig(ActorConfig):
    model_cycle_seconds: int = 300    # 5 min between model runs
    danger_check_enabled: bool = True  # check exit rules on each event
    live_mode: bool = False            # enables live pollers in Phase 3
    scan_opportunities: bool = True   # scan all instruments for entry opportunities


class FeatureActor(Actor):
    """Bridges climate data sources to trading strategy signals."""

    def __init__(self, config: FeatureActorConfig):
        super().__init__(config)
        self._cfg = config
        self._city_features: dict[tuple[str, str], CityFeatureState] = {}
        self._positions: dict[str, dict] = {}  # ticker -> {side, threshold, city}
        self._events_received: int = 0
        
        # Ensemble model state
        self.ensemble_models = []
        self.ensemble_names = []
        self.ensemble_weights = []
        self._models_loaded = False
        self._kw_config = None
        if load_config:
            try:
                self._kw_config = load_config()
            except Exception as e:
                self.log.error(f"Config load failed: {e}")
        self._load_models()

    def _load_models(self):
        """Load ensemble models from config (ensemble_models + ensemble_weights)."""
        try:
            from kalshi_weather_ml.models.emos import EMOSModel
            from kalshi_weather_ml.models.ngboost_model import NGBoostModel
            from kalshi_weather_ml.models.drn_model import DRNModel

            kw_root = Path(_KW_ROOT)
            models_dir = kw_root / "data" / "models"

            model_loaders = {
                "emos":           lambda: EMOSModel.load(models_dir / "emos_normal.json"),
                "ngboost":        lambda: NGBoostModel.load(models_dir / "ngboost_normal.pkl"),
                "ngboost_spread": lambda: NGBoostModel.load(models_dir / "ngboost_normal_wx_spread.pkl"),
                "drn":            lambda: DRNModel.load(models_dir / "drn_normal"),
                "drn_spread":     lambda: DRNModel.load(models_dir / "drn_normal_wx_spread"),
            }

            # Determine which models to load from config (fallback: emos + ngboost)
            model_names = ["emos", "ngboost"]
            config_weights = {}
            if self._kw_config and hasattr(self._kw_config, "ensemble_models"):
                model_names = self._kw_config.ensemble_models or model_names
            if self._kw_config and hasattr(self._kw_config, "ensemble_weights"):
                config_weights = self._kw_config.ensemble_weights or {}

            for name in model_names:
                loader = model_loaders.get(name)
                if loader is None:
                    self.log.error(f"Unknown model name in ensemble_models: {name!r}")
                    continue
                try:
                    model = loader()
                    weight = config_weights.get(name, 1.0)
                    self.ensemble_models.append(model)
                    self.ensemble_names.append(name)
                    self.ensemble_weights.append(weight)
                    self.log.info(f"Loaded model {name!r} (weight={weight})")
                except Exception as e:
                    self.log.error(f"Failed to load model {name!r}: {e}")

            if self.ensemble_models:
                self._models_loaded = True
                self.log.info(
                    f"Ensemble ready: {len(self.ensemble_models)} models — "
                    + ", ".join(f"{n}={w}" for n, w in zip(self.ensemble_names, self.ensemble_weights))
                )
            else:
                self.log.error("No models loaded — model files not found or all loaders failed")
        except Exception as e:
            self.log.error(f"Failed to load models in FeatureActor: {e}")

    def on_start(self):
        """Subscribe to climate events and start model cycle timer."""
        self.subscribe_data(DataType(ClimateEvent), client_id=ClientId("INTERNAL"))
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

    def _on_model_timer(self, event):
        """Run ML ensemble on accumulated features.

        1. For each city with active positions, re-evaluate and publish ModelSignal
        2. Scan all cached instruments for new entry opportunities
        """
        ts = self.clock.timestamp_ns()
        now_dt = datetime.now()

        # Track tickers already evaluated for position monitoring
        evaluated_tickers: set[str] = set()

        # --- Position monitoring: re-evaluate held positions ---
        if self._positions:
            # Group positions by (city, settlement_date)
            cd_positions: dict[tuple[str, str], list[str]] = {}
            for ticker, info in self._positions.items():
                city = info.get("city", "")
                sd = ""
                if parse_ticker:
                    parsed = parse_ticker(ticker)
                    if parsed:
                        sd = parsed["settlement_date"]
                cd_positions.setdefault((city, sd), []).append(ticker)

            for (city, sd), tickers in cd_positions.items():
                state = self._city_features.get((city, sd))
                if state is None:
                    continue

                features = state.snapshot()
                if not features:
                    continue

                ecmwf = features.get("ecmwf_high")
                gfs = features.get("gfs_high")
                if ecmwf is None or gfs is None:
                    ecmwf = ecmwf or features.get("forecast_high")
                    gfs = gfs or features.get("forecast_high")

                if ecmwf is None or gfs is None:
                    continue

                for ticker in tickers:
                    evaluated_tickers.add(ticker)
                    pos_info = self._positions[ticker]

                    p_win = 0.0
                    model_scores = {}

                    if score_opportunities and parse_ticker and self.ensemble_models:
                        parsed = parse_ticker(ticker)
                        if parsed:
                            try:
                                scores = score_opportunities(
                                    ticker=ticker, city=city, direction="above",
                                    threshold=float(parsed["threshold"]),
                                    settlement_date=parsed["settlement_date"],
                                    ecmwf=ecmwf, gfs=gfs,
                                    models=self.ensemble_models,
                                    model_names=self.ensemble_names,
                                    model_weights=self.ensemble_weights,
                                    now=now_dt,
                                    extra_features=features,
                                )
                                side = pos_info.get("side", "no").lower()
                                for s in scores:
                                    if s.side == side:
                                        p_win = s.p_win
                                        model_scores = s.model_scores
                                        break
                            except Exception as e:
                                self.log.warning(f"Score eval failed for {ticker}: {e}")

                    signal = ModelSignal(
                        city=city,
                        ticker=ticker,
                        side=pos_info.get("side", "no"),
                        p_win=p_win,
                        model_scores=model_scores,
                        features_snapshot=features,
                        ts_event=ts,
                        ts_init=ts,
                    )
                    self.publish_data(DataType(ModelSignal), signal)

        # --- Opportunity scanning: evaluate all cached instruments for new entries ---
        if self._cfg.scan_opportunities:
            self._scan_opportunities(ts, now_dt, evaluated_tickers)

    def _scan_opportunities(
        self,
        ts: int,
        now_dt: datetime,
        skip_tickers: set[str],
    ):
        """Evaluate all cached instruments for entry opportunities."""
        if not (score_and_filter and parse_ticker):
            self.log.error("Cannot scan: scoring functions not available (import failed)")
            return
        if not self._models_loaded:
            self.log.error("Cannot scan: no models loaded")
            return

        instruments = self.cache.instruments()
        if not instruments:
            return

        from kalshi_weather_ml.markets import SERIES_CONFIG

        series_to_city = {s: c for s, c in SERIES_CONFIG}

        # Group instruments by (city, settlement_date), dedup tickers
        cd_tickers: dict[tuple[str, str], list[dict]] = {}
        seen_tickers: set[str] = set()
        for inst in instruments:
            sym = inst.id.symbol.value  # "KXHIGHCHI-26MAR01-T55-NO"
            if not (sym.endswith("-YES") or sym.endswith("-NO")):
                continue
            ticker = sym.rsplit("-", 1)[0]
            if ticker in skip_tickers or ticker in seen_tickers:
                continue
            seen_tickers.add(ticker)

            parsed = parse_ticker(ticker)
            if not parsed:
                continue
            city = series_to_city.get(parsed["series"], "")
            sd = parsed["settlement_date"]
            if city:
                cd_tickers.setdefault((city, sd), []).append(
                    {"ticker": ticker, "parsed": parsed}
                )

        for (city, sd), ticker_list in cd_tickers.items():
            state = self._city_features.get((city, sd))
            features = state.snapshot() if state else {}

            ecmwf = features.get("ecmwf_high")
            gfs = features.get("gfs_high")

            # Bootstrap: fetch forecasts + derived features if not yet accumulated
            if (ecmwf is None or gfs is None) and get_forecast is not None:
                try:
                    ecmwf = ecmwf or get_forecast(city, sd, PRIMARY_MODEL)
                    gfs = gfs or get_forecast(city, sd, CONSENSUS_MODEL)
                    icon = get_forecast(city, sd, "icon_seamless")
                    wx = get_weather_features(city, sd) if get_weather_features else {}

                    if ecmwf is not None or gfs is not None:
                        bootstrap = _build_extra_features(
                            ecmwf, gfs, icon, city, sd, wx,
                        )
                        key = (city, sd)
                        s = self._city_features.setdefault(key, CityFeatureState())
                        s.update("bootstrap_forecast", bootstrap)
                        features = s.snapshot()
                except Exception as e:
                    self.log.warning(f"Bootstrap forecast fetch failed for {city}/{sd}: {e}")

            if ecmwf is None or gfs is None:
                continue

            for entry in ticker_list:
                ticker = entry["ticker"]
                parsed = entry["parsed"]
                market = {
                    "ticker": ticker,
                    "city": city,
                    "direction": "above",
                    "threshold": float(parsed["threshold"]),
                    "settlement_date": parsed["settlement_date"],
                    "yes_bid": 50,
                    "yes_ask": 51,
                }
                try:
                    scored = score_and_filter(
                        ticker=ticker, city=city, direction="above",
                        threshold=float(parsed["threshold"]),
                        settlement_date=parsed["settlement_date"],
                        ecmwf=ecmwf, gfs=gfs, config=self._kw_config,
                        models=self.ensemble_models,
                        model_names=self.ensemble_names,
                        model_weights=self.ensemble_weights,
                        now=now_dt,
                        extra_features=features,
                    )
                    for s in scored:
                        signal = ModelSignal(
                            city=city,
                            ticker=ticker,
                            side=s.side,
                            p_win=s.p_win,
                            model_scores=s.model_scores,
                            features_snapshot=features,
                            ts_event=ts,
                            ts_init=ts,
                        )
                        self.publish_data(DataType(ModelSignal), signal)
                except Exception as e:
                    self.log.warning(f"Scan eval failed for {ticker}: {e}")

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

                features = _build_extra_features(ecmwf, gfs, icon, city, sd, wx)

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
