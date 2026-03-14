"""Tests for WeatherMakerStrategy lifecycle and event wiring."""
import types
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from kalshi.signals import ForecastDrift, SignalScore
from kalshi.strategy import WeatherMakerConfig, WeatherMakerStrategy


def _make_score(
    ticker: str = "KXHIGHNY-26MAR15-T54",
    city: str = "new_york",
    no_p_win: float = 0.98,
    yes_p_win: float = 0.02,
    n_models: int = 3,
    status: str = "open",
) -> SignalScore:
    return SignalScore(
        ticker=ticker,
        city=city,
        threshold=54.0,
        direction="above",
        no_p_win=no_p_win,
        yes_p_win=yes_p_win,
        no_margin=7.0,
        n_models=n_models,
        emos_no=0.95,
        ngboost_no=0.99,
        drn_no=0.98,
        yes_bid=85,
        yes_ask=90,
        status=status,
        ts_event=0,
        ts_init=0,
    )


def _make_strategy(config: WeatherMakerConfig | None = None):
    """Create a testable strategy stub using SimpleNamespace + MethodType.

    Avoids Cython descriptor issues — NT Strategy is a Cython class so
    object.__new__ is not safe. Bind real methods onto a plain namespace.
    """
    cfg = config or WeatherMakerConfig()
    stub = SimpleNamespace(
        _config=cfg,
        log=MagicMock(),
        clock=MagicMock(),
        cache=MagicMock(),
        portfolio=MagicMock(),
        id=MagicMock(),
        subscribe_data=MagicMock(),
        subscribe_quote_ticks=MagicMock(),
        publish_data=MagicMock(),
        submit_order=MagicMock(),
        cancel_order=MagicMock(),
        cancel_orders=MagicMock(),
        order_factory=MagicMock(),
    )
    stub.clock.timestamp_ns.return_value = 1_000_000_000

    # Initialize state by binding and calling _init_state
    stub._init_state = types.MethodType(WeatherMakerStrategy._init_state, stub)
    stub._init_state(cfg)

    # Bind all real methods we need to test
    for method_name in (
        "_handle_signal_score",
        "_handle_forecast_drift",
        "_evaluate_contract",
        "_cancel_all_for_ticker",
        "_market_exposure_cents",
        "_city_exposure_cents",
        "_check_circuit_breaker",
        "_trigger_halt",
        "on_stop",
        "on_start",
    ):
        method = getattr(WeatherMakerStrategy, method_name)
        setattr(stub, method_name, types.MethodType(method, stub))

    return stub


class TestStrategyState:
    def test_initial_state_empty(self):
        strategy = _make_strategy()
        assert strategy._scores == {}
        assert strategy._drift_cities == set()
        assert strategy._quoted_tickers == set()
        assert strategy._halted is False

    def test_on_signal_score_updates_scores(self):
        strategy = _make_strategy()
        score = _make_score()
        strategy._handle_signal_score(score)
        assert "KXHIGHNY-26MAR15-T54" in strategy._scores
        assert strategy._scores["KXHIGHNY-26MAR15-T54"] is score

    def test_on_forecast_drift_adds_city(self):
        strategy = _make_strategy()
        drift = ForecastDrift(
            city="new_york",
            date="2026-03-15",
            message="ECMWF shifted +2.3F",
            ts_event=0,
            ts_init=0,
        )
        strategy._handle_forecast_drift(drift)
        assert "new_york" in strategy._drift_cities

    def test_score_update_does_not_clear_drift(self):
        """Per Enhancement #18: drift is NOT cleared by a new score. Persists until session end."""
        strategy = _make_strategy()
        strategy._drift_cities.add("new_york")
        score = _make_score(city="new_york")
        strategy._handle_signal_score(score)
        # Drift still present — no auto-clear
        assert "new_york" in strategy._drift_cities


class TestFilterEvaluation:
    def test_passing_contract_added_to_quoted(self):
        """When a high-confidence score arrives, contract is added to _quoted_tickers."""
        strategy = _make_strategy()
        # Mock cache.instrument to return a valid instrument
        strategy.cache.instrument.return_value = MagicMock()
        score = _make_score(no_p_win=0.98, n_models=3)
        strategy._handle_signal_score(score)
        assert "KXHIGHNY-26MAR15-T54" in strategy._quoted_tickers

    def test_failing_contract_removed_from_quoted(self):
        """When drift pauses a city, contract is removed from _quoted_tickers."""
        strategy = _make_strategy()
        strategy.cache.instrument.return_value = MagicMock()
        score = _make_score(city="new_york", no_p_win=0.98, n_models=3)
        strategy._handle_signal_score(score)
        assert "KXHIGHNY-26MAR15-T54" in strategy._quoted_tickers

        # Now drift arrives
        drift = ForecastDrift(city="new_york", date="2026-03-15", message="drift", ts_event=0, ts_init=0)
        strategy._handle_forecast_drift(drift)
        assert "KXHIGHNY-26MAR15-T54" not in strategy._quoted_tickers

    def test_subscribe_quote_ticks_called_on_new_pass(self):
        """subscribe_quote_ticks called when contract first passes filter."""
        strategy = _make_strategy()
        instrument = MagicMock()
        strategy.cache.instrument.return_value = instrument
        score = _make_score(no_p_win=0.98, n_models=3)
        strategy._handle_signal_score(score)
        # subscribe_quote_ticks should have been called for YES and NO instruments
        assert strategy.subscribe_quote_ticks.call_count >= 1


class TestOnStop:
    def test_on_stop_cancels_open_orders(self):
        """on_stop cancels all open orders via cache.orders_open + cancel_orders."""
        strategy = _make_strategy()
        open_orders = [MagicMock(), MagicMock()]
        strategy.cache.orders_open.return_value = open_orders
        strategy.on_stop()
        strategy.cache.orders_open.assert_called_once_with(strategy_id=strategy.id)
        strategy.cancel_orders.assert_called_once_with(open_orders)


class TestCircuitBreaker:
    def test_halted_flag_blocks_quoting(self):
        """When _halted is True, _evaluate_contract exits early."""
        strategy = _make_strategy()
        strategy._halted = True
        strategy.cache.instrument.return_value = MagicMock()
        score = _make_score(no_p_win=0.98, n_models=3)
        strategy._handle_signal_score(score)
        # Should not be quoted since halted
        assert "KXHIGHNY-26MAR15-T54" not in strategy._quoted_tickers

    def test_halt_file_triggers_halt(self, tmp_path):
        """When halt file exists, strategy halts."""
        halt_file = tmp_path / "kalshi-halt"
        halt_file.touch()
        cfg = WeatherMakerConfig(halt_file_path=str(halt_file))
        strategy = _make_strategy(cfg)
        strategy.cache.orders_open.return_value = []
        strategy._check_circuit_breaker()
        assert strategy._halted is True
