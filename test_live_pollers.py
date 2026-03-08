"""Tests for FeatureActor live polling logic.

Run: pytest test_live_pollers.py --noconftest -v

Tests the poller methods in isolation using mocks for kalshi_weather_ml.
Uses the same _TestableFeatureActor pattern as test_feature_actor.py:
bind FeatureActor's unbound methods onto a plain Python stand-in.
"""
import pytest
from unittest.mock import MagicMock, patch

from data_types import ClimateEvent
from exit_rules import CityFeatureState
from feature_actor import FeatureActor, FeatureActorConfig


class _TestableFeatureActor:
    """Pure Python stand-in for FeatureActor with mockable NT attributes."""

    def __init__(self, config=None):
        self._cfg = config or FeatureActorConfig(live_mode=True)
        self._city_features: dict[str, CityFeatureState] = {}
        self._positions: dict[str, dict] = {}
        self._events_received: int = 0
        self.log = MagicMock()
        self.clock = MagicMock()
        self.clock.timestamp_ns.return_value = 999
        self._published: list = []
        self.publish_data = lambda dt, data: self._published.append(data)

    # Bind live poller methods from FeatureActor
    on_data = FeatureActor.on_data
    _start_live_pollers = FeatureActor._start_live_pollers
    _poll_metar = FeatureActor._poll_metar
    _poll_forecasts = FeatureActor._poll_forecasts
    _poll_sst = FeatureActor._poll_sst
    _check_danger = FeatureActor._check_danger

    @property
    def poll_cities(self) -> set[str]:
        if self._positions:
            return {info["city"] for info in self._positions.values()}
        return set()


class TestLivePollers:
    """Test live polling methods on FeatureActor."""

    def _make_actor(self) -> _TestableFeatureActor:
        config = FeatureActorConfig(live_mode=True)
        actor = _TestableFeatureActor(config)
        actor._positions = {
            "KXHIGHCHI-T55": {"side": "no", "threshold": 55.0, "city": "chicago"}
        }
        return actor

    @patch("feature_actor.get_current_temp")
    def test_poll_metar_publishes_event(self, mock_temp):
        """METAR poll should publish ClimateEvent with obs_temp."""
        mock_temp.return_value = 52.0
        actor = self._make_actor()

        actor._poll_metar(MagicMock())

        events = [e for e in actor._published if isinstance(e, ClimateEvent)]
        assert len(events) == 1
        assert events[0].source == "metar_obs"
        assert events[0].city == "chicago"
        assert events[0].features["obs_temp"] == 52.0

    @patch("feature_actor.get_current_temp")
    def test_poll_metar_self_ingests(self, mock_temp):
        """METAR poll should also ingest the event locally."""
        mock_temp.return_value = 52.0
        actor = self._make_actor()

        actor._poll_metar(MagicMock())

        assert actor._events_received == 1
        assert "chicago" in actor._city_features
        assert actor._city_features["chicago"].get("obs_temp") == 52.0

    @patch("feature_actor.get_current_temp")
    def test_poll_metar_none_skipped(self, mock_temp):
        """METAR poll should skip when no temp available."""
        mock_temp.return_value = None
        actor = self._make_actor()

        actor._poll_metar(MagicMock())

        events = [e for e in actor._published if isinstance(e, ClimateEvent)]
        assert len(events) == 0

    def test_poll_metar_no_positions(self):
        """METAR poll should do nothing with no positions."""
        actor = self._make_actor()
        actor._positions = {}

        actor._poll_metar(MagicMock())
        assert len(actor._published) == 0

    @patch("feature_actor.get_current_temp", None)
    def test_poll_metar_import_unavailable(self):
        """METAR poll should do nothing if kalshi_weather_ml not installed."""
        actor = self._make_actor()

        actor._poll_metar(MagicMock())
        assert len(actor._published) == 0

    @patch("feature_actor.get_weather_features")
    @patch("feature_actor.get_forecast")
    def test_poll_forecasts_publishes_event(self, mock_fc, mock_wx):
        """Forecast poll should publish ClimateEvent with forecast features."""
        mock_fc.side_effect = [55.0, 54.0]  # ecmwf, gfs
        mock_wx.return_value = {"wind_speed_max": 12.0}
        actor = self._make_actor()

        actor._poll_forecasts(MagicMock())

        events = [e for e in actor._published if isinstance(e, ClimateEvent)]
        assert len(events) == 1
        assert events[0].features["ecmwf_high"] == 55.0
        assert events[0].features["gfs_high"] == 54.0
        assert events[0].features["forecast_high"] == 55.0  # max
        assert events[0].features["wind_speed_max"] == 12.0

    @patch("feature_actor.get_weather_features")
    @patch("feature_actor.get_forecast")
    def test_poll_forecasts_partial_data(self, mock_fc, mock_wx):
        """Forecast poll should handle partial data (only ecmwf)."""
        mock_fc.side_effect = [55.0, None]  # ecmwf ok, gfs unavailable
        mock_wx.return_value = {}
        actor = self._make_actor()

        actor._poll_forecasts(MagicMock())

        events = [e for e in actor._published if isinstance(e, ClimateEvent)]
        assert len(events) == 1
        assert events[0].features["ecmwf_high"] == 55.0
        assert "gfs_high" not in events[0].features
        assert "forecast_high" not in events[0].features  # needs both

    def test_poll_forecasts_no_positions(self):
        """Forecast poll should do nothing with no positions."""
        actor = self._make_actor()
        actor._positions = {}

        actor._poll_forecasts(MagicMock())
        assert len(actor._published) == 0

    @patch("feature_actor.get_forecast", None)
    def test_poll_forecasts_import_unavailable(self):
        """Forecast poll should do nothing if kalshi_weather_ml not installed."""
        actor = self._make_actor()

        actor._poll_forecasts(MagicMock())
        assert len(actor._published) == 0

    @patch("feature_actor.COASTAL_CITIES", {"chicago"})
    @patch("feature_actor.get_current_sst")
    def test_poll_sst_publishes_event(self, mock_sst):
        """SST poll should publish ClimateEvent for coastal cities."""
        mock_sst.return_value = {"sst_value": 42.3, "sst_anomaly": 1.5}
        actor = self._make_actor()

        actor._poll_sst(MagicMock())

        events = [e for e in actor._published if isinstance(e, ClimateEvent)]
        assert len(events) == 1
        assert events[0].source == "ndbc_buoy_sst"
        assert events[0].features["sst_value"] == 42.3

    @patch("feature_actor.COASTAL_CITIES", set())
    @patch("feature_actor.get_current_sst")
    def test_poll_sst_non_coastal_skipped(self, mock_sst):
        """SST poll should skip non-coastal cities."""
        actor = self._make_actor()

        actor._poll_sst(MagicMock())
        assert len(actor._published) == 0
        mock_sst.assert_not_called()

    def test_poll_sst_no_positions(self):
        """SST poll should do nothing with no positions."""
        actor = self._make_actor()
        actor._positions = {}

        actor._poll_sst(MagicMock())
        assert len(actor._published) == 0

    @patch("feature_actor.get_current_sst", None)
    def test_poll_sst_import_unavailable(self):
        """SST poll should do nothing if kalshi_weather_ml not installed."""
        actor = self._make_actor()

        actor._poll_sst(MagicMock())
        assert len(actor._published) == 0

    def test_poll_cities_property(self):
        """poll_cities should return unique cities from positions."""
        actor = self._make_actor()
        actor._positions = {
            "T55": {"city": "chicago"},
            "T60": {"city": "miami"},
            "T65": {"city": "chicago"},  # duplicate
        }
        assert actor.poll_cities == {"chicago", "miami"}

    def test_poll_cities_empty(self):
        """poll_cities should return empty set with no positions."""
        actor = self._make_actor()
        actor._positions = {}
        assert actor.poll_cities == set()

    @patch("feature_actor.get_current_temp")
    def test_poll_metar_exception_handled(self, mock_temp):
        """Poll failures should log warning, not crash."""
        mock_temp.side_effect = ConnectionError("network down")
        actor = self._make_actor()

        # Should not raise
        actor._poll_metar(MagicMock())
        actor.log.warning.assert_called()

    @patch("feature_actor.get_weather_features")
    @patch("feature_actor.get_forecast")
    def test_poll_forecasts_exception_handled(self, mock_fc, mock_wx):
        """Forecast poll failures should log warning, not crash."""
        mock_fc.side_effect = ConnectionError("network down")
        actor = self._make_actor()

        # Should not raise
        actor._poll_forecasts(MagicMock())
        actor.log.warning.assert_called()

    def test_start_live_pollers_sets_timers(self):
        """_start_live_pollers should set 3 timers."""
        actor = _TestableFeatureActor(FeatureActorConfig(live_mode=True))

        actor._start_live_pollers()

        # Should have called set_timer 3 times (metar, forecast, sst)
        assert actor.clock.set_timer.call_count == 3
        timer_names = [call.args[0] for call in actor.clock.set_timer.call_args_list]
        assert "poll_metar" in timer_names
        assert "poll_forecast" in timer_names
        assert "poll_sst" in timer_names

    @patch("feature_actor.get_current_temp")
    def test_poll_metar_multiple_cities(self, mock_temp):
        """METAR poll should poll all unique cities from positions."""
        mock_temp.return_value = 50.0
        actor = self._make_actor()
        actor._positions = {
            "T55": {"side": "no", "threshold": 55.0, "city": "chicago"},
            "T60": {"side": "no", "threshold": 60.0, "city": "miami"},
        }

        actor._poll_metar(MagicMock())

        events = [e for e in actor._published if isinstance(e, ClimateEvent)]
        assert len(events) == 2
        cities = {e.city for e in events}
        assert cities == {"chicago", "miami"}
