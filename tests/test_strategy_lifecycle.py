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
        "_exit_position",
        "_reprice_ladder",
        "_market_exposure_cents",
        "_city_exposure_cents",
        "_check_circuit_breaker",
        "_portfolio_value_cents",
        "_trigger_halt",
        "on_order_filled",
        "on_order_rejected",
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

    def test_cache_add_instrument_called_before_subscribe(self):
        """cache.add_instrument() is called before subscribe_quote_ticks() for each side."""
        strategy = _make_strategy()
        instrument = MagicMock()
        strategy.cache.instrument.return_value = instrument
        call_order = []
        strategy.cache.add_instrument = MagicMock(side_effect=lambda i: call_order.append("add"))
        strategy.subscribe_quote_ticks = MagicMock(side_effect=lambda i: call_order.append("sub"))
        score = _make_score(no_p_win=0.98, n_models=3)
        strategy._handle_signal_score(score)
        # For each of YES and NO: add must precede subscribe
        assert call_order == ["add", "sub", "add", "sub"]


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
        """Drawdown beyond max_drawdown_pct halts the strategy (no open positions)."""
        cfg = WeatherMakerConfig(max_drawdown_pct=0.15)
        strategy = _make_strategy(cfg)
        strategy._initial_balance_cents = 100_000
        # Current balance = 84,000 → drawdown = 16% > 15%
        usd_balance = MagicMock()
        usd_balance.total.as_double.return_value = 840.0  # $840 = 84,000 cents
        mock_account = MagicMock()
        mock_account.balances.return_value.get.return_value = usd_balance
        strategy.portfolio.account.return_value = mock_account
        strategy.cache.orders_open.return_value = []
        strategy.cache.positions.return_value = []  # no positions — pure cash loss
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
        strategy.cache.positions.return_value = []
        strategy._check_circuit_breaker()
        assert strategy._halted is False

    def test_positions_offset_cash_drawdown(self):
        """Cash spent on positions doesn't trigger breaker when positions have value."""
        cfg = WeatherMakerConfig(max_drawdown_pct=0.15)
        strategy = _make_strategy(cfg)
        strategy._initial_balance_cents = 2_000  # $20
        # Cash dropped to 1,615c (bought 5 @ 77c), but positions worth 490c
        # Portfolio value = 1615 + 490 = 2105 → no drawdown
        usd_balance = MagicMock()
        usd_balance.total.as_double.return_value = 16.15
        mock_account = MagicMock()
        mock_account.balances.return_value.get.return_value = usd_balance
        strategy.portfolio.account.return_value = mock_account
        # Mock an open position with last bid
        position = MagicMock()
        position.is_closed = False
        position.quantity.as_double.return_value = 5.0
        position.instrument_id = MagicMock()
        strategy.cache.positions.return_value = [position]
        last_tick = MagicMock()
        last_tick.bid_price = 0.98  # 98c
        strategy.cache.quote_tick.return_value = last_tick
        strategy._check_circuit_breaker()
        assert strategy._halted is False

    def test_small_account_uses_relaxed_limit(self):
        """Accounts below threshold use small_account_drawdown_pct."""
        cfg = WeatherMakerConfig(
            max_drawdown_pct=0.15,
            small_account_drawdown_pct=0.25,
            small_account_threshold_usd=100,
        )
        strategy = _make_strategy(cfg)
        strategy._initial_balance_cents = 2_000  # $20 < $100 threshold
        # 20% drawdown — exceeds 15% but within 25% small account limit
        usd_balance = MagicMock()
        usd_balance.total.as_double.return_value = 16.0  # $16 = 1600c
        mock_account = MagicMock()
        mock_account.balances.return_value.get.return_value = usd_balance
        strategy.portfolio.account.return_value = mock_account
        strategy.cache.positions.return_value = []
        strategy._check_circuit_breaker()
        assert strategy._halted is False


class TestExitGuard:
    def test_exit_in_progress_blocks_second_exit(self):
        """Duplicate IOC exits are blocked when _exiting_tickers contains the ticker."""
        from nautilus_trader.model.identifiers import InstrumentId, Symbol
        from kalshi.common.constants import KALSHI_VENUE

        strategy = _make_strategy()
        ticker = "KXHIGHNY-26MAR15-T54"
        instrument_id = InstrumentId(Symbol(f"{ticker}-NO"), KALSHI_VENUE)

        # Set up: no open positions (so no real order placed, but guard still tested)
        strategy.cache.positions.return_value = []
        # Mark as already exiting
        strategy._exiting_tickers.add(ticker)

        strategy._exit_position(ticker, instrument_id, 97)
        # submit_order should NOT be called — guarded
        strategy.submit_order.assert_not_called()

    def test_exit_guard_cleared_on_fill(self):
        """_exiting_tickers cleared when order fill event arrives."""
        from nautilus_trader.model.identifiers import InstrumentId, Symbol
        from kalshi.common.constants import KALSHI_VENUE

        strategy = _make_strategy()
        ticker = "KXHIGHNY-26MAR15-T54"
        instrument_id = InstrumentId(Symbol(f"{ticker}-NO"), KALSHI_VENUE)
        strategy._exiting_tickers.add(ticker)

        event = MagicMock()
        event.instrument_id = instrument_id
        event.order_side = MagicMock()
        event.last_qty = MagicMock()
        event.last_px = MagicMock()
        strategy.on_order_filled(event)
        assert ticker not in strategy._exiting_tickers

    def test_exit_guard_cleared_on_reject(self):
        """_exiting_tickers cleared when order rejection event arrives."""
        from nautilus_trader.model.identifiers import InstrumentId, Symbol
        from kalshi.common.constants import KALSHI_VENUE

        strategy = _make_strategy()
        ticker = "KXHIGHNY-26MAR15-T54"
        instrument_id = InstrumentId(Symbol(f"{ticker}-NO"), KALSHI_VENUE)
        strategy._exiting_tickers.add(ticker)

        event = MagicMock()
        event.instrument_id = instrument_id
        event.reason = "test rejection"
        strategy.on_order_rejected(event)
        assert ticker not in strategy._exiting_tickers


class TestDiagnosticCounters:
    def test_signals_received_increments(self):
        """_signals_received increments once per _handle_signal_score call."""
        strategy = _make_strategy()
        strategy.cache.instrument.return_value = MagicMock()
        assert strategy._signals_received == 0
        score = _make_score()
        strategy._handle_signal_score(score)
        assert strategy._signals_received == 1
        strategy._handle_signal_score(score)
        assert strategy._signals_received == 2

    def test_filter_passes_increments_on_new_pass(self):
        """_filter_passes increments when a contract newly passes the filter."""
        strategy = _make_strategy()
        strategy.cache.instrument.return_value = MagicMock()
        assert strategy._filter_passes == 0
        score = _make_score(no_p_win=0.98, n_models=3)
        strategy._handle_signal_score(score)
        assert strategy._filter_passes == 1

    def test_filter_fails_increments_on_fail(self):
        """_filter_fails increments when a contract fails the filter."""
        strategy = _make_strategy()
        strategy.cache.instrument.return_value = MagicMock()
        assert strategy._filter_fails == 0
        # Low confidence — should fail
        score = _make_score(no_p_win=0.50, yes_p_win=0.50, n_models=3)
        strategy._handle_signal_score(score)
        assert strategy._filter_fails == 1

    def test_filter_fails_increments_on_exit(self):
        """_filter_fails increments when a previously-passing contract exits the filter."""
        strategy = _make_strategy()
        strategy.cache.instrument.return_value = MagicMock()
        score = _make_score(no_p_win=0.98, n_models=3)
        strategy._handle_signal_score(score)
        assert strategy._filter_passes == 1
        assert strategy._filter_fails == 0

        # Drift pauses the city — contract fails and exits
        drift = ForecastDrift(city="new_york", date="2026-03-15", message="drift", ts_event=0, ts_init=0)
        strategy._handle_forecast_drift(drift)
        assert strategy._filter_fails == 1

    def test_ladders_placed_and_orders_submitted(self):
        """_ladders_placed and _orders_submitted increment when _reprice_ladder places orders."""
        from nautilus_trader.model.identifiers import InstrumentId, Symbol
        from kalshi.common.constants import KALSHI_VENUE

        strategy = _make_strategy()
        ticker = "KXHIGHNY-26MAR15-T54"
        instrument_id = InstrumentId(Symbol(f"{ticker}-NO"), KALSHI_VENUE)
        score = _make_score()

        # Set up account balance and instrument mocks
        instrument = MagicMock()
        instrument.make_qty.side_effect = lambda q: q
        instrument.make_price.side_effect = lambda p: p
        strategy.cache.instrument.return_value = instrument
        strategy.cache.orders_open.return_value = []

        usd_balance = MagicMock()
        usd_balance.total.as_double.return_value = 1000.0  # $1000
        mock_account = MagicMock()
        mock_account.balances.return_value.get.return_value = usd_balance
        strategy.portfolio.account.return_value = mock_account

        assert strategy._ladders_placed == 0
        assert strategy._orders_submitted == 0

        strategy._reprice_ladder(ticker, instrument_id, "no", 85, score)

        assert strategy._ladders_placed == 1
        # Default config: ladder_depth=3, so up to 3 orders placed
        assert strategy._orders_submitted == strategy.submit_order.call_count
        assert strategy._orders_submitted > 0

    def test_exits_attempted_increments(self):
        """_exits_attempted increments when _exit_position submits an IOC order."""
        from nautilus_trader.model.identifiers import InstrumentId, Symbol
        from kalshi.common.constants import KALSHI_VENUE

        strategy = _make_strategy()
        ticker = "KXHIGHNY-26MAR15-T54"
        instrument_id = InstrumentId(Symbol(f"{ticker}-NO"), KALSHI_VENUE)

        # Set up a non-zero position
        pos = MagicMock()
        pos.instrument_id = instrument_id
        pos.is_closed = False
        pos.quantity.as_double.return_value = 5.0
        strategy.cache.positions.return_value = [pos]
        instrument = MagicMock()
        strategy.cache.instrument.return_value = instrument
        strategy.cache.orders_open.return_value = []

        assert strategy._exits_attempted == 0
        strategy._exit_position(ticker, instrument_id, 97)
        assert strategy._exits_attempted == 1


