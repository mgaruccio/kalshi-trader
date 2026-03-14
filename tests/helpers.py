"""Shared test helpers for Kalshi adapter tests.

Importable directly (no conftest needed — respects --noconftest).
"""
from unittest.mock import MagicMock

from nautilus_trader.model.enums import OrderSide, OrderType, TimeInForce
from nautilus_trader.model.identifiers import ClientOrderId, InstrumentId, Symbol
from nautilus_trader.model.objects import Price, Quantity

from kalshi.common.constants import KALSHI_VENUE


def make_mock_order(
    side: OrderSide,
    price_cents: int,
    qty: int = 5,
    tif: TimeInForce = TimeInForce.GTC,
    order_type: OrderType = OrderType.LIMIT,
    client_order_id: str = "C-TEST",
) -> MagicMock:
    """Build a minimal mock order. Price in cents (1-99)."""
    order = MagicMock()
    order.side = side
    order.order_type = order_type
    order.time_in_force = tif
    order.client_order_id = ClientOrderId(client_order_id)
    if order_type == OrderType.LIMIT and price_cents is not None:
        order.price = Price.from_str(f"{price_cents / 100:.2f}")
    else:
        order.price = None
    order.quantity = Quantity.from_int(qty)
    return order


def make_instrument_id(ticker: str = "KXHIGHCHI-26MAR14-T55", side: str = "YES") -> InstrumentId:
    """Build an InstrumentId for testing."""
    return InstrumentId(Symbol(f"{ticker}-{side}"), KALSHI_VENUE)
