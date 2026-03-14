"""Tests for KalshiExecutionClient — order translation and fill processing."""
import pytest
from unittest.mock import MagicMock

from nautilus_trader.model.enums import OrderSide, OrderType, TimeInForce
from nautilus_trader.model.identifiers import (
    ClientOrderId, InstrumentId, Symbol, VenueOrderId,
)
from nautilus_trader.model.objects import Money, Price, Quantity
from nautilus_trader.model.currencies import USD

from kalshi.common.constants import KALSHI_VENUE
from kalshi.execution import _order_to_kalshi_params, _parse_fill_commission


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

    def test_ioc_uses_full_string(self):
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
