from nautilus_trader.model.data import QuoteTick
from nautilus_trader.model.events import OrderFilled, OrderCanceled, OrderRejected, OrderExpired
from nautilus_trader.model.identifiers import InstrumentId
from nautilus_trader.model.enums import OrderSide, TimeInForce, OrderStatus
from nautilus_trader.trading.strategy import Strategy, StrategyConfig


class KalshiSpreadCaptureConfig(StrategyConfig):
    instrument_id: str
    trade_size: int = 1
    max_position: int = 10
    target_spread_cents: int = 5


class KalshiSpreadCaptureStrategy(Strategy):
    """
    A mechanically sound sample strategy that acts as a naive market maker.
    It waits for a quote tick, then submits limit orders on both sides of the book
    if the spread is wide enough, attempting to capture the spread.

    This strategy assumes the adapter exposes Kalshi "Yes" and "No" contracts
    as separate Nautilus instruments, meaning we simply Buy/Sell the target instrument.
    """

    def __init__(self, config: KalshiSpreadCaptureConfig):
        super().__init__(config)
        self.instr_id_str = config.instrument_id
        self.trade_size = config.trade_size
        self.max_position = config.max_position
        self.target_spread_cents = config.target_spread_cents

        self.instrument_id = None
        self.instrument = None
        self.active_buy_order = None
        self.active_sell_order = None

    @property
    def current_position(self) -> int:
        pos = self.cache.position(self.instrument_id)
        if pos is None:
            return 0
        return int(pos.quantity.as_double()) if pos.is_long else -int(pos.quantity.as_double())

    def on_start(self):
        self.instrument_id = InstrumentId.from_str(self.instr_id_str)
        self.instrument = self.cache.instrument(self.instrument_id)

        if self.instrument is None:
            self.log.error(
                f"Instrument {self.instrument_id} not found in cache. Strategy will not run."
            )
            return

        self.subscribe_quote_ticks(self.instrument_id)
        self.log.info(f"Started KalshiSpreadCaptureStrategy for {self.instrument_id}")

        # Demo: Immediately place a bad trade to ensure execution
        qty = self.instrument.make_qty(self.trade_size)
        my_bid_price = self.instrument.make_price(0.99) # Very high buy price to cross spread
        buy_order = self.order_factory.limit(
            instrument_id=self.instrument_id,
            order_side=OrderSide.BUY,
            quantity=qty,
            price=my_bid_price,
            time_in_force=TimeInForce.GTC,
        )
        self.submit_order(buy_order)
        self.active_buy_order = buy_order.client_order_id
        self.log.info(f"Placed aggressive buy limit at {my_bid_price} for {self.instrument_id} in on_start")

    def on_quote_tick(self, tick: QuoteTick):
        # Use as_double() to compare raw prices, default to 0 if side is empty
        bid = tick.bid_price.as_double() if tick.bid_price else 0.0
        ask = tick.ask_price.as_double() if tick.ask_price else 0.0

        # Require a valid two-sided quote to calculate spread
        if bid == 0.0 or ask == 0.0:
            return

        spread = ask - bid

        # In a real market, price is bounded ($0.01 to $0.99)
        # We check if the spread in dollars is wider than our target spread in cents
        if spread >= (self.target_spread_cents / 100.0):
            # BAD TRADES: Cross the spread to guarantee a fill
            my_bid_price = self.instrument.make_price(ask)
            my_ask_price = self.instrument.make_price(bid)
            qty = self.instrument.make_qty(self.trade_size)

            # --- BUY SIDE ---
            if self.active_buy_order:
                order = self.cache.order(self.active_buy_order)
                if order and order.price != my_bid_price and order.status not in (OrderStatus.PENDING_CANCEL, OrderStatus.CANCELED, OrderStatus.FILLED):
                    # Cancel and replace if price moved
                    self.cancel_order(order)
                    self.active_buy_order = None
            else:
                if self.current_position < self.max_position:
                    buy_order = self.order_factory.limit(
                        instrument_id=self.instrument_id,
                        order_side=OrderSide.BUY,
                        quantity=qty,
                        price=my_bid_price,
                        time_in_force=TimeInForce.GTC,
                    )
                    self.submit_order(buy_order)
                    self.active_buy_order = buy_order.client_order_id
                    self.log.info(f"Placed buy limit at {my_bid_price} for {self.instrument_id}")

            # --- SELL SIDE ---
            if self.active_sell_order:
                order = self.cache.order(self.active_sell_order)
                if order and order.price != my_ask_price and order.status not in (OrderStatus.PENDING_CANCEL, OrderStatus.CANCELED, OrderStatus.FILLED):
                    # Cancel and replace if price moved
                    self.cancel_order(order)
                    self.active_sell_order = None
            else:
                if self.current_position > 0:
                    sell_order = self.order_factory.limit(
                        instrument_id=self.instrument_id,
                        order_side=OrderSide.SELL,
                        quantity=qty,
                        price=my_ask_price,
                        time_in_force=TimeInForce.GTC,
                    )
                    self.submit_order(sell_order)
                    self.active_sell_order = sell_order.client_order_id
                    self.log.info(f"Placed sell limit at {my_ask_price} for {self.instrument_id}")

    def on_order_filled(self, event: OrderFilled):
        self.log.info(f"Order filled: {event}")

        order = self.cache.order(event.client_order_id)
        # Handle partial fills by checking if the order is completely filled
        is_final = (order.status == OrderStatus.FILLED) if order else True

        if self.active_buy_order == event.client_order_id:
            if is_final:
                self.active_buy_order = None
            self.log.info(f"Buy order filled. Current position: {self.current_position}")
        elif self.active_sell_order == event.client_order_id:
            if is_final:
                self.active_sell_order = None
            self.log.info(f"Sell order filled. Current position: {self.current_position}")

    def on_order_canceled(self, event: OrderCanceled):
        self.log.info(f"Order canceled: {event}")
        if self.active_buy_order == event.client_order_id:
            self.active_buy_order = None
        elif self.active_sell_order == event.client_order_id:
            self.active_sell_order = None

    def on_order_rejected(self, event: OrderRejected):
        self.log.error(f"Order rejected: {event}")
        if self.active_buy_order == event.client_order_id:
            self.active_buy_order = None
        elif self.active_sell_order == event.client_order_id:
            self.active_sell_order = None

    def on_order_expired(self, event: OrderExpired):
        self.log.info(f"Order expired: {event}")
        if self.active_buy_order == event.client_order_id:
            self.active_buy_order = None
        elif self.active_sell_order == event.client_order_id:
            self.active_sell_order = None

    def on_stop(self):
        self.log.info("Stopped KalshiSpreadCaptureStrategy. Canceling all active orders.")
        if self.active_buy_order:
            order = self.cache.order(self.active_buy_order)
            if order and order.status not in (OrderStatus.CANCELED, OrderStatus.FILLED, OrderStatus.PENDING_CANCEL):
                self.cancel_order(order)
        if self.active_sell_order:
            order = self.cache.order(self.active_sell_order)
            if order and order.status not in (OrderStatus.CANCELED, OrderStatus.FILLED, OrderStatus.PENDING_CANCEL):
                self.cancel_order(order)
