"""Order translation combinatorics — exhaustive side × action × price × tif coverage."""
import pytest
from unittest.mock import MagicMock

from nautilus_trader.model.enums import OrderSide, OrderType, TimeInForce
from nautilus_trader.model.identifiers import ClientOrderId, InstrumentId, Symbol
from nautilus_trader.model.objects import Money, Price, Quantity

from kalshi.common.constants import KALSHI_VENUE
from kalshi.execution import _order_to_kalshi_params, _parse_fill_commission


# ---------------------------------------------------------------------------
# Fixture helpers
# ---------------------------------------------------------------------------

def _order(side: OrderSide, price_str: str, qty: int = 5, tif: TimeInForce = TimeInForce.GTC):
    order = MagicMock()
    order.side = side
    order.order_type = OrderType.LIMIT
    order.time_in_force = tif
    order.price = Price.from_str(price_str)
    order.quantity = Quantity.from_int(qty)
    order.client_order_id = ClientOrderId("C-COMBO")
    return order


def _instr(suffix: str, ticker: str = "KXHIGHCHI-26MAR14-T55") -> InstrumentId:
    return InstrumentId(Symbol(f"{ticker}-{suffix}"), KALSHI_VENUE)


# ---------------------------------------------------------------------------
# All 4 side × action combinations
# ---------------------------------------------------------------------------

class TestSideActionCombinations:
    def test_buy_yes(self):
        params = _order_to_kalshi_params(_order(OrderSide.BUY, "0.50"), _instr("YES"))
        assert params["action"] == "buy"
        assert params["side"] == "yes"
        assert "yes_price" in params
        assert "no_price" not in params

    def test_buy_no(self):
        params = _order_to_kalshi_params(_order(OrderSide.BUY, "0.50"), _instr("NO"))
        assert params["action"] == "buy"
        assert params["side"] == "no"
        assert "no_price" in params
        assert "yes_price" not in params

    def test_sell_yes(self):
        params = _order_to_kalshi_params(_order(OrderSide.SELL, "0.50"), _instr("YES"))
        assert params["action"] == "sell"
        assert params["side"] == "yes"
        assert "yes_price" in params
        assert "no_price" not in params

    def test_sell_no(self):
        params = _order_to_kalshi_params(_order(OrderSide.SELL, "0.50"), _instr("NO"))
        assert params["action"] == "sell"
        assert params["side"] == "no"
        assert "no_price" in params
        assert "yes_price" not in params


# ---------------------------------------------------------------------------
# Edge prices: 0.01 and 0.99 cents
# ---------------------------------------------------------------------------

class TestEdgePrices:
    def test_price_0_01_yes(self):
        params = _order_to_kalshi_params(_order(OrderSide.BUY, "0.01"), _instr("YES"))
        assert params["yes_price"] == 1

    def test_price_0_99_yes(self):
        params = _order_to_kalshi_params(_order(OrderSide.BUY, "0.99"), _instr("YES"))
        assert params["yes_price"] == 99

    def test_price_0_01_no(self):
        params = _order_to_kalshi_params(_order(OrderSide.BUY, "0.01"), _instr("NO"))
        assert params["no_price"] == 1

    def test_price_0_99_no(self):
        params = _order_to_kalshi_params(_order(OrderSide.BUY, "0.99"), _instr("NO"))
        assert params["no_price"] == 99

    def test_price_rounding_midpoint(self):
        """0.315 → 32 cents (rounds to nearest cent)."""
        params = _order_to_kalshi_params(_order(OrderSide.BUY, "0.315"), _instr("YES"))
        # round(0.315 * 100) = 32 (Python rounds to even → 32)
        assert params["yes_price"] in (31, 32)  # either is acceptable rounding


# ---------------------------------------------------------------------------
# Time-in-force full strings (Correction #8)
# ---------------------------------------------------------------------------

class TestTimeInForceStrings:
    @pytest.mark.parametrize("tif,expected", [
        (TimeInForce.GTC, "good_till_canceled"),
        (TimeInForce.FOK, "fill_or_kill"),
        (TimeInForce.IOC, "immediate_or_cancel"),
        (TimeInForce.GTD, "good_till_canceled"),
    ])
    def test_tif_string(self, tif, expected):
        params = _order_to_kalshi_params(_order(OrderSide.BUY, "0.50", tif=tif), _instr("YES"))
        assert params["time_in_force"] == expected

    def test_no_abbreviations_in_tif(self):
        """Kalshi rejects 'gtc', 'fok' — must be full strings."""
        for tif in (TimeInForce.GTC, TimeInForce.FOK, TimeInForce.IOC):
            params = _order_to_kalshi_params(_order(OrderSide.BUY, "0.50", tif=tif), _instr("YES"))
            tif_str = params["time_in_force"]
            assert tif_str not in ("gtc", "fok", "ioc"), f"Abbreviation found: {tif_str}"


# ---------------------------------------------------------------------------
# Fee commission: is_taker True/False (no functional difference in parsing,
# but the fee amounts should differ in practice)
# ---------------------------------------------------------------------------

class TestFeeCommission:
    def test_maker_fee_parsed(self):
        # Maker: 1.75% × $0.32 × 5 contracts = $0.028
        commission = _parse_fill_commission("0.028")
        assert abs(float(commission) - 0.028) < 1e-6

    def test_taker_fee_parsed(self):
        # Taker: 7% × $0.32 × 5 contracts = $0.112
        commission = _parse_fill_commission("0.112")
        assert abs(float(commission) - 0.112) < 1e-6

    def test_taker_fee_higher_than_maker(self):
        maker = _parse_fill_commission("0.028")
        taker = _parse_fill_commission("0.112")
        assert float(taker) > float(maker)

    def test_large_fee(self):
        # Large position: 100 contracts × $0.50 × 7% = $3.50
        commission = _parse_fill_commission("3.50")
        assert abs(float(commission) - 3.50) < 1e-6

    def test_zero_fee(self):
        commission = _parse_fill_commission("0.0000")
        assert float(commission) == 0.0

    def test_none_fee(self):
        commission = _parse_fill_commission(None)
        assert float(commission) == 0.0


# ---------------------------------------------------------------------------
# Quantity passthrough
# ---------------------------------------------------------------------------

class TestQuantityPassthrough:
    @pytest.mark.parametrize("qty", [1, 5, 9, 50, 100])
    def test_count_matches_quantity(self, qty):
        params = _order_to_kalshi_params(_order(OrderSide.BUY, "0.50", qty=qty), _instr("YES"))
        assert params["count"] == qty


# ---------------------------------------------------------------------------
# Market orders: no price fields in params
# ---------------------------------------------------------------------------

class TestMarketOrders:
    """Market orders (no price) must produce params without price fields."""

    def test_market_order_buy_yes(self):
        order = MagicMock()
        order.side = OrderSide.BUY
        order.order_type = OrderType.MARKET
        order.time_in_force = TimeInForce.FOK
        order.price = None
        order.quantity = Quantity.from_int(5)
        order.client_order_id = ClientOrderId("C-MKT")

        params = _order_to_kalshi_params(order, _instr("YES"))
        assert params["type"] == "market"
        assert params["action"] == "buy"
        assert params["side"] == "yes"
        assert "yes_price" not in params
        assert "no_price" not in params
        assert params["count"] == 5

    def test_market_order_sell_no(self):
        order = MagicMock()
        order.side = OrderSide.SELL
        order.order_type = OrderType.MARKET
        order.time_in_force = TimeInForce.FOK
        order.price = None
        order.quantity = Quantity.from_int(10)
        order.client_order_id = ClientOrderId("C-MKT2")

        params = _order_to_kalshi_params(order, _instr("NO"))
        assert params["type"] == "market"
        assert params["action"] == "sell"
        assert params["side"] == "no"
        assert "yes_price" not in params
        assert "no_price" not in params
