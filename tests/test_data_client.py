"""Tests for KalshiDataClient — quote derivation and book management."""
import pytest

from kalshi.data import _derive_quotes


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
