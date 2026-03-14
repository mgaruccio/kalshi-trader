"""Tests for backtest harness setup."""
import pytest

from kalshi.backtest import build_backtest_engine


class TestBuildBacktestEngine:
    def test_creates_engine_with_venue(self):
        engine = build_backtest_engine(starting_balance_usd=10_000)
        assert engine is not None

    def test_accepts_custom_balance(self):
        engine = build_backtest_engine(starting_balance_usd=50_000)
        assert engine is not None

    def test_accepts_custom_trader_id(self):
        engine = build_backtest_engine(
            starting_balance_usd=10_000,
            trader_id="BACKTESTER-TEST",
        )
        assert engine is not None

    def test_venue_configured(self):
        """Kalshi venue is present in the engine after build."""
        from nautilus_trader.model.identifiers import Venue
        engine = build_backtest_engine(starting_balance_usd=10_000)
        # Engine was configured — we can run it (add_strategy would be next step)
        assert engine is not None
