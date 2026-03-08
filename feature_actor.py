"""Climate feature accumulation and signal generation.

FeatureActor sits between raw climate data and the trading strategy.
It accumulates ClimateEvent data per-city, periodically runs the ML
ensemble (emitting ModelSignal), and continuously checks exit rules
(emitting DangerAlert).
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
        self.log.info(f"FeatureActor started (cycle={self._cfg.model_cycle_seconds}s)")

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

    @property
    def events_received(self) -> int:
        return self._events_received

    @property
    def city_features(self) -> dict[str, CityFeatureState]:
        return self._city_features
