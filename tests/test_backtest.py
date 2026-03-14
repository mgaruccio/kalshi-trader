"""Tests for backtest harness setup."""
import tempfile
import pytest

from nautilus_trader.backtest.engine import BacktestEngine
from nautilus_trader.model.enums import OmsType
from nautilus_trader.model.objects import Money
from nautilus_trader.model.currencies import USD

from kalshi.backtest import build_backtest_engine, run_full_backtest
from kalshi.common.constants import KALSHI_VENUE
from kalshi.strategy import WeatherMakerConfig, WeatherMakerStrategy


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


class TestRunFullBacktest:
    """Tests for run_full_backtest() convenience function."""

    def test_returns_engine_and_strategy_tuple(self):
        """run_full_backtest returns (BacktestEngine, WeatherMakerStrategy) tuple."""
        with tempfile.TemporaryDirectory() as tmpdir:
            engine, strategy = run_full_backtest(
                catalog_path=tmpdir,
                scores=[],
            )
        assert isinstance(engine, BacktestEngine)
        assert isinstance(strategy, WeatherMakerStrategy)

    def test_accepts_custom_strategy_config(self):
        """Strategy config is passed through to the strategy instance."""
        config = WeatherMakerConfig(confidence_threshold=0.99, ladder_depth=2)
        with tempfile.TemporaryDirectory() as tmpdir:
            _, strategy = run_full_backtest(
                catalog_path=tmpdir,
                scores=[],
                strategy_config=config,
            )
        assert strategy._config.confidence_threshold == 0.99
        assert strategy._config.ladder_depth == 2

    def test_default_strategy_config_used_when_none(self):
        """When strategy_config=None, defaults are applied."""
        with tempfile.TemporaryDirectory() as tmpdir:
            _, strategy = run_full_backtest(
                catalog_path=tmpdir,
                scores=[],
                strategy_config=None,
            )
        assert strategy._config.confidence_threshold == WeatherMakerConfig().confidence_threshold

    def test_runs_cleanly_with_empty_catalog_and_no_scores(self):
        """Empty catalog and zero scores completes without error."""
        with tempfile.TemporaryDirectory() as tmpdir:
            engine, strategy = run_full_backtest(
                catalog_path=tmpdir,
                scores=[],
            )
        # Engine ran — no exception raised

    def test_starting_balance_passed_to_engine(self):
        """starting_balance_usd is reflected in the engine account balance."""
        with tempfile.TemporaryDirectory() as tmpdir:
            engine, _ = run_full_backtest(
                catalog_path=tmpdir,
                scores=[],
                starting_balance_usd=25_000,
            )
        account = engine.portfolio.account(KALSHI_VENUE)
        assert account is not None
        bal = account.balances().get(USD)
        assert bal is not None
        assert int(bal.total.as_double()) == 25_000
