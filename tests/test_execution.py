"""Tests for KalshiExecutionClient — order translation and fill processing."""
import asyncio
import pytest
from collections import OrderedDict
from unittest.mock import AsyncMock, MagicMock, patch

from nautilus_trader.model.enums import OrderSide, OrderType, TimeInForce
from nautilus_trader.model.identifiers import (
    ClientOrderId, InstrumentId, Symbol, VenueOrderId,
)
from nautilus_trader.model.objects import Money, Price, Quantity
from nautilus_trader.model.currencies import USD

from kalshi.common.constants import KALSHI_VENUE
from kalshi.execution import _order_to_kalshi_params, _parse_fill_commission, _DEDUP_MAX


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_order(side: OrderSide, ticker: str, order_side_suffix: str, price_dollars: float, qty: int = 5, tif: TimeInForce = TimeInForce.GTC):
    """Build a minimal mock LimitOrder."""
    order = MagicMock()
    order.side = side
    order.order_type = OrderType.LIMIT
    order.time_in_force = tif
    order.price = Price.from_str(f"{price_dollars:.2f}")
    order.quantity = Quantity.from_int(qty)
    order.client_order_id = ClientOrderId("C-001")
    instrument_id = InstrumentId(Symbol(f"{ticker}-{order_side_suffix}"), KALSHI_VENUE)
    return order, instrument_id


# ---------------------------------------------------------------------------
# Order translation tests
# ---------------------------------------------------------------------------

class TestOrderToKalshiParams:
    def test_buy_yes_at_32_cents(self):
        order, instrument_id = _make_order(OrderSide.BUY, "KXHIGHCHI-26MAR14-T55", "YES", 0.32)
        params = _order_to_kalshi_params(order, instrument_id)
        assert params["action"] == "buy"
        assert params["side"] == "yes"
        assert params["yes_price"] == 32
        assert "no_price" not in params

    def test_buy_no_at_68_cents(self):
        order, instrument_id = _make_order(OrderSide.BUY, "KXHIGHCHI-26MAR14-T55", "NO", 0.68)
        params = _order_to_kalshi_params(order, instrument_id)
        assert params["action"] == "buy"
        assert params["side"] == "no"
        assert params["no_price"] == 68
        assert "yes_price" not in params

    def test_sell_yes_at_95_cents(self):
        order, instrument_id = _make_order(OrderSide.SELL, "KXHIGHCHI-26MAR14-T55", "YES", 0.95)
        params = _order_to_kalshi_params(order, instrument_id)
        assert params["action"] == "sell"
        assert params["side"] == "yes"
        assert params["yes_price"] == 95

    def test_sell_no_at_90_cents(self):
        order, instrument_id = _make_order(OrderSide.SELL, "KXHIGHCHI-26MAR14-T55", "NO", 0.90)
        params = _order_to_kalshi_params(order, instrument_id)
        assert params["action"] == "sell"
        assert params["side"] == "no"
        assert params["no_price"] == 90

    def test_gtc_uses_full_string(self):
        """Correction #8: time_in_force must be 'good_till_canceled' not 'gtc'."""
        order, instrument_id = _make_order(OrderSide.BUY, "KXHIGHCHI-26MAR14-T55", "YES", 0.32, tif=TimeInForce.GTC)
        params = _order_to_kalshi_params(order, instrument_id)
        assert params["time_in_force"] == "good_till_canceled"

    def test_fok_uses_full_string(self):
        """Correction #8: time_in_force must be 'fill_or_kill' not 'fok'."""
        order, instrument_id = _make_order(OrderSide.BUY, "KXHIGHCHI-26MAR14-T55", "YES", 0.32, tif=TimeInForce.FOK)
        params = _order_to_kalshi_params(order, instrument_id)
        assert params["time_in_force"] == "fill_or_kill"

    def test_count_matches_quantity(self):
        order, instrument_id = _make_order(OrderSide.BUY, "KXHIGHCHI-26MAR14-T55", "YES", 0.32, qty=10)
        params = _order_to_kalshi_params(order, instrument_id)
        assert params["count"] == 10

    def test_ticker_extracted_from_instrument_id(self):
        order, instrument_id = _make_order(OrderSide.BUY, "KXHIGHCHI-26MAR14-T55", "YES", 0.32)
        params = _order_to_kalshi_params(order, instrument_id)
        assert params["ticker"] == "KXHIGHCHI-26MAR14-T55"


# ---------------------------------------------------------------------------
# Fill commission parsing tests
# ---------------------------------------------------------------------------

class TestParseFillCommission:
    def test_fee_cost_parsed_as_money(self):
        """Correction: fee_cost from FillMsg → Money (not hardcoded 0)."""
        commission = _parse_fill_commission("0.0056")
        assert isinstance(commission, Money)
        assert abs(float(commission) - 0.0056) < 1e-9

    def test_none_fee_cost_returns_zero(self):
        commission = _parse_fill_commission(None)
        assert float(commission) == 0.0

    def test_zero_fee_cost(self):
        commission = _parse_fill_commission("0.0000")
        assert float(commission) == 0.0


# ---------------------------------------------------------------------------
# Helpers shared by settlement + dedup tests
# ---------------------------------------------------------------------------

def _make_fill_stub():
    """Bind KalshiExecutionClient._handle_fill onto a plain Python SimpleNamespace.

    KalshiExecutionClient inherits from a Cython Component that makes attributes
    like _clock read-only descriptors. We cannot __new__ the class and assign to
    them in tests. Instead, we bind the pure-Python _handle_fill method onto a
    SimpleNamespace that has only the attributes the method actually needs.
    """
    import types as _types
    from kalshi.execution import KalshiExecutionClient

    # Mock cache: orders are looked up by _strategy_id_for for WS event routing.
    mock_order = MagicMock()
    mock_order.strategy_id = MagicMock()
    mock_cache = MagicMock()
    mock_cache.order.return_value = mock_order

    stub = _types.SimpleNamespace(
        _seen_trade_ids=OrderedDict(),
        _accepted_orders={},
        _instrument_provider=MagicMock(),
        _cache=mock_cache,
        _clock=MagicMock(timestamp_ns=MagicMock(return_value=1_000_000)),
        generate_order_accepted=MagicMock(),
        generate_order_filled=MagicMock(),
        generate_position_status_reports=AsyncMock(return_value=[]),
        generate_fill_reports=AsyncMock(return_value=[]),
    )
    stub._handle_fill = _types.MethodType(KalshiExecutionClient._handle_fill, stub)
    stub._strategy_id_for = _types.MethodType(KalshiExecutionClient._strategy_id_for, stub)
    return stub


def _mock_fill(trade_id: str, market_ticker: str | None = "KXHIGHCHI-26MAR14-T55") -> MagicMock:
    fill = MagicMock()
    fill.trade_id = trade_id
    fill.market_ticker = market_ticker
    fill.client_order_id = "C-001"
    fill.order_id = "O-001"
    fill.side = "yes"
    fill.yes_price_dollars = "0.50"
    fill.no_price_dollars = None
    fill.count_fp = "5"
    fill.fee_cost = "0.01"
    fill.is_taker = True
    fill.action = "buy"
    return fill


# ---------------------------------------------------------------------------
# Settlement fill → position reconciliation (Correction #4)
# ---------------------------------------------------------------------------

class TestSettlementFill:
    def test_settlement_fill_triggers_position_reconciliation(self):
        """Correction #4: Fill with no market_ticker → ensure_future(generate_position_status_reports)."""
        stub = _make_fill_stub()
        fill = _mock_fill("T-SETTLE", market_ticker=None)

        with patch("kalshi.execution.asyncio.create_task") as mock_future:
            stub._handle_fill(fill, 1_000_000)
            assert mock_future.call_count == 1

    def test_settlement_fill_does_not_emit_order_filled(self):
        """Settlement fills must not produce a fill event."""
        stub = _make_fill_stub()
        fill = _mock_fill("T-SETTLE", market_ticker=None)

        with patch("kalshi.execution.asyncio.create_task"):
            stub._handle_fill(fill, 1_000_000)

        stub.generate_order_filled.assert_not_called()


# ---------------------------------------------------------------------------
# Dedup cache (Correction #22)
# ---------------------------------------------------------------------------

class TestDedupCache:
    def _stub_with_instrument(self):
        stub = _make_fill_stub()
        instrument = MagicMock()
        instrument.make_qty = lambda x: x
        instrument.make_price = lambda x: x
        stub._instrument_provider.find.return_value = instrument
        return stub

    def test_duplicate_trade_id_skipped(self):
        """Correction #22: Same trade_id must not generate two fill events."""
        stub = self._stub_with_instrument()
        fill = _mock_fill("T-001")

        stub._handle_fill(fill, 1_000_000)
        stub._handle_fill(fill, 1_000_001)  # duplicate

        assert stub.generate_order_filled.call_count == 1

    def test_cache_evicts_oldest_at_10k(self):
        """Correction #22: After 10K entries, the oldest is evicted on the next insert."""
        stub = self._stub_with_instrument()

        # Pre-fill the cache to the max
        for i in range(_DEDUP_MAX):
            stub._seen_trade_ids[f"T-{i:06d}"] = None

        oldest = "T-000000"
        assert oldest in stub._seen_trade_ids

        fill = _mock_fill("T-NEW")
        stub._handle_fill(fill, 1_000_000)

        assert oldest not in stub._seen_trade_ids, "Oldest entry should have been evicted"
        assert "T-NEW" in stub._seen_trade_ids
