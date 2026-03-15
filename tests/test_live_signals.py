"""Live integration tests — signal server compat, filter acceptance, backtest smoke.

These tests hit the real signal server at localhost:8000 and exercise actual
code paths with real data. They skip (not fail) when infrastructure is
unavailable.

Run in isolation (like test_live_confidence.py):
    uv run python -m pytest tests/test_live_signals.py -v

Requires either:
    - Signal server running locally on port 8000, or
    - SSH tunnel: ssh -L 8000:localhost:8000 root@161.35.114.105 -N
"""
from __future__ import annotations

import socket
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Skip helpers
# ---------------------------------------------------------------------------

SIGNAL_SERVER_URL = "http://localhost:8000"
SIGNAL_FILE = Path("data/model_signals.parquet")
CATALOG_PATH = Path("kalshi_data_catalog")


def _signal_server_reachable() -> bool:
    """Check if signal server is listening on localhost:8000."""
    try:
        with socket.create_connection(("localhost", 8000), timeout=2):
            return True
    except OSError:
        return False


skip_no_server = pytest.mark.skipif(
    not _signal_server_reachable(),
    reason="Signal server not reachable at localhost:8000",
)

skip_no_signal_file = pytest.mark.skipif(
    not SIGNAL_FILE.exists(),
    reason=f"Signal file not found: {SIGNAL_FILE}",
)

skip_no_catalog = pytest.mark.skipif(
    not CATALOG_PATH.exists(),
    reason=f"Catalog directory not found: {CATALOG_PATH}",
)


# ---------------------------------------------------------------------------
# TestSignalServerCompat
# ---------------------------------------------------------------------------


@skip_no_server
class TestSignalServerCompat:
    """Validates producer/consumer agreement between signal server and parser."""

    def _fetch_scores(self) -> list[dict]:
        import httpx

        resp = httpx.get(f"{SIGNAL_SERVER_URL}/v1/trading/scores", timeout=30.0)
        resp.raise_for_status()
        return resp.json()

    def test_all_items_parse_to_signal_score(self):
        """Every item from /v1/trading/scores must parse to a non-None SignalScore."""
        from kalshi.signal_actor import parse_score_msg

        items = self._fetch_scores()
        assert len(items) > 0, "Signal server returned empty scores list"

        failed = []
        for i, item in enumerate(items):
            score = parse_score_msg(item, ts_ns=0)
            if score is None:
                failed.append((i, item.get("ticker", "???")))

        assert not failed, (
            f"{len(failed)}/{len(items)} items failed parse_score_msg: "
            f"{failed[:5]}"
        )

    def test_n_models_at_least_2(self):
        """All scores must have n_models >= 2 (ensemble requirement)."""
        from kalshi.signal_actor import parse_score_msg

        items = self._fetch_scores()
        for item in items:
            score = parse_score_msg(item, ts_ns=0)
            if score is not None:
                assert score.n_models >= 2, (
                    f"{score.ticker}: n_models={score.n_models}, expected >= 2"
                )

    def test_status_accepted_by_should_quote(self):
        """Status values from the server must not be rejected as non-tradeable."""
        from kalshi.signal_actor import parse_score_msg

        items = self._fetch_scores()
        rejected_statuses = set()
        for item in items:
            score = parse_score_msg(item, ts_ns=0)
            if score is not None and score.status and score.status not in ("open", "active"):
                rejected_statuses.add(score.status)

        assert not rejected_statuses, (
            f"Server returned statuses that should_quote() would reject: {rejected_statuses}"
        )

    def test_required_fields_present_and_valid(self):
        """Required fields must be present and in valid ranges."""
        from kalshi.signal_actor import parse_score_msg

        items = self._fetch_scores()
        for item in items:
            score = parse_score_msg(item, ts_ns=0)
            if score is None:
                continue

            assert score.ticker, f"Empty ticker in score"
            assert score.yes_bid >= 0, f"{score.ticker}: yes_bid={score.yes_bid} < 0"
            assert score.yes_ask >= 0, f"{score.ticker}: yes_ask={score.yes_ask} < 0"
            assert score.threshold != 0.0, (
                f"{score.ticker}: threshold=0.0 — signal conversion data loss"
            )


# ---------------------------------------------------------------------------
# TestFilterAcceptsRealSignals
# ---------------------------------------------------------------------------


@skip_no_server
class TestFilterAcceptsRealSignals:
    """Validates the filter doesn't reject everything from the real server."""

    def test_at_least_one_score_passes_filter(self):
        """High-confidence contracts always exist — at least one must pass."""
        import httpx

        from kalshi.signal_actor import parse_score_msg
        from kalshi.strategy import WeatherMakerConfig, should_quote

        resp = httpx.get(f"{SIGNAL_SERVER_URL}/v1/trading/scores", timeout=30.0)
        resp.raise_for_status()
        items = resp.json()
        assert len(items) > 0

        config = WeatherMakerConfig()
        passing = []
        for item in items:
            score = parse_score_msg(item, ts_ns=0)
            if score is None:
                continue
            side, passes = should_quote(config, score, drift_cities=set())
            if passes:
                passing.append((score.ticker, side))

        assert len(passing) >= 1, (
            f"0/{len(items)} scores passed should_quote() — "
            f"filter is rejecting all real signals"
        )

    def test_passing_scores_have_correct_side(self):
        """Scores that pass the filter must have side set based on probabilities."""
        import httpx

        from kalshi.signal_actor import parse_score_msg
        from kalshi.strategy import WeatherMakerConfig, should_quote

        resp = httpx.get(f"{SIGNAL_SERVER_URL}/v1/trading/scores", timeout=30.0)
        resp.raise_for_status()
        items = resp.json()

        config = WeatherMakerConfig()
        for item in items:
            score = parse_score_msg(item, ts_ns=0)
            if score is None:
                continue
            side, passes = should_quote(config, score, drift_cities=set())
            if passes:
                if score.no_p_win >= score.yes_p_win:
                    assert side == "no", (
                        f"{score.ticker}: no_p_win={score.no_p_win} >= "
                        f"yes_p_win={score.yes_p_win} but side={side}"
                    )
                else:
                    assert side == "yes", (
                        f"{score.ticker}: yes_p_win={score.yes_p_win} > "
                        f"no_p_win={score.no_p_win} but side={side}"
                    )


# ---------------------------------------------------------------------------
# TestBacktestPipelineSmoke
# ---------------------------------------------------------------------------


@skip_no_signal_file
@skip_no_catalog
class TestBacktestPipelineSmoke:
    """Validates the full backtest pipeline executes with real data."""

    def test_full_pipeline_produces_results(self):
        """Load real signals + catalog, run backtest, assert non-zero activity."""
        from kalshi.backtest import run_full_backtest
        from kalshi.backtest_loader import load_signal_file
        from kalshi.backtest_results import extract_results

        scores = load_signal_file(SIGNAL_FILE)
        assert len(scores) > 0, "Signal file loaded 0 scores"

        engine, strategy = run_full_backtest(
            catalog_path=CATALOG_PATH,
            scores=scores,
        )

        results = extract_results(engine, strategy)

        assert results.signals_received > 0, (
            f"signals_received={results.signals_received} — signals not reaching strategy"
        )
        assert results.filter_passes > 0, (
            f"filter_passes={results.filter_passes} — filter rejecting all real signals"
        )
        assert results.orders_submitted > 0, (
            f"orders_submitted={results.orders_submitted} — no orders placed"
        )
        assert results.fill_rate > 0, (
            f"fill_rate={results.fill_rate} — no fills occurred"
        )


# ---------------------------------------------------------------------------
# TestSignalFileRoundTrip
# ---------------------------------------------------------------------------


class TestSignalFileRoundTrip:
    """Validates parquet serialization preserves all SignalScore fields."""

    def test_round_trip_preserves_values(self, tmp_path):
        """Create SignalScore objects, write to parquet, read back, assert exact match."""
        from nautilus_trader.persistence.catalog import ParquetDataCatalog

        from kalshi.backtest_loader import load_signal_file
        from kalshi.signals import SignalScore

        # Create catalog in temp dir
        catalog = ParquetDataCatalog(str(tmp_path / "catalog"))

        # Known values
        scores = [
            SignalScore(
                ticker="KXHIGHNY-26MAR15-T54",
                city="new_york",
                threshold=54.0,
                direction="above",
                no_p_win=0.97,
                yes_p_win=0.03,
                no_margin=7.5,
                n_models=3,
                emos_no=0.95,
                ngboost_no=0.98,
                drn_no=0.97,
                yes_bid=85,
                yes_ask=90,
                status="active",
                ts_event=1_710_000_000_000_000_000,
                ts_init=1_710_000_000_000_000_000,
            ),
            SignalScore(
                ticker="KXHIGHCHI-26MAR15-T48",
                city="chicago",
                threshold=48.0,
                direction="above",
                no_p_win=0.96,
                yes_p_win=0.04,
                no_margin=5.2,
                n_models=2,
                emos_no=0.94,
                ngboost_no=0.97,
                drn_no=0.0,
                yes_bid=80,
                yes_ask=87,
                status="active",
                ts_event=1_710_000_001_000_000_000,
                ts_init=1_710_000_001_000_000_000,
            ),
        ]

        # Write via catalog
        catalog.write_data(scores)

        # Read back via our loader
        parquet_file = tmp_path / "catalog" / "signal_score.parquet"
        if not parquet_file.exists():
            # NT may use a different path structure — find the parquet file
            parquet_files = list((tmp_path / "catalog").rglob("*.parquet"))
            assert parquet_files, "No parquet files written by catalog"
            parquet_file = parquet_files[0]

        loaded = load_signal_file(parquet_file)
        assert len(loaded) == 2

        # Sort both by ticker for deterministic comparison
        original = sorted(scores, key=lambda s: s.ticker)
        loaded = sorted(loaded, key=lambda s: s.ticker)

        for orig, got in zip(original, loaded):
            assert got.ticker == orig.ticker
            assert got.city == orig.city
            assert got.threshold == pytest.approx(orig.threshold)
            assert got.direction == orig.direction
            assert got.no_p_win == pytest.approx(orig.no_p_win)
            assert got.yes_p_win == pytest.approx(orig.yes_p_win)
            assert got.no_margin == pytest.approx(orig.no_margin)
            assert got.n_models == orig.n_models
            assert got.emos_no == pytest.approx(orig.emos_no)
            assert got.ngboost_no == pytest.approx(orig.ngboost_no)
            assert got.drn_no == pytest.approx(orig.drn_no)
            assert got.yes_bid == orig.yes_bid
            assert got.yes_ask == orig.yes_ask
            assert got.status == orig.status
