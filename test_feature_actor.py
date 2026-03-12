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
        self._city_features: dict[tuple[str, str], CityFeatureState] = {}
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
        self._models_loaded = False
        self._kw_config = None

        # Mock cache for opportunity scanning
        self.cache = MagicMock()
        self.cache.instruments.return_value = []

    # Bind the actual methods from FeatureActor
    on_data = FeatureActor.on_data
    _active_dates_for_city = FeatureActor._active_dates_for_city
    _on_model_timer = FeatureActor._on_model_timer
    _scan_opportunities = FeatureActor._scan_opportunities
    _check_danger = FeatureActor._check_danger
    update_positions = FeatureActor.update_positions

    @property
    def events_received(self) -> int:
        return self._events_received

    @property
    def city_features(self) -> dict[tuple[str, str], CityFeatureState]:
        return self._city_features


class TestFeatureActorLogic:
    """Test FeatureActor core logic without full NT Actor lifecycle."""

    def _make_actor(self, **config_kwargs) -> _TestableFeatureActor:
        config = FeatureActorConfig(**config_kwargs)
        return _TestableFeatureActor(config)

    def test_feature_accumulation(self):
        """City-level events with no active dates go to fallback bucket."""
        actor = self._make_actor()

        evt = ClimateEvent(
            source="ndbc_buoy_sst",
            city="chicago",
            features={"sst_value": 42.3, "sst_anomaly": 1.5},
            ts_event=1_000_000_000,
            ts_init=1_000_000_000,
        )
        actor.on_data(evt)

        assert ("chicago", "") in actor._city_features
        state = actor._city_features[("chicago", "")]
        assert state.get("sst_value") == 42.3
        assert actor.events_received == 1

    def test_date_specific_event(self):
        """Date-specific events go to exact (city, date) bucket."""
        actor = self._make_actor()

        evt = ClimateEvent(
            source="ecmwf_forecast",
            city="chicago",
            features={"ecmwf_high": 55.0},
            ts_event=1_000_000_000,
            ts_init=1_000_000_000,
            date="2026-03-10",
        )
        actor.on_data(evt)

        assert ("chicago", "2026-03-10") in actor._city_features
        state = actor._city_features[("chicago", "2026-03-10")]
        assert state.get("ecmwf_high") == 55.0
        # Should NOT be in fallback
        assert ("chicago", "") not in actor._city_features

    def test_date_specific_isolation(self):
        """Denver Mar 10 and Mar 11 forecasts don't cross-contaminate."""
        actor = self._make_actor()

        evt_mar10 = ClimateEvent(
            source="ecmwf_forecast", city="denver",
            features={"ecmwf_high": 68.0, "gfs_high": 65.0},
            ts_event=1_000, ts_init=1_000,
            date="2026-03-10",
        )
        evt_mar11 = ClimateEvent(
            source="ecmwf_forecast", city="denver",
            features={"ecmwf_high": 46.1, "gfs_high": 44.0},
            ts_event=2_000, ts_init=2_000,
            date="2026-03-11",
        )
        actor.on_data(evt_mar10)
        actor.on_data(evt_mar11)

        # Each date has its own features
        mar10 = actor._city_features[("denver", "2026-03-10")]
        mar11 = actor._city_features[("denver", "2026-03-11")]
        assert mar10.get("ecmwf_high") == 68.0
        assert mar11.get("ecmwf_high") == 46.1
        # Mar 10 NOT overwritten by Mar 11
        assert mar10.get("ecmwf_high") != mar11.get("ecmwf_high")

    def test_city_level_event_fans_out(self):
        """City-level events fan out to all active (city, date) buckets."""
        actor = self._make_actor()

        # First create date-specific buckets via forecast events
        actor.on_data(ClimateEvent(
            source="ecmwf_forecast", city="chicago",
            features={"ecmwf_high": 55.0}, ts_event=1, ts_init=1,
            date="2026-03-10",
        ))
        actor.on_data(ClimateEvent(
            source="ecmwf_forecast", city="chicago",
            features={"ecmwf_high": 60.0}, ts_event=2, ts_init=2,
            date="2026-03-11",
        ))

        # Now send a city-level event (no date) with positions that have dates
        actor._positions = {
            "KXHIGHCHI-26MAR10-T55": {"side": "no", "threshold": 55.0, "city": "chicago"},
            "KXHIGHCHI-26MAR11-T60": {"side": "no", "threshold": 60.0, "city": "chicago"},
        }
        actor.on_data(ClimateEvent(
            source="metar_obs", city="chicago",
            features={"obs_temp": 52.0}, ts_event=3, ts_init=3,
        ))

        # obs_temp should be in both date buckets
        assert actor._city_features[("chicago", "2026-03-10")].get("obs_temp") == 52.0
        assert actor._city_features[("chicago", "2026-03-11")].get("obs_temp") == 52.0
        # Forecasts should still be date-isolated
        assert actor._city_features[("chicago", "2026-03-10")].get("ecmwf_high") == 55.0
        assert actor._city_features[("chicago", "2026-03-11")].get("ecmwf_high") == 60.0

    def test_fallback_seeds_new_date(self):
        """City-level events arriving before forecasts get seeded into new date buckets."""
        actor = self._make_actor()

        # City-level event arrives first (no active dates -> fallback)
        actor.on_data(ClimateEvent(
            source="ndbc_buoy_sst", city="miami",
            features={"sst_value": 78.0}, ts_event=1, ts_init=1,
        ))
        assert ("miami", "") in actor._city_features

        # Now a date-specific forecast arrives -- should seed from fallback
        actor.on_data(ClimateEvent(
            source="ecmwf_forecast", city="miami",
            features={"ecmwf_high": 85.0}, ts_event=2, ts_init=2,
            date="2026-03-10",
        ))

        state = actor._city_features[("miami", "2026-03-10")]
        assert state.get("ecmwf_high") == 85.0
        # Seeded from fallback
        assert state.get("sst_value") == 78.0

    def test_multi_source_accumulation(self):
        """Multiple date-specific sources merge into same (city, date) state."""
        actor = self._make_actor()

        evt1 = ClimateEvent("ecmwf_forecast", "chicago",
                            {"ecmwf_high": 55.0}, 1, 1, date="2026-03-10")
        evt2 = ClimateEvent("openmeteo_pressure_levels", "chicago",
                            {"geopotential_500_max": 58200.0}, 2, 2, date="2026-03-10")

        actor.on_data(evt1)
        actor.on_data(evt2)

        state = actor._city_features[("chicago", "2026-03-10")]
        assert state.get("ecmwf_high") == 55.0
        assert state.get("geopotential_500_max") == 58200.0
        assert actor.events_received == 2

    def test_danger_check_with_positions(self):
        """Exit rules should fire when features indicate danger."""
        actor = self._make_actor(danger_check_enabled=True)
        actor._positions = {
            "KXHIGHCHI-26MAR10-T55": {"side": "no", "threshold": 55.0, "city": "chicago"}
        }

        # Inject dangerous observation via date-specific event
        actor._city_features[("chicago", "2026-03-10")] = CityFeatureState()
        evt = ClimateEvent("metar", "chicago", {"obs_temp": 54.5}, 1, 1,
                           date="2026-03-10")
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

    def test_model_timer_no_models_no_signals(self):
        """Model timer without scoring functions should not emit signals.

        Previously this emitted p_win=0.0 signals. After the silent-failure
        fix, no signal is emitted when scoring is unavailable.
        """
        actor = self._make_actor()
        actor._city_features[("chicago", "2026-03-10")] = CityFeatureState()
        actor._city_features[("chicago", "2026-03-10")].update(
            "ecmwf", {"forecast_high": 55.0})
        actor._positions = {
            "KXHIGHCHI-26MAR10-T55": {"side": "no", "threshold": 55.0, "city": "chicago"}
        }

        actor._on_model_timer(MagicMock())

        signals = [s for s in actor._published if isinstance(s, ModelSignal)]
        assert len(signals) == 0

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
        actor._city_features = {("chi", ""): CityFeatureState()}

        assert ("chi", "") in actor.city_features

    def test_danger_check_disabled(self):
        """No alerts should fire when danger_check_enabled is False."""
        actor = self._make_actor(danger_check_enabled=False)
        actor._positions = {
            "KXHIGHCHI-26MAR10-T55": {"side": "no", "threshold": 55.0, "city": "chicago"}
        }

        evt = ClimateEvent("metar", "chicago", {"obs_temp": 54.5}, 1, 1)
        actor.on_data(evt)

        assert len(actor._published) == 0

    def test_model_timer_skips_city_without_features(self):
        """Model timer should skip cities that have no accumulated features."""
        actor = self._make_actor()
        actor._city_features = {}  # no features
        actor._positions = {
            "KXHIGHCHI-26MAR10-T55": {"side": "no", "threshold": 55.0, "city": "chicago"}
        }

        actor._on_model_timer(MagicMock())
        assert len(actor._published) == 0

    def test_model_timer_multiple_tickers_no_models_no_signals(self):
        """Without scoring functions, no signals emitted even with multiple tickers."""
        actor = self._make_actor()
        actor._city_features[("chicago", "2026-03-10")] = CityFeatureState()
        actor._city_features[("chicago", "2026-03-10")].update(
            "ecmwf", {"forecast_high": 55.0})
        actor._positions = {
            "KXHIGHCHI-26MAR10-T55": {"side": "no", "threshold": 55.0, "city": "chicago"},
            "KXHIGHCHI-26MAR10-T60": {"side": "no", "threshold": 60.0, "city": "chicago"},
        }

        actor._on_model_timer(MagicMock())

        signals = [s for s in actor._published if isinstance(s, ModelSignal)]
        assert len(signals) == 0

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
        actor._city_features[("chicago", "2026-03-10")] = CityFeatureState()
        actor._positions = {
            "KXHIGHCHI-26MAR10-T55": {"side": "no", "threshold": 55.0, "city": "chicago"}
        }

        actor._on_model_timer(MagicMock())
        assert len(actor._published) == 0
