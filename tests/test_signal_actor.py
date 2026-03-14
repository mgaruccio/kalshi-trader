"""Tests for SignalActor message parsing and event publishing."""
import pytest

from kalshi.signal_actor import SignalActorConfig, parse_score_msg, parse_alert_msg
from kalshi.signals import ForecastDrift, SignalScore


class TestParseScoreMsg:
    """Test parsing signal server score messages into SignalScore."""

    def _valid_msg(self, **overrides):
        msg = {
            "ticker": "KXHIGHNY-26MAR15-T54",
            "city": "new_york",
            "threshold": 54.0,
            "direction": "above",
            "no_p_win": 0.9834,
            "yes_p_win": 0.0166,
            "no_margin": 7.2,
            "n_models": 3,
            "emos_no": 0.952,
            "ngboost_no": 0.991,
            "drn_no": 0.988,
            "yes_bid": 85,
            "yes_ask": 90,
            "status": "open",
        }
        msg.update(overrides)
        return msg

    def test_parse_valid_score(self):
        msg = self._valid_msg()
        ts = 1_000_000_000
        score = parse_score_msg(msg, ts)
        assert isinstance(score, SignalScore)
        assert score.ticker == "KXHIGHNY-26MAR15-T54"
        assert score.city == "new_york"
        assert score.no_p_win == pytest.approx(0.9834)
        assert score.n_models == 3
        assert score.yes_bid == 85
        assert score.status == "open"
        assert score.ts_event == 1_000_000_000

    def test_parse_score_missing_optional_models(self):
        """When n_models < 3, missing model scores default to 0.0."""
        msg = {
            "ticker": "KXHIGHNY-26MAR15-T54",
            "city": "new_york",
            "threshold": 54.0,
            "direction": "above",
            "no_p_win": 0.95,
            "yes_p_win": 0.05,
            "no_margin": 5.0,
            "n_models": 1,
            "emos_no": 0.95,
            "yes_bid": 80,
            "yes_ask": 85,
            "status": "open",
        }
        score = parse_score_msg(msg, 1_000_000_000)
        assert score.n_models == 1
        assert score.emos_no == pytest.approx(0.95)
        assert score.ngboost_no == 0.0
        assert score.drn_no == 0.0

    def test_parse_score_status_field(self):
        """status field is passed through to SignalScore."""
        msg = self._valid_msg(status="open")
        score = parse_score_msg(msg, 0)
        assert score.status == "open"

    def test_parse_score_status_missing_defaults_empty(self):
        """Missing status defaults to empty string."""
        msg = self._valid_msg()
        del msg["status"]
        score = parse_score_msg(msg, 0)
        assert score.status == ""

    # --- Input validation (Enhancement #13) ---

    def test_invalid_p_win_out_of_range_returns_none(self):
        """p_win outside [0, 1] returns None."""
        msg = self._valid_msg(no_p_win=1.5)
        assert parse_score_msg(msg, 0) is None

    def test_invalid_p_win_negative_returns_none(self):
        msg = self._valid_msg(yes_p_win=-0.1)
        assert parse_score_msg(msg, 0) is None

    def test_invalid_n_models_zero_returns_none(self):
        """n_models must be 1-3."""
        msg = self._valid_msg(n_models=0)
        assert parse_score_msg(msg, 0) is None

    def test_invalid_n_models_too_large_returns_none(self):
        msg = self._valid_msg(n_models=4)
        assert parse_score_msg(msg, 0) is None

    def test_invalid_yes_bid_out_of_range_returns_none(self):
        """yes_bid must be 0-99."""
        msg = self._valid_msg(yes_bid=100)
        assert parse_score_msg(msg, 0) is None

    def test_invalid_yes_bid_negative_returns_none(self):
        msg = self._valid_msg(yes_bid=-1)
        assert parse_score_msg(msg, 0) is None

    def test_boundary_values_pass(self):
        """Exactly boundary values are valid."""
        msg = self._valid_msg(no_p_win=0.0, yes_p_win=1.0, n_models=1, yes_bid=0)
        score = parse_score_msg(msg, 0)
        assert score is not None

    def test_boundary_values_max_pass(self):
        msg = self._valid_msg(no_p_win=1.0, yes_p_win=0.0, n_models=3, yes_bid=99)
        score = parse_score_msg(msg, 0)
        assert score is not None


class TestParseAlertMsg:
    """Test parsing signal server alert messages into ForecastDrift."""

    def test_parse_forecast_drift(self):
        msg = {
            "type": "alert",
            "alert_type": "forecast_drift",
            "city": "new_york",
            "date": "2026-03-15",
            "message": "ECMWF shifted +2.3F in 30min",
            "details": {},
            "timestamp": "2026-03-14T12:40:00Z",
        }
        drift = parse_alert_msg(msg, 1_000_000_000)
        assert isinstance(drift, ForecastDrift)
        assert drift.city == "new_york"
        assert drift.date == "2026-03-15"
        assert drift.message == "ECMWF shifted +2.3F in 30min"

    def test_non_drift_alert_returns_none(self):
        """Non-forecast_drift alerts are ignored."""
        msg = {
            "type": "alert",
            "alert_type": "other_alert",
            "city": "new_york",
            "date": "2026-03-15",
            "message": "something else",
            "details": {},
            "timestamp": "2026-03-14T12:40:00Z",
        }
        result = parse_alert_msg(msg, 1_000_000_000)
        assert result is None

    def test_missing_alert_type_returns_none(self):
        """Missing alert_type returns None."""
        msg = {"type": "alert", "city": "new_york", "date": "2026-03-15", "message": "x"}
        result = parse_alert_msg(msg, 0)
        assert result is None


class TestSignalActorConfig:
    def test_defaults(self):
        cfg = SignalActorConfig(
            signal_ws_url="ws://localhost:8000/v1/stream",
            signal_http_url="http://localhost:8000",
        )
        assert cfg.signal_ws_url == "ws://localhost:8000/v1/stream"
        assert cfg.signal_http_url == "http://localhost:8000"

    def test_inherits_actor_config(self):
        from nautilus_trader.config import ActorConfig
        cfg = SignalActorConfig(
            signal_ws_url="ws://localhost:8000/v1/stream",
            signal_http_url="http://localhost:8000",
        )
        assert isinstance(cfg, ActorConfig)
