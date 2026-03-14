"""Tests for backtest data loading — signal server backfill → SignalScore list."""
import pytest

from kalshi.backtest_loader import parse_backfill_response, _iso_to_ns
from kalshi.signals import SignalScore


def _make_item(**overrides) -> dict:
    item = {
        "ticker": "KXHIGHNY-26MAR15-T54",
        "city": "new_york",
        "threshold": 54.0,
        "direction": "above",
        "no_p_win": 0.98,
        "yes_p_win": 0.02,
        "no_margin": 7.0,
        "n_models": 3,
        "emos_no": 0.95,
        "ngboost_no": 0.99,
        "drn_no": 0.98,
        "yes_bid": 85,
        "yes_ask": 90,
        "status": "open",
        "timestamp": "2026-03-14T12:00:00Z",
    }
    item.update(overrides)
    return item


class TestIsoToNs:
    def test_known_timestamp(self):
        """2026-03-14T12:00:00Z converts to a deterministic ns value."""
        ns = _iso_to_ns("2026-03-14T12:00:00Z")
        assert ns > 0
        # Sanity check: 2026 is past 2020
        ns_2020 = _iso_to_ns("2020-01-01T00:00:00Z")
        assert ns > ns_2020

    def test_plus_utc_suffix(self):
        """Also handles +00:00 suffix."""
        ns_z = _iso_to_ns("2026-03-14T12:00:00Z")
        ns_plus = _iso_to_ns("2026-03-14T12:00:00+00:00")
        assert ns_z == ns_plus

    def test_different_times_ordered(self):
        """Earlier timestamp produces smaller ns value."""
        ns_early = _iso_to_ns("2026-03-14T12:00:00Z")
        ns_late = _iso_to_ns("2026-03-14T14:00:00Z")
        assert ns_early < ns_late


class TestParseBackfillResponse:
    def test_parses_single_score(self):
        data = [_make_item()]
        scores = parse_backfill_response(data)
        assert len(scores) == 1
        assert isinstance(scores[0], SignalScore)
        assert scores[0].ticker == "KXHIGHNY-26MAR15-T54"
        assert scores[0].ts_event > 0  # parsed from timestamp

    def test_sorted_by_timestamp(self):
        data = [
            _make_item(ticker="T1", timestamp="2026-03-14T14:00:00Z"),
            _make_item(ticker="T2", city="chicago", threshold=60.0, timestamp="2026-03-14T12:00:00Z"),
        ]
        scores = parse_backfill_response(data)
        assert scores[0].ticker == "T2"  # earlier timestamp first
        assert scores[1].ticker == "T1"

    def test_empty_response(self):
        scores = parse_backfill_response([])
        assert scores == []

    def test_invalid_scores_dropped(self):
        """Items that fail parse_score_msg validation are silently dropped."""
        data = [
            _make_item(ticker="VALID"),
            _make_item(ticker="INVALID", no_p_win=2.0),  # out of range
        ]
        scores = parse_backfill_response(data)
        assert len(scores) == 1
        assert scores[0].ticker == "VALID"

    def test_status_passed_through(self):
        data = [_make_item(status="open")]
        scores = parse_backfill_response(data)
        assert scores[0].status == "open"

    def test_multiple_scores_sorted(self):
        """Multiple scores across different timestamps are sorted correctly."""
        data = [
            _make_item(ticker="C", timestamp="2026-03-14T16:00:00Z"),
            _make_item(ticker="A", timestamp="2026-03-14T10:00:00Z"),
            _make_item(ticker="B", timestamp="2026-03-14T13:00:00Z"),
        ]
        scores = parse_backfill_response(data)
        assert [s.ticker for s in scores] == ["A", "B", "C"]
