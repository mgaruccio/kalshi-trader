"""Tests for custom signal data types."""
import pytest
from nautilus_trader.core.data import Data
from nautilus_trader.model.data import DataType

from kalshi.signals import ForecastDrift, SignalScore


def _make_score(**kwargs) -> SignalScore:
    defaults = dict(
        ticker="KXHIGHNY-26MAR15-T54",
        city="new_york",
        threshold=54.0,
        direction="above",
        no_p_win=0.983,
        yes_p_win=0.017,
        no_margin=7.2,
        n_models=3,
        emos_no=0.952,
        ngboost_no=0.991,
        drn_no=0.988,
        yes_bid=85,
        yes_ask=90,
        status="open",
        ts_event=1_000_000_000,
        ts_init=1_000_000_000,
    )
    defaults.update(kwargs)
    return SignalScore(**defaults)


class TestSignalScore:
    def test_is_data_subclass(self):
        score = _make_score()
        assert isinstance(score, Data)

    def test_roundtrip_dict(self):
        score = _make_score()
        d = score.to_dict()
        restored = SignalScore.from_dict(d)
        assert restored.ticker == "KXHIGHNY-26MAR15-T54"
        assert restored.city == "new_york"
        assert restored.no_p_win == pytest.approx(0.983)
        assert restored.n_models == 3
        assert restored.yes_bid == 85
        assert restored.ts_event == 1_000_000_000

    def test_roundtrip_bytes(self):
        score = _make_score()
        raw = score.to_bytes()
        restored = SignalScore.from_bytes(raw)
        assert restored.ticker == "KXHIGHNY-26MAR15-T54"
        assert restored.no_p_win == pytest.approx(0.983)

    def test_data_type_creation(self):
        dt = DataType(SignalScore)
        assert dt.type == SignalScore

    def test_winning_side_no(self):
        """When no_p_win > yes_p_win, NO is the winning side."""
        score = _make_score(no_p_win=0.983, yes_p_win=0.017, ts_event=0, ts_init=0)
        assert score.no_p_win > score.yes_p_win

    def test_status_field_present(self):
        """status field is captured and survives roundtrip."""
        score = _make_score(status="open")
        assert score.status == "open"
        d = score.to_dict()
        restored = SignalScore.from_dict(d)
        assert restored.status == "open"

    def test_status_default_empty(self):
        """status field defaults to empty string."""
        score = _make_score(status="")
        assert score.status == ""

    def test_per_model_scores_stored(self):
        """Individual model probabilities are stored correctly."""
        score = _make_score(emos_no=0.952, ngboost_no=0.991, drn_no=0.988)
        assert score.emos_no == pytest.approx(0.952)
        assert score.ngboost_no == pytest.approx(0.991)
        assert score.drn_no == pytest.approx(0.988)


class TestForecastDrift:
    def test_is_data_subclass(self):
        drift = ForecastDrift(
            city="new_york",
            date="2026-03-15",
            message="ECMWF shifted +2.3F in 30min",
            ts_event=1_000_000_000,
            ts_init=1_000_000_000,
        )
        assert isinstance(drift, Data)

    def test_roundtrip_dict(self):
        drift = ForecastDrift(
            city="new_york",
            date="2026-03-15",
            message="ECMWF shifted +2.3F in 30min",
            ts_event=1_000_000_000,
            ts_init=1_000_000_000,
        )
        d = drift.to_dict()
        restored = ForecastDrift.from_dict(d)
        assert restored.city == "new_york"
        assert restored.date == "2026-03-15"
        assert restored.message == "ECMWF shifted +2.3F in 30min"

    def test_roundtrip_bytes(self):
        drift = ForecastDrift(
            city="new_york",
            date="2026-03-15",
            message="ECMWF shifted +2.3F in 30min",
            ts_event=1_000_000_000,
            ts_init=1_000_000_000,
        )
        raw = drift.to_bytes()
        restored = ForecastDrift.from_bytes(raw)
        assert restored.city == "new_york"
        assert restored.message == "ECMWF shifted +2.3F in 30min"

    def test_data_type_creation(self):
        dt = DataType(ForecastDrift)
        assert dt.type == ForecastDrift
