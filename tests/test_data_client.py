"""Tests for KalshiDataClient — quote derivation and book management."""
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from kalshi.data import KalshiDataClient, _derive_quotes


class TestDeriveQuotes:
    def test_normal_book(self):
        yes_book = {0.32: 5.0, 0.31: 10.0}
        no_book = {0.68: 3.0, 0.67: 7.0}
        quotes = _derive_quotes("TICKER", yes_book, no_book)
        assert quotes["YES"]["bid"] == 0.32
        assert quotes["YES"]["ask"] == pytest.approx(1.0 - 0.68)
        assert quotes["YES"]["bid_size"] == 5.0
        assert quotes["YES"]["ask_size"] == 3.0
        assert quotes["NO"]["bid"] == 0.68
        assert quotes["NO"]["ask"] == pytest.approx(1.0 - 0.32)

    def test_empty_yes_book(self):
        quotes = _derive_quotes("TICKER", {}, {0.68: 3.0})
        assert quotes["YES"]["bid"] == 0.0
        assert quotes["YES"]["ask"] == pytest.approx(1.0 - 0.68)
        assert quotes["NO"]["bid"] == 0.68
        assert quotes["NO"]["ask"] == 1.0

    def test_empty_both_books(self):
        quotes = _derive_quotes("TICKER", {}, {})
        assert quotes is None

    def test_empty_no_book(self):
        """YES data only — NO book empty. NO ask derived from YES bid; YES ask = 1.0."""
        quotes = _derive_quotes("TICKER", {0.32: 5.0}, {})
        assert quotes["YES"]["bid"] == 0.32
        assert quotes["YES"]["ask"] == 1.0  # no NO bids → 1.0 - 0.0
        assert quotes["YES"]["bid_size"] == 5.0
        assert quotes["YES"]["ask_size"] == 0.0
        assert quotes["NO"]["bid"] == 0.0
        assert quotes["NO"]["ask"] == pytest.approx(1.0 - 0.32)
        assert quotes["NO"]["ask_size"] == 5.0

    def test_single_level_each_side(self):
        quotes = _derive_quotes("TICKER", {0.50: 10.0}, {0.50: 10.0})
        assert quotes["YES"]["bid"] == 0.50
        assert quotes["YES"]["ask"] == 0.50
        assert quotes["NO"]["bid"] == 0.50
        assert quotes["NO"]["ask"] == 0.50


class TestOrderbookDelta:
    """Tests for _handle_orderbook_delta — negative delta removal."""

    def _make_data_client_stub(self):
        """Create a minimal stub with the delta handler and book state."""
        stub = SimpleNamespace(
            _books={},
            _instrument_provider=MagicMock(),
            _clock=MagicMock(),
        )
        stub._instrument_provider.find.return_value = None  # suppress _emit_quotes
        # Bind the real methods
        import types
        stub._handle_orderbook_delta = types.MethodType(
            KalshiDataClient._handle_orderbook_delta, stub,
        )
        stub._emit_quotes = types.MethodType(
            KalshiDataClient._emit_quotes, stub,
        )
        return stub

    def test_negative_delta_removes_level(self):
        """Negative delta_fp should remove the price level, not store a negative quantity."""
        stub = self._make_data_client_stub()
        ticker = "KXHIGHNY-26MAR15-T54"
        # Pre-seed a book with a level at price 0.85
        stub._books[ticker] = {"yes": {0.85: 72.0}, "no": {}}

        msg = SimpleNamespace(
            market_ticker=ticker,
            side="yes",
            price_dollars="0.85",
            delta_fp="-72",
        )
        stub._handle_orderbook_delta(msg)

        # Level should be removed, not stored as -72
        assert 0.85 not in stub._books[ticker]["yes"]

    def test_zero_delta_removes_level(self):
        """Zero delta_fp should also remove the price level."""
        stub = self._make_data_client_stub()
        ticker = "KXHIGHNY-26MAR15-T54"
        stub._books[ticker] = {"yes": {0.85: 50.0}, "no": {}}

        msg = SimpleNamespace(
            market_ticker=ticker,
            side="yes",
            price_dollars="0.85",
            delta_fp="0",
        )
        stub._handle_orderbook_delta(msg)

        assert 0.85 not in stub._books[ticker]["yes"]

    def test_positive_delta_stored(self):
        """Positive delta_fp should be stored as the new quantity."""
        stub = self._make_data_client_stub()
        ticker = "KXHIGHNY-26MAR15-T54"
        stub._books[ticker] = {"yes": {}, "no": {}}

        msg = SimpleNamespace(
            market_ticker=ticker,
            side="yes",
            price_dollars="0.85",
            delta_fp="72",
        )
        stub._handle_orderbook_delta(msg)

        assert stub._books[ticker]["yes"][0.85] == 72.0

    def test_negative_delta_then_emit_no_error(self):
        """After removing a level via negative delta, _emit_quotes should not raise."""
        stub = self._make_data_client_stub()
        ticker = "KXHIGHNY-26MAR15-T54"
        stub._books[ticker] = {"yes": {0.85: 72.0, 0.84: 30.0}, "no": {0.16: 10.0}}

        msg = SimpleNamespace(
            market_ticker=ticker,
            side="yes",
            price_dollars="0.85",
            delta_fp="-72",
        )
        stub._handle_orderbook_delta(msg)

        # Book should still have the 0.84 level, not the 0.85
        assert 0.85 not in stub._books[ticker]["yes"]
        assert stub._books[ticker]["yes"][0.84] == 30.0

        # _derive_quotes should work without errors (no negative quantities)
        quotes = _derive_quotes(ticker, stub._books[ticker]["yes"], stub._books[ticker]["no"])
        assert quotes is not None
        assert quotes["YES"]["bid"] == 0.84
        assert quotes["YES"]["bid_size"] == 30.0
