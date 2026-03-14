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
        "_in_entry_phase",
        "_is_tomorrow_contract",
        "_tomorrow_delay_elapsed",
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
        """subscribe_quote_ticks called exactly twice (YES + NO) when contract passes filter."""
        strategy = _make_strategy()
        instrument = MagicMock()
        strategy.cache.instrument.return_value = instrument
        score = _make_score(no_p_win=0.98, n_models=3)
        strategy._handle_signal_score(score)
        assert strategy.subscribe_quote_ticks.call_count == 2


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

    def test_drawdown_circuit_breaker_triggers(self):
        """Drawdown beyond max_drawdown_pct halts the strategy."""
        cfg = WeatherMakerConfig(max_drawdown_pct=0.15)
        strategy = _make_strategy(cfg)
        strategy._initial_balance_cents = 100_000
        # Current balance = 84,000 → drawdown = 16% > 15%
        usd_balance = MagicMock()
        usd_balance.total.as_double.return_value = 840.0  # $840 = 84,000 cents
        strategy.portfolio.account.return_value.balances.return_value = {
            # Use a real Currency key or mock — strategy uses _USD module-level constant
            # so we need the mock to return our balance for any key lookup
        }
        # Patch balances().get() directly
        mock_account = MagicMock()
        mock_account.balances.return_value.get.return_value = usd_balance
        strategy.portfolio.account.return_value = mock_account
        strategy.cache.orders_open.return_value = []
        strategy._check_circuit_breaker()
        assert strategy._halted is True

    def test_drawdown_within_limit_does_not_halt(self):
        """Drawdown within limit does not trigger halt."""
        cfg = WeatherMakerConfig(max_drawdown_pct=0.15)
        strategy = _make_strategy(cfg)
        strategy._initial_balance_cents = 100_000
        # Current balance = 90,000 → drawdown = 10% < 15%
        usd_balance = MagicMock()
        usd_balance.total.as_double.return_value = 900.0  # $900 = 90,000 cents
        mock_account = MagicMock()
        mock_account.balances.return_value.get.return_value = usd_balance
        strategy.portfolio.account.return_value = mock_account
        strategy._check_circuit_breaker()
        assert strategy._halted is False


class TestTimeOfDayGating:
    def test_in_entry_phase_during_window(self):
        """Returns True when current ET time is within [10:30, 15:00)."""
        cfg = WeatherMakerConfig(entry_phase_start_et="10:30", entry_phase_end_et="15:00")
        strategy = _make_strategy(cfg)
        from unittest.mock import patch
        from datetime import datetime, time
        from zoneinfo import ZoneInfo
        # Mock datetime.now to return 12:00 ET
        with patch("kalshi.strategy.datetime") as mock_dt:
            mock_dt.now.return_value = MagicMock(time=lambda: time(12, 0))
            assert strategy._in_entry_phase() is True

    def test_before_entry_phase(self):
        cfg = WeatherMakerConfig(entry_phase_start_et="10:30", entry_phase_end_et="15:00")
        strategy = _make_strategy(cfg)
        from unittest.mock import patch
        from datetime import time
        with patch("kalshi.strategy.datetime") as mock_dt:
            mock_dt.now.return_value = MagicMock(time=lambda: time(9, 0))
            assert strategy._in_entry_phase() is False

    def test_after_entry_phase(self):
        cfg = WeatherMakerConfig(entry_phase_start_et="10:30", entry_phase_end_et="15:00")
        strategy = _make_strategy(cfg)
        from unittest.mock import patch
        from datetime import time
        with patch("kalshi.strategy.datetime") as mock_dt:
            mock_dt.now.return_value = MagicMock(time=lambda: time(16, 0))
            assert strategy._in_entry_phase() is False


class TestTomorrowContractGating:
    def test_today_contract_not_tomorrow(self):
        """Ticker with today's date is not a tomorrow contract."""
        from datetime import date
        today = date.today()
        ticker = f"KXHIGHNY-{today.strftime('%y%b%d').upper()}-T54"
        strategy = _make_strategy()
        assert strategy._is_tomorrow_contract(ticker) is False

    def test_future_contract_is_tomorrow(self):
        """Ticker with tomorrow's date is a tomorrow contract."""
        from datetime import date, timedelta
        tomorrow = date.today() + timedelta(days=1)
        ticker = f"KXHIGHNY-{tomorrow.strftime('%y%b%d').upper()}-T54"
        strategy = _make_strategy()
        assert strategy._is_tomorrow_contract(ticker) is True

    def test_delay_not_elapsed(self):
        """Returns False when not enough time has passed since first quote."""
        cfg = WeatherMakerConfig(tomorrow_min_age_minutes=30)
        strategy = _make_strategy(cfg)
        ticker = "KXHIGHNY-26MAR15-T54"
        # First seen 10 minutes ago (10 * 60 * 1e9 ns)
        ten_min_ago_ns = 1_000_000_000 - int(10 * 60 * 1e9)
        strategy._first_quoted_ns[ticker] = ten_min_ago_ns
        strategy.clock.timestamp_ns.return_value = 1_000_000_000
        assert strategy._tomorrow_delay_elapsed(ticker) is False

    def test_delay_elapsed(self):
        """Returns True when enough time has passed since first quote."""
        cfg = WeatherMakerConfig(tomorrow_min_age_minutes=30)
        strategy = _make_strategy(cfg)
        ticker = "KXHIGHNY-26MAR15-T54"
        # First seen 31 minutes ago
        thirty_one_min_ago_ns = 1_000_000_000 - int(31 * 60 * 1e9)
        strategy._first_quoted_ns[ticker] = thirty_one_min_ago_ns
        strategy.clock.timestamp_ns.return_value = 1_000_000_000
        assert strategy._tomorrow_delay_elapsed(ticker) is True

    def test_first_quoted_tracks_timestamp(self):
        """_first_quoted_ns is set when contract first passes filter."""
        strategy = _make_strategy()
        strategy.cache.instrument.return_value = MagicMock()
        strategy.clock.timestamp_ns.return_value = 42_000_000_000
        score = _make_score(no_p_win=0.98, n_models=3)
        strategy._handle_signal_score(score)
        assert strategy._first_quoted_ns.get("KXHIGHNY-26MAR15-T54") == 42_000_000_000

    def test_first_quoted_not_overwritten_on_second_pass(self):
        """_first_quoted_ns is NOT updated on subsequent filter passes."""
        strategy = _make_strategy()
        strategy.cache.instrument.return_value = MagicMock()
        strategy.clock.timestamp_ns.return_value = 42_000_000_000
        score = _make_score(no_p_win=0.98, n_models=3)
        strategy._handle_signal_score(score)
        # Second score with different timestamp
        strategy.clock.timestamp_ns.return_value = 99_000_000_000
        strategy._quoted_tickers.discard("KXHIGHNY-26MAR15-T54")  # force re-entry
        strategy._handle_signal_score(score)
        # Should still have original timestamp
        assert strategy._first_quoted_ns.get("KXHIGHNY-26MAR15-T54") == 42_000_000_000
