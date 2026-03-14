"""Tests for backtest harness setup."""
import pytest

from nautilus_trader.model.enums import OmsType
from nautilus_trader.model.objects import Money
from nautilus_trader.model.currencies import USD

from kalshi.backtest import build_backtest_engine
from kalshi.common.constants import KALSHI_VENUE


class TestBuildBacktestEngine:
    def test_returns_engine(self):
        engine = build_backtest_engine(starting_balance_usd=10_000)
        assert engine is not None

    def test_kalshi_venue_registered(self):
        """KALSHI venue is present after build."""
        engine = build_backtest_engine(starting_balance_usd=10_000)
        assert KALSHI_VENUE in engine.list_venues()

    def test_trader_id_set(self):
        """trader_id is set correctly on the engine."""
        engine = build_backtest_engine(
            starting_balance_usd=10_000,
            trader_id="BACKTESTER-TEST",
        )
        assert str(engine.trader_id) == "BACKTESTER-TEST"

    def test_default_trader_id(self):
        """Default trader_id is BACKTESTER-001."""
        engine = build_backtest_engine(starting_balance_usd=10_000)
        assert str(engine.trader_id) == "BACKTESTER-001"

    def test_different_balances_produce_different_engines(self):
        """Two engines with different balances are independent objects."""
        engine_a = build_backtest_engine(starting_balance_usd=10_000)
        engine_b = build_backtest_engine(starting_balance_usd=50_000)
        assert engine_a is not engine_b
