"""ConfidenceTestStrategy — minimal NautilusTrader strategy for mock exchange tests.

Behaviour:
  1. on_start: subscribe to QuoteTicks for the NO instrument.
  2. on_quote_tick (first tick): place one market BUY NO (FOK) + one limit BUY NO
     at (best_no_bid - 1c).
  3. on_quote_tick (subsequent ticks): if best NO bid has changed, cancel the
     outstanding limit order and repost at (new_no_bid - 1c).
  4. on_order_filled: track fills; signal done when ≥ 2 fills received.
  5. on_order_accepted / canceled / rejected: track events.

"Done" means the strategy has collected enough evidence (≥ 2 fills) for the
test runner to assert outcomes against the scenario expectations.
"""
from __future__ import annotations

import logging
from decimal import Decimal

from nautilus_trader.model.data import QuoteTick
from nautilus_trader.model.enums import OrderSide, TimeInForce
from nautilus_trader.model.identifiers import InstrumentId, Symbol
from nautilus_trader.model.objects import Price, Quantity
from nautilus_trader.trading.strategy import Strategy

from kalshi.common.constants import KALSHI_VENUE

log = logging.getLogger(__name__)


class ConfidenceTestStrategy(Strategy):
    """Minimal strategy that exercises the adapter's order lifecycle.

    Designed to be driven by mock exchange orderbook updates from the scenario
    runner (Task 8).  `done_callback` is invoked (with no args) once the
    strategy reaches terminal state.
    """

    def __init__(self, ticker: str, done_callback=None) -> None:
        super().__init__()
        self._ticker = ticker
        self._no_instrument_id = InstrumentId(Symbol(f"{ticker}-NO"), KALSHI_VENUE)
        self._done_callback = done_callback

        # Order state
        self._market_order_placed = False
        self._current_limit_client_oid = None   # ClientOrderId of live limit order
        self._last_no_bid_cents: int | None = None
        self._limit_order_count = 0             # total limit orders placed

        # Event tracking (populated by order callbacks)
        self.accepted_orders: list = []
        self.filled_orders: list = []
        self.canceled_orders: list = []
        self.rejected_orders: list = []
        self.quotes_received: list = []
        self.done = False

    # ------------------------------------------------------------------
    # NT lifecycle
    # ------------------------------------------------------------------

    def on_start(self) -> None:
        self.subscribe_quote_ticks(self._no_instrument_id)

    def on_stop(self) -> None:
        self.unsubscribe_quote_ticks(self._no_instrument_id)

    # ------------------------------------------------------------------
    # Market data
    # ------------------------------------------------------------------

    def on_quote_tick(self, tick: QuoteTick) -> None:
        if self.done:
            return

        self.quotes_received.append(tick)

        # bid_price is the best NO bid in dollars (e.g. 0.35 = 35c)
        no_bid_cents = round(float(tick.bid_price) * 100)

        if not self._market_order_placed:
            # First tick: place market + first limit
            self._place_market_order()
            self._place_limit_order(no_bid_cents - 1)
            self._last_no_bid_cents = no_bid_cents
        elif no_bid_cents != self._last_no_bid_cents:
            # Bid moved: cancel existing limit and repost 1c below new bid
            self._cancel_and_replace_limit(no_bid_cents - 1)
            self._last_no_bid_cents = no_bid_cents

    # ------------------------------------------------------------------
    # Order placement helpers
    # ------------------------------------------------------------------

    def _place_market_order(self) -> None:
        order = self.order_factory.market(
            instrument_id=self._no_instrument_id,
            order_side=OrderSide.BUY,
            quantity=Quantity.from_int(1),
            time_in_force=TimeInForce.FOK,
        )
        self.submit_order(order)
        self._market_order_placed = True
        log.debug(f"[strategy] market order submitted: {order.client_order_id}")

    def _place_limit_order(self, price_cents: int) -> None:
        price_cents = max(1, min(99, price_cents))  # clamp to valid range
        price = Price(Decimal(str(price_cents)) / Decimal("100"), precision=2)
        order = self.order_factory.limit(
            instrument_id=self._no_instrument_id,
            order_side=OrderSide.BUY,
            quantity=Quantity.from_int(1),
            price=price,
            time_in_force=TimeInForce.GTC,
        )
        self.submit_order(order)
        self._current_limit_client_oid = order.client_order_id
        self._limit_order_count += 1
        log.debug(f"[strategy] limit order submitted at {price_cents}c: {order.client_order_id}")

    def _cancel_and_replace_limit(self, new_price_cents: int) -> None:
        if self._current_limit_client_oid is not None:
            order = self.cache.order(self._current_limit_client_oid)
            if order is not None and order.is_open:
                self.cancel_order(order)
                log.debug(f"[strategy] canceling limit: {self._current_limit_client_oid}")
        self._place_limit_order(new_price_cents)

    # ------------------------------------------------------------------
    # Order event callbacks
    # ------------------------------------------------------------------

    def on_order_accepted(self, event) -> None:
        self.accepted_orders.append(event)

    def on_order_filled(self, event) -> None:
        self.filled_orders.append(event)
        log.debug(f"[strategy] fill received: {event.client_order_id}")
        if len(self.filled_orders) >= 2 and not self.done:
            self._signal_done()

    def on_order_canceled(self, event) -> None:
        self.canceled_orders.append(event)
        # Clear stale reference if it matches the canceled order
        if event.client_order_id == self._current_limit_client_oid:
            self._current_limit_client_oid = None

    def on_order_rejected(self, event) -> None:
        self.rejected_orders.append(event)
        log.warning(f"[strategy] order rejected: {event.client_order_id} — {event.reason}")

    # ------------------------------------------------------------------
    # Terminal state
    # ------------------------------------------------------------------

    def _signal_done(self) -> None:
        self.done = True
        log.info(f"[strategy] done — {len(self.filled_orders)} fills, "
                 f"{len(self.canceled_orders)} cancels")
        if self._done_callback is not None:
            self._done_callback()
