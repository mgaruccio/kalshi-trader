"""Tests for FeatureActor.

Run: pytest test_feature_actor.py --noconftest -v

Approach: Bind FeatureActor's methods onto a pure Python stand-in object.
NT's Actor base is Cython with read-only descriptors (log, clock), so we
cannot use __new__ + attribute assignment. Instead, we create a plain class
with the same state attributes and bind the unbound methods from FeatureActor.
"""
import pytest
from unittest.mock import MagicMock

from data_types import ClimateEvent, ModelSignal, DangerAlert
from exit_rules import CityFeatureState
from feature_actor import FeatureActor, FeatureActorConfig


class _TestableFeatureActor:
    """Pure Python stand-in for FeatureActor with mockable NT attributes."""

    def __init__(self, config=None):
        self._cfg = config or FeatureActorConfig()
        self._city_features: dict[str, CityFeatureState] = {}
        self._positions: dict[str, dict] = {}
        self._events_received: int = 0
        self.log = MagicMock()
        self.clock = MagicMock()
        self.clock.timestamp_ns.return_value = 1_000_000_000
        self._published: list = []
        self.publish_data = lambda dt, data: self._published.append(data)
        
        # Mock ensemble state
        self.ensemble_models = []
        self.ensemble_names = []
        self.ensemble_weights = []
        self._kw_config = None

    # Bind the actual methods from FeatureActor
    on_data = FeatureActor.on_data
    _on_model_timer = FeatureActor._on_model_timer
    _check_danger = FeatureActor._check_danger
    update_positions = FeatureActor.update_positions

    @property
    def events_received(self) -> int:
        return self._events_received

    @property
    def city_features(self) -> dict[str, CityFeatureState]:
        return self._city_features


class TestFeatureActorLogic:
    """Test FeatureActor core logic without full NT Actor lifecycle."""

    def _make_actor(self, **config_kwargs) -> _TestableFeatureActor:
        config = FeatureActorConfig(**config_kwargs)
        return _TestableFeatureActor(config)

    def test_feature_accumulation(self):
        """ClimateEvents should accumulate in CityFeatureState."""
        actor = self._make_actor()

        evt = ClimateEvent(
            source="ndbc_buoy_sst",
            city="chicago",
            features={"sst_value": 42.3, "sst_anomaly": 1.5},
            ts_event=1_000_000_000,
            ts_init=1_000_000_000,
        )
        actor.on_data(evt)

        assert "chicago" in actor._city_features
        state = actor._city_features["chicago"]
        assert state.get("sst_value") == 42.3
        assert actor.events_received == 1

    def test_multi_source_accumulation(self):
        """Multiple sources should merge into same city state."""
        actor = self._make_actor()

        evt1 = ClimateEvent("ndbc", "chicago", {"sst_value": 42.3}, 1, 1)
        evt2 = ClimateEvent("ecmwf", "chicago", {"forecast_high": 55.0}, 2, 2)

        actor.on_data(evt1)
        actor.on_data(evt2)

        state = actor._city_features["chicago"]
        assert state.get("sst_value") == 42.3
        assert state.get("forecast_high") == 55.0
        assert actor.events_received == 2

    def test_danger_check_with_positions(self):
        """Exit rules should fire when features indicate danger."""
        actor = self._make_actor(danger_check_enabled=True)
        actor._positions = {
            "KXHIGHCHI-T55": {"side": "no", "threshold": 55.0, "city": "chicago"}
        }

        # Inject dangerous observation (within 1F of threshold)
        evt = ClimateEvent("metar", "chicago", {"obs_temp": 54.5}, 1, 1)
        actor.on_data(evt)

        danger_alerts = [d for d in actor._published if isinstance(d, DangerAlert)]
        assert len(danger_alerts) >= 1
        assert danger_alerts[0].rule_name == "obs_approaching_threshold"
        assert danger_alerts[0].alert_level in ("CRITICAL", "CAUTION")

    def test_no_danger_without_positions(self):
        """No alerts should fire when no positions are held."""
        actor = self._make_actor(danger_check_enabled=True)
        # No positions

        evt = ClimateEvent("metar", "chicago", {"obs_temp": 54.5}, 1, 1)
        actor.on_data(evt)

        assert len(actor._published) == 0

    def test_model_timer_publishes_signals(self):
        """Model timer should publish ModelSignal for cities with positions."""
        actor = self._make_actor()
        actor._city_features = {"chicago": CityFeatureState()}
        actor._city_features["chicago"].update("ecmwf", {"forecast_high": 55.0})
        actor._positions = {
            "KXHIGHCHI-T55": {"side": "no", "threshold": 55.0, "city": "chicago"}
        }

        actor._on_model_timer(MagicMock())

        signals = [s for s in actor._published if isinstance(s, ModelSignal)]
        assert len(signals) == 1
        assert signals[0].ticker == "KXHIGHCHI-T55"
        assert signals[0].city == "chicago"

    def test_model_timer_no_positions_no_signals(self):
        """No signals when no positions are held."""
        actor = self._make_actor()

        actor._on_model_timer(MagicMock())
        assert len(actor._published) == 0

    def test_update_positions(self):
        """update_positions should replace the positions dict."""
        actor = self._make_actor()

        new_positions = {
            "T55": {"side": "no", "threshold": 55.0, "city": "chicago"},
            "T60": {"side": "no", "threshold": 60.0, "city": "miami"},
        }
        actor.update_positions(new_positions)
        assert len(actor._positions) == 2

    def test_events_received_property(self):
        """events_received should track count."""
        actor = self._make_actor()

        assert actor.events_received == 0
        actor.on_data(ClimateEvent("src", "city", {"k": 1.0}, 0, 0))
        assert actor.events_received == 1

    def test_city_features_property(self):
        """city_features should expose the internal dict."""
        actor = self._make_actor()
        actor._city_features = {"chi": CityFeatureState()}

        assert "chi" in actor.city_features

    def test_danger_check_disabled(self):
        """No alerts should fire when danger_check_enabled is False."""
        actor = self._make_actor(danger_check_enabled=False)
        actor._positions = {
            "KXHIGHCHI-T55": {"side": "no", "threshold": 55.0, "city": "chicago"}
        }

        evt = ClimateEvent("metar", "chicago", {"obs_temp": 54.5}, 1, 1)
        actor.on_data(evt)

        assert len(actor._published) == 0

    def test_model_timer_skips_city_without_features(self):
        """Model timer should skip cities that have no accumulated features."""
        actor = self._make_actor()
        actor._city_features = {}  # no features
        actor._positions = {
            "KXHIGHCHI-T55": {"side": "no", "threshold": 55.0, "city": "chicago"}
        }

        actor._on_model_timer(MagicMock())
        assert len(actor._published) == 0

    def test_model_timer_multiple_tickers_same_city(self):
        """Model timer should publish one signal per ticker."""
        actor = self._make_actor()
        actor._city_features = {"chicago": CityFeatureState()}
        actor._city_features["chicago"].update("ecmwf", {"forecast_high": 55.0})
        actor._positions = {
            "KXHIGHCHI-T55": {"side": "no", "threshold": 55.0, "city": "chicago"},
            "KXHIGHCHI-T60": {"side": "no", "threshold": 60.0, "city": "chicago"},
        }

        actor._on_model_timer(MagicMock())

        signals = [s for s in actor._published if isinstance(s, ModelSignal)]
        assert len(signals) == 2
        tickers = {s.ticker for s in signals}
        assert tickers == {"KXHIGHCHI-T55", "KXHIGHCHI-T60"}

    def test_non_climate_data_ignored(self):
        """on_data should ignore non-ClimateEvent data."""
        actor = self._make_actor()

        # Pass a ModelSignal (wrong type)
        sig = ModelSignal("chi", "T55", "no", 0.95, {}, {}, 0, 0)
        actor.on_data(sig)

        assert actor.events_received == 0
        assert len(actor._city_features) == 0

    def test_model_timer_skips_empty_snapshot(self):
        """Model timer should skip cities whose feature snapshot is empty."""
        actor = self._make_actor()
        # Create state but with no actual features
        actor._city_features = {"chicago": CityFeatureState()}
        actor._positions = {
            "KXHIGHCHI-T55": {"side": "no", "threshold": 55.0, "city": "chicago"}
        }

        actor._on_model_timer(MagicMock())
        assert len(actor._published) == 0
