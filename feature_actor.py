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
import sys
from datetime import datetime, timedelta
from pathlib import Path

# Add kalshi-weather package to path
sys.path.append("/home/mike/code/altmarkets/kalshi-weather/src")

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
        self._city_features: dict[str, CityFeatureState] = {}
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

            kw_root = Path("/home/mike/code/altmarkets/kalshi-weather")
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
        self.subscribe_data(DataType(ClimateEvent), client_id=ClientId("CLIMATE"))
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
            cities_with_positions: dict[str, list[str]] = {}
            for ticker, info in self._positions.items():
                city = info.get("city", "")
                cities_with_positions.setdefault(city, []).append(ticker)

            for city, tickers in cities_with_positions.items():
                state = self._city_features.get(city)
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

        # Group instruments by city, dedup tickers
        city_tickers: dict[str, list[dict]] = {}
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
            if city:
                city_tickers.setdefault(city, []).append(
                    {"ticker": ticker, "parsed": parsed}
                )

        for city, ticker_list in city_tickers.items():
            state = self._city_features.get(city)
            if state is None:
                continue
            features = state.snapshot()
            ecmwf = features.get("ecmwf_high")
            gfs = features.get("gfs_high")
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
        state = self._city_features.get(city)
        if state is None:
            self.log.warning(f"No feature state for {city}, skipping danger check")
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
