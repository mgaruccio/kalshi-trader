"""Tests for backtest_runner.

Run: pytest test_backtest_runner.py --noconftest -v

Focus on data loading -- the pure Python parts that don't need a full
NT engine running. Engine integration is tested via smoke test.
"""
import json
import pytest
import tempfile
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq


def _make_test_parquet(path: Path, rows: list[dict]):
    """Helper: write test climate events parquet."""
    sources = [r["source"] for r in rows]
    cities = [r["city"] for r in rows]
    dates = [r["date"] for r in rows]
    features = [json.dumps(r["features"]) for r in rows]
    ts_events = [r["ts_event"] for r in rows]
    ts_inits = [r["ts_init"] for r in rows]

    table = pa.table({
        "source": pa.array(sources, type=pa.string()),
        "city": pa.array(cities, type=pa.string()),
        "date": pa.array(dates, type=pa.string()),
        "features": pa.array(features, type=pa.string()),
        "ts_event": pa.array(ts_events, type=pa.int64()),
        "ts_init": pa.array(ts_inits, type=pa.int64()),
    })
    pq.write_table(table, str(path))


class TestLoadClimateEvents:
    def test_basic_load(self, tmp_path):
        """Load parquet and verify ClimateEvent construction."""
        from backtest_runner import load_climate_events

        path = tmp_path / "events.parquet"
        _make_test_parquet(path, [
            {
                "source": "ndbc_buoy_sst",
                "city": "chicago",
                "date": "2026-03-08",
                "features": {"sst_value": 42.3, "sst_anomaly": 1.5},
                "ts_event": 1_000_000_000,
                "ts_init": 1_000_000_000,
            },
            {
                "source": "iem_afd_archive",
                "city": "chicago",
                "date": "2026-03-08",
                "features": {"afd_confidence": 0.8},
                "ts_event": 2_000_000_000,
                "ts_init": 2_000_000_000,
            },
        ])

        events = load_climate_events(path)
        assert len(events) == 2
        assert events[0].source == "ndbc_buoy_sst"
        assert events[0].city == "chicago"
        assert events[0].date == "2026-03-08"
        assert events[0].features["sst_value"] == 42.3
        assert events[0].ts_event == 1_000_000_000

    def test_sorted_by_ts_event(self, tmp_path):
        """Events should be sorted by ts_event ascending."""
        from backtest_runner import load_climate_events

        path = tmp_path / "events.parquet"
        # Write in reverse order
        _make_test_parquet(path, [
            {"source": "b", "city": "miami", "date": "2026-03-08",
             "features": {"val": 2.0}, "ts_event": 2_000, "ts_init": 2_000},
            {"source": "a", "city": "chicago", "date": "2026-03-08",
             "features": {"val": 1.0}, "ts_event": 1_000, "ts_init": 1_000},
        ])

        events = load_climate_events(path)
        assert events[0].ts_event < events[1].ts_event

    def test_features_parsed_correctly(self, tmp_path):
        """Features should be dicts with float values."""
        from backtest_runner import load_climate_events

        path = tmp_path / "events.parquet"
        _make_test_parquet(path, [
            {"source": "ecmwf", "city": "denver", "date": "2026-01-15",
             "features": {"geopotential_500_max": 58200.5, "wind_speed_max": 12.3},
             "ts_event": 100, "ts_init": 100},
        ])

        events = load_climate_events(path)
        assert len(events) == 1
        assert events[0].features["geopotential_500_max"] == 58200.5
        assert isinstance(events[0].features["wind_speed_max"], float)

    def test_empty_parquet(self, tmp_path):
        """Empty parquet should return empty list."""
        from backtest_runner import load_climate_events

        path = tmp_path / "events.parquet"
        _make_test_parquet(path, [])

        events = load_climate_events(path)
        assert events == []

    def test_many_events_performance(self, tmp_path):
        """Should handle thousands of events efficiently."""
        from backtest_runner import load_climate_events

        path = tmp_path / "events.parquet"
        rows = [
            {"source": f"src_{i % 5}", "city": f"city_{i % 10}",
             "date": "2026-01-01", "features": {"val": float(i)},
             "ts_event": i * 1000, "ts_init": i * 1000}
            for i in range(5000)
        ]
        _make_test_parquet(path, rows)

        events = load_climate_events(path)
        assert len(events) == 5000
        # Verify sorted
        for i in range(len(events) - 1):
            assert events[i].ts_event <= events[i + 1].ts_event


class TestCreateEngine:
    def test_engine_creation(self):
        """Engine should create without errors."""
        from backtest_runner import create_backtest_engine

        engine = create_backtest_engine(starting_balance_usd=5_000)
        assert engine is not None
        # Engine should have KALSHI venue configured
        engine.dispose()
