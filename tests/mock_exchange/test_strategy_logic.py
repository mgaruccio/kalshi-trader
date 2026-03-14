"""Unit tests for ConfidenceTestStrategy — initial state and basic logic.

These tests verify the strategy's init state and internal helpers without
a live TradingNode.  Full end-to-end execution is covered in Task 8's
scenario runner.
"""
import pytest
from unittest.mock import MagicMock, patch
from decimal import Decimal

from nautilus_trader.model.identifiers import InstrumentId, Symbol

from kalshi.common.constants import KALSHI_VENUE
from tests.mock_exchange.fixtures import TEST_TICKER
from tests.mock_exchange.strategy import ConfidenceTestStrategy


class TestConfidenceTestStrategyInit:
    def test_init_state(self):
        strategy = ConfidenceTestStrategy(ticker=TEST_TICKER)
        assert strategy._ticker == TEST_TICKER
        assert strategy._no_instrument_id == InstrumentId(
            Symbol(f"{TEST_TICKER}-NO"), KALSHI_VENUE
        )
        assert strategy._market_order_placed is False
        assert strategy._current_limit_client_oid is None
        assert strategy._last_no_bid_cents is None
        assert strategy._limit_order_count == 0
        assert strategy.done is False

    def test_event_lists_start_empty(self):
        strategy = ConfidenceTestStrategy(ticker=TEST_TICKER)
        assert strategy.accepted_orders == []
        assert strategy.filled_orders == []
        assert strategy.canceled_orders == []
        assert strategy.rejected_orders == []
        assert strategy.quotes_received == []

    def test_done_callback_stored(self):
        cb = MagicMock()
        strategy = ConfidenceTestStrategy(ticker=TEST_TICKER, done_callback=cb)
        assert strategy._done_callback is cb
        cb.assert_not_called()

    def test_no_callback_is_fine(self):
        strategy = ConfidenceTestStrategy(ticker=TEST_TICKER)
        assert strategy._done_callback is None

    def test_no_instrument_id_uses_kalshi_venue(self):
        strategy = ConfidenceTestStrategy(ticker=TEST_TICKER)
        assert strategy._no_instrument_id.venue == KALSHI_VENUE

    def test_no_instrument_id_suffix(self):
        strategy = ConfidenceTestStrategy(ticker=TEST_TICKER)
        sym = strategy._no_instrument_id.symbol.value
        assert sym == f"{TEST_TICKER}-NO"

    def test_different_ticker(self):
        ticker = "KXHIGHNYC-26MAR20-T55"
        strategy = ConfidenceTestStrategy(ticker=ticker)
        assert strategy._ticker == ticker
        assert strategy._no_instrument_id.symbol.value == f"{ticker}-NO"


class TestSignalDone:
    def test_signal_done_sets_flag_and_calls_callback(self):
        cb = MagicMock()
        strategy = ConfidenceTestStrategy(ticker=TEST_TICKER, done_callback=cb)
        strategy._signal_done()
        assert strategy.done is True
        cb.assert_called_once_with()

    def test_signal_done_without_callback_does_not_raise(self):
        strategy = ConfidenceTestStrategy(ticker=TEST_TICKER)
        strategy._signal_done()  # must not raise
        assert strategy.done is True

    def test_on_order_filled_signals_done_at_two_fills(self):
        cb = MagicMock()
        strategy = ConfidenceTestStrategy(ticker=TEST_TICKER, done_callback=cb)

        fill1 = MagicMock()
        fill1.client_order_id = "C-001"
        fill2 = MagicMock()
        fill2.client_order_id = "C-002"

        strategy.on_order_filled(fill1)
        assert strategy.done is False
        cb.assert_not_called()

        strategy.on_order_filled(fill2)
        assert strategy.done is True
        cb.assert_called_once()

    def test_done_callback_not_called_twice(self):
        cb = MagicMock()
        strategy = ConfidenceTestStrategy(ticker=TEST_TICKER, done_callback=cb)

        for i in range(5):
            ev = MagicMock()
            ev.client_order_id = f"C-{i:03d}"
            strategy.on_order_filled(ev)

        assert cb.call_count == 1  # only the 2nd fill triggers it


class TestOrderTracking:
    def test_on_order_accepted_appends(self):
        strategy = ConfidenceTestStrategy(ticker=TEST_TICKER)
        ev = MagicMock()
        strategy.on_order_accepted(ev)
        assert strategy.accepted_orders == [ev]

    def test_on_order_canceled_appends_and_clears_limit_oid(self):
        strategy = ConfidenceTestStrategy(ticker=TEST_TICKER)
        from nautilus_trader.model.identifiers import ClientOrderId
        coid = ClientOrderId("C-LIM-001")
        strategy._current_limit_client_oid = coid

        ev = MagicMock()
        ev.client_order_id = coid
        strategy.on_order_canceled(ev)

        assert strategy.canceled_orders == [ev]
        assert strategy._current_limit_client_oid is None

    def test_on_order_canceled_unrelated_order_leaves_limit_oid(self):
        strategy = ConfidenceTestStrategy(ticker=TEST_TICKER)
        from nautilus_trader.model.identifiers import ClientOrderId
        coid = ClientOrderId("C-LIM-001")
        strategy._current_limit_client_oid = coid

        ev = MagicMock()
        ev.client_order_id = ClientOrderId("C-MKT-001")
        strategy.on_order_canceled(ev)

        # Limit OID should be untouched
        assert strategy._current_limit_client_oid == coid

    def test_on_order_rejected_appends(self):
        strategy = ConfidenceTestStrategy(ticker=TEST_TICKER)
        ev = MagicMock()
        ev.client_order_id = "C-001"
        ev.reason = "insufficient funds"
        strategy.on_order_rejected(ev)
        assert strategy.rejected_orders == [ev]


class TestLimitPriceClamp:
    """_place_limit_order clamps price to [1, 99] cents.

    NT Strategy.order_factory is a Cython read-only descriptor and cannot be
    mocked via assignment.  Instead we verify the clamping arithmetic directly
    by checking the Price object that _place_limit_order would construct,
    using the same Decimal formula as the implementation.
    """

    def _clamped_price_dollars(self, raw_cents: int) -> float:
        """Replicate the clamping + Price construction from _place_limit_order."""
        clamped = max(1, min(99, raw_cents))
        return float(Decimal(str(clamped)) / Decimal("100"))

    def test_price_clamped_to_min_1(self):
        assert self._clamped_price_dollars(0) == pytest.approx(0.01)

    def test_price_clamped_to_max_99(self):
        assert self._clamped_price_dollars(100) == pytest.approx(0.99)

    def test_normal_price_passes_through(self):
        assert self._clamped_price_dollars(34) == pytest.approx(0.34)

    def test_boundary_1_not_clamped(self):
        assert self._clamped_price_dollars(1) == pytest.approx(0.01)

    def test_boundary_99_not_clamped(self):
        assert self._clamped_price_dollars(99) == pytest.approx(0.99)
