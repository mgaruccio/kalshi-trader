"""Weather trading strategy for NautilusTrader.

2-phase market-making strategy with laddered GTC orders:

Phase 1 — Open Spread:
  For contracts settling tomorrow, place a spread of GTC buy orders at fixed
  price levels immediately on first ModelSignal, within an opening window.
  Unfilled orders are cancelled when the window expires.

Phase 2 — Stable Ladder:
  For today contracts (or tomorrow after the window), place GTC buy orders
  laddered below the current bid. On fill, immediately post a resting GTC sell
  at sell_target_cents. A periodic timer refreshes the ladder each cycle.

Reacts to:
- ModelSignal: evaluate potential entries (Phase 1 or Phase 2 ladder)
- DangerAlert: evaluate exits (sell when CRITICAL corroboration)
- QuoteTick: track prices for ladder pricing
"""
import logging
from datetime import date, timedelta, datetime, timezone

from nautilus_trader.model.data import QuoteTick, DataType
from nautilus_trader.model.events import OrderFilled
from nautilus_trader.model.identifiers import InstrumentId, Symbol
from nautilus_trader.model.enums import OrderSide, TimeInForce
from nautilus_trader.trading.strategy import Strategy, StrategyConfig

from kalshi_weather_ml.markets import parse_ticker, SERIES_CONFIG

from adapter import KALSHI_VENUE
from data_types import ModelSignal, DangerAlert

log = logging.getLogger(__name__)


class WeatherStrategyConfig(StrategyConfig, frozen=True):
    # Phase 1 — Open Spread
    open_spread_enabled: bool = True
    open_spread_prices_cents: tuple[int, ...] = (45, 50, 55)
    open_spread_size: int = 3                     # contracts per price level
    open_spread_min_p_win: float = 0.90
    open_spread_window_minutes: int = 30

    # Phase 2 — Stable Ladder
    stable_min_p_win: float = 0.95
    stable_ladder_offsets_cents: tuple[int, ...] = (0, 1, 3, 5, 10)  # below bid
    stable_size: int = 3                          # contracts per ladder level
    sell_target_cents: int = 97
    refresh_interval_minutes: int = 5             # periodic ladder refresh

    # Capital Conservation
    backoff_hour_utc: int = 7                     # 2 AM EST = 7 UTC
    resume_hour_utc: int = 14                     # 9 AM EST = 14 UTC

    # Cost ceiling
    max_cost_cents: int = 92              # never buy above this price

    # Portfolio-level capital guard
    max_total_deployed_cents: int = 0     # 0 = disabled; e.g. 2500 = $25 cap

    # Global (unchanged)
    max_position_per_ticker: int = 20
    danger_exit_enabled: bool = True


class WeatherStrategy(Strategy):
    """Event-driven weather trading strategy with 2-phase market-making.

    Phase 1 (Open Spread): For tomorrow contracts in the opening window,
    place a laddered spread at fixed prices.

    Phase 2 (Stable Ladder): For today contracts or tomorrow after window,
    place GTC buys laddered below current bid. Immediately post resting sell
    on each fill. Periodic refresh redeploys capital.

    2 AM back-off: Cancel all resting buys during the dead overnight window.
    """

    def __init__(self, config: WeatherStrategyConfig):
        super().__init__(config)
        self._cfg = config

        # Quote tracking
        self._latest_quotes: dict[str, QuoteTick] = {}  # instrument_id_str -> tick

        # Position tracking
        self._positions_info: dict[str, dict] = {}  # ticker -> {side, threshold, city, contracts}
        self._danger_exited: set[str] = set()  # tickers we've danger-exited (no retry)

        # Phase 1 state
        self._open_spread_placed: set[str] = set()  # tickers with spread deployed
        self._open_spread_orders: dict[str, list] = {}  # ticker -> list of ClientOrderId
        self._first_tick_time: dict[str, int] = {}  # ticker -> ns timestamp of first signal

        # Phase 2 state
        self._ladder_orders: dict[str, list] = {}  # ticker -> list of ClientOrderId
        self._resting_sells: dict[str, list] = {}  # ticker -> list of ClientOrderId
        self._eligible_signals: dict[str, ModelSignal] = {}  # ticker -> qualifying signal
        self._last_ladder_bid: dict[str, int] = {}  # ticker -> bid_cents used in last deployment

        # Counters
        self._signals_received: int = 0
        self._alerts_received: int = 0
        self._spread_orders_placed: int = 0
        self._stable_orders_placed: int = 0
        self._feature_actor = None  # set after construction

    def set_feature_actor(self, actor):
        """Wire the FeatureActor reference for position updates."""
        self._feature_actor = actor

    def on_start(self):
        """Subscribe to ModelSignal, DangerAlert, and quote ticks. Start refresh timer."""
        # Subscribe via msgbus directly — subscribe_data() requires client_id
        # which doesn't exist for internal pub/sub in live TradingNode.
        for dtype in (ModelSignal, DangerAlert):
            self.msgbus.subscribe(
                topic=f"data.{DataType(dtype).topic}",
                handler=self.handle_data,
            )

        # Subscribe to all cached instruments for quote ticks
        instruments = self.cache.instruments()
        for inst in instruments:
            self.subscribe_quote_ticks(inst.id)

        # Periodic ladder refresh timer
        self.clock.set_timer(
            name="ladder_refresh",
            interval=timedelta(minutes=self._cfg.refresh_interval_minutes),
            callback=self._on_refresh,
        )

        self.log.info(
            f"WeatherStrategy started, subscribed to {len(instruments)} instruments, "
            f"refresh interval={self._cfg.refresh_interval_minutes}m"
        )

    def on_data(self, data):
        """Route incoming data to appropriate handler."""
        if isinstance(data, ModelSignal):
            self._signals_received += 1
            self._evaluate_entry(data)
        elif isinstance(data, DangerAlert):
            self._alerts_received += 1
            self._evaluate_exit(data)

    def on_quote_tick(self, tick: QuoteTick):
        """Track latest quotes for ladder pricing."""
        key = tick.instrument_id.value
        self._latest_quotes[key] = tick

    def _quote_key(self, ticker: str, side: str) -> str:
        """Build the canonical quote cache key matching on_quote_tick storage."""
        return f"{ticker}-{side.upper()}.KALSHI"

    def _utc_now(self) -> datetime:
        """Return current UTC datetime from the strategy clock."""
        dt = self.clock.utc_now()
        if dt.tzinfo is None:
            return dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)

    def _today(self) -> date:
        """Return today's date in UTC."""
        return self._utc_now().date()

    def _is_backoff_window(self) -> bool:
        """Return True if current UTC hour is in the overnight back-off window."""
        hour = self._utc_now().hour
        bk = self._cfg.backoff_hour_utc
        rv = self._cfg.resume_hour_utc
        if bk < rv:
            return bk <= hour < rv
        else:
            # Wraps midnight
            return hour >= bk or hour < rv

    def _evaluate_entry(self, signal: ModelSignal):
        """Evaluate a ModelSignal for potential entry (Phase 1 or Phase 2)."""
        # NO-only strategy — reject YES signals (defense-in-depth)
        if signal.side != "no":
            return

        if signal.ticker in self._danger_exited:
            self.log.info(f"Skip {signal.ticker}: danger-exited")
            return

        # Determine settlement date from ticker
        parsed = parse_ticker(signal.ticker)
        if parsed is None:
            self.log.warning(f"Cannot parse ticker {signal.ticker}, skipping")
            return

        try:
            settlement_date = date.fromisoformat(parsed["settlement_date"])
        except (KeyError, ValueError) as e:
            self.log.warning(f"Cannot parse settlement_date from {signal.ticker}: {e}")
            return

        today = self._today()
        is_tomorrow = settlement_date > today

        # Check position capacity
        existing = self._positions_info.get(signal.ticker, {})
        current_contracts = existing.get("contracts", 0)
        if current_contracts >= self._cfg.max_position_per_ticker:
            self.log.info(f"Skip {signal.ticker}: position full ({current_contracts})")
            return

        # Phase 1: Tomorrow contracts in opening window
        if (
            is_tomorrow
            and self._cfg.open_spread_enabled
            and signal.ticker not in self._open_spread_placed
            and signal.p_win >= self._cfg.open_spread_min_p_win
        ):
            # Record first tick time for this ticker
            if signal.ticker not in self._first_tick_time:
                self._first_tick_time[signal.ticker] = signal.ts_event

            # Check we're still within the opening window
            elapsed_ns = signal.ts_event - self._first_tick_time[signal.ticker]
            window_ns = self._cfg.open_spread_window_minutes * 60 * 1_000_000_000
            if elapsed_ns <= window_ns:
                self._deploy_open_spread(signal)
                return
            # Window expired — fall through to Phase 2

        # Guard: Phase 1 spread still active — suppress Phase 2 until timer cancels
        if signal.ticker in self._open_spread_placed and signal.ticker in self._open_spread_orders:
            return

        # Phase 2: Stable ladder
        if signal.p_win < self._cfg.stable_min_p_win:
            self.log.info(
                f"Skip {signal.ticker}: p_win={signal.p_win:.3f} < {self._cfg.stable_min_p_win}"
            )
            return

        # Store qualifying signal for periodic refresh (deployment on _on_refresh)
        self._eligible_signals[signal.ticker] = signal

    def _deploy_open_spread(self, signal: ModelSignal):
        """Place Phase 1 spread orders at fixed price levels."""
        instrument_id = InstrumentId(
            Symbol(f"{signal.ticker}-{signal.side.upper()}"),
            KALSHI_VENUE,
        )
        instrument = self.cache.instrument(instrument_id)
        if instrument is None:
            self.log.warning(f"Instrument {instrument_id} not in cache, skipping Phase 1")
            return

        # Capital cap check
        if self._cfg.max_total_deployed_cents > 0:
            at_risk = self._total_capital_at_risk()
            spread_cost = sum(self._cfg.open_spread_prices_cents) * self._cfg.open_spread_size
            if at_risk + spread_cost > self._cfg.max_total_deployed_cents:
                self.log.info(
                    f"Skip Phase1 {signal.ticker}: capital cap "
                    f"({at_risk}c + {spread_cost}c > {self._cfg.max_total_deployed_cents}c)"
                )
                return

        order_ids = []
        for price_cents in self._cfg.open_spread_prices_cents:
            price = instrument.make_price(price_cents / 100.0)
            qty = instrument.make_qty(self._cfg.open_spread_size)
            order = self.order_factory.limit(
                instrument_id=instrument_id,
                order_side=OrderSide.BUY,
                quantity=qty,
                price=price,
                time_in_force=TimeInForce.GTC,
            )
            self.submit_order(order)
            order_ids.append(order.client_order_id)
            self._spread_orders_placed += 1
            self.log.info(
                f"Phase1 spread: {signal.ticker} {signal.side} "
                f"p_win={signal.p_win:.3f} price={price_cents}c qty={self._cfg.open_spread_size}"
            )

        self._open_spread_placed.add(signal.ticker)
        self._open_spread_orders[signal.ticker] = order_ids

        # Set one-shot timer to cancel unfilled spread orders after window expires
        window_ns = self._cfg.open_spread_window_minutes * 60 * 1_000_000_000
        cancel_time_ns = self.clock.timestamp_ns() + window_ns
        timer_name = f"spread_cancel_{signal.ticker}"
        self.clock.set_time_alert_ns(
            name=timer_name,
            alert_time_ns=cancel_time_ns,
            callback=self._make_spread_cancel_callback(signal.ticker),
        )

    def _make_spread_cancel_callback(self, ticker: str):
        """Return a callback that cancels unfilled spread orders for a ticker."""
        def _cancel(event=None):
            self._cancel_spread_orders(ticker)
        return _cancel

    def _cancel_spread_orders(self, ticker: str):
        """Cancel all outstanding Phase 1 spread buy orders for a ticker."""
        order_ids = set(self._open_spread_orders.get(ticker, []))
        open_orders = self.cache.orders_open(strategy_id=self.id)
        for order in open_orders:
            if order.client_order_id in order_ids and order.side == OrderSide.BUY:
                self.cancel_order(order)
                self.log.info(f"Phase1 spread cancel: {ticker} order {order.client_order_id}")
        # Pop AFTER cancellation attempts
        self._open_spread_orders.pop(ticker, None)

    def _deploy_ladder(self, ticker: str, signal: ModelSignal,
                       budget_remaining: int | None = None) -> int:
        """Place Phase 2 ladder buy orders relative to current bid.

        Args:
            budget_remaining: If provided, use this instead of querying
                cache.orders_open() for capital/capacity checks. Used by
                _on_refresh to avoid counting async-cancelled orders.

        Returns:
            Total cents deployed (for caller budget tracking).
        """
        quote_key = self._quote_key(ticker, signal.side)
        quote = self._latest_quotes.get(quote_key)
        if quote is None:
            self.log.warning(f"No quote for {quote_key}, skipping Phase 2 ladder")
            return 0

        bid_cents = int(round(quote.bid_price.as_double() * 100))

        instrument_id = InstrumentId(
            Symbol(f"{ticker}-{signal.side.upper()}"),
            KALSHI_VENUE,
        )
        instrument = self.cache.instrument(instrument_id)
        if instrument is None:
            self.log.warning(f"Instrument {instrument_id} not in cache, skipping Phase 2")
            return 0

        # Check remaining capacity
        existing = self._positions_info.get(ticker, {})
        current_contracts = existing.get("contracts", 0)
        capacity = self._cfg.max_position_per_ticker - current_contracts

        if budget_remaining is None:
            # Standalone call — query cache for pending orders
            pending = self._count_pending_buys(instrument_id)
            capacity -= pending
        # else: rebalance mode — all buys just cancelled, skip cache query

        if capacity <= 0:
            self.log.info(f"Skip ladder {ticker}: no capacity (pos={current_contracts})")
            return 0

        # Capital cap
        if self._cfg.max_total_deployed_cents > 0:
            if budget_remaining is not None:
                remaining_budget = budget_remaining
            else:
                at_risk = self._total_capital_at_risk()
                remaining_budget = self._cfg.max_total_deployed_cents - at_risk
            if remaining_budget <= 0:
                self.log.info(
                    f"Skip ladder {ticker}: capital cap reached (budget={remaining_budget}c)"
                )
                return 0
        else:
            remaining_budget = float('inf')

        total_deployed = 0
        order_ids = []
        for offset in self._cfg.stable_ladder_offsets_cents:
            price_cents = max(1, bid_cents - offset)
            # Cost ceiling: never buy above max_cost_cents
            if price_cents > self._cfg.max_cost_cents:
                continue
            price = instrument.make_price(price_cents / 100.0)
            size = min(self._cfg.stable_size, max(0, capacity))
            if size <= 0:
                break
            order_cost = price_cents * size
            if order_cost > remaining_budget:
                self.log.info(
                    f"Ladder cap: {ticker} budget={remaining_budget}c < order={order_cost}c"
                )
                break
            qty = instrument.make_qty(size)
            order = self.order_factory.limit(
                instrument_id=instrument_id,
                order_side=OrderSide.BUY,
                quantity=qty,
                price=price,
                time_in_force=TimeInForce.GTC,
            )
            self.submit_order(order)
            order_ids.append(order.client_order_id)
            self._stable_orders_placed += 1
            self.log.info(
                f"Phase2 ladder: {ticker} {signal.side} "
                f"p_win={signal.p_win:.3f} bid={bid_cents}c offset={offset}c price={price_cents}c qty={size}"
            )
            capacity -= size
            remaining_budget -= order_cost
            total_deployed += order_cost

        if order_ids:
            self._ladder_orders.setdefault(ticker, []).extend(order_ids)
            self._last_ladder_bid[ticker] = bid_cents

        return total_deployed

    def _total_capital_at_risk(self) -> int:
        """Conservative estimate of deployed capital in cents.

        Sums:
        - Pending buy orders: price_cents * leaves_qty for all open BUY orders
        - Held positions: contracts * max_cost_cents (conservative, since we
          don't track actual fill prices)
        """
        total = 0
        # Pending buy orders
        open_orders = self.cache.orders_open(strategy_id=self.id)
        for order in open_orders:
            if order.side == OrderSide.BUY:
                price_cents = int(round(order.price.as_double() * 100))
                leaves = int(order.leaves_qty.as_double())
                total += price_cents * leaves
        # Held positions
        for info in self._positions_info.values():
            total += info.get("contracts", 0) * self._cfg.max_cost_cents
        return total

    def _count_pending_buys(self, instrument_id: InstrumentId) -> int:
        """Count contracts in outstanding BUY orders for an instrument."""
        total = 0
        target_value = instrument_id.value
        open_orders = self.cache.orders_open(strategy_id=self.id)
        for order in open_orders:
            if order.instrument_id.value == target_value and order.side == OrderSide.BUY:
                total += int(order.leaves_qty.as_double())
        return total

    def _evaluate_exit(self, alert: DangerAlert):
        """Evaluate a DangerAlert for potential exit."""
        if not self._cfg.danger_exit_enabled:
            return

        if alert.alert_level != "CRITICAL":
            self.log.info(f"DangerAlert {alert.alert_level} for {alert.ticker}: {alert.reason}")
            return

        if alert.ticker in self._danger_exited:
            self.log.debug(f"Already danger-exited {alert.ticker}, ignoring alert")
            return

        pos_info = self._positions_info.get(alert.ticker)
        if pos_info is None:
            self.log.debug(f"No position for {alert.ticker}, ignoring CRITICAL alert")
            return

        self.log.warning(
            f"DANGER EXIT: {alert.ticker} rule={alert.rule_name} "
            f"reason={alert.reason}"
        )

        # Mark as danger-exited BEFORE attempting sell (prevents retry loops)
        self._danger_exited.add(alert.ticker)

        # Cancel all resting buy orders for this ticker first
        self._cancel_all_buys_for_ticker(alert.ticker)

        # Cancel resting sells to avoid duplicate sell orders
        self._cancel_resting_sells_for_ticker(alert.ticker)

        # Submit sell order for existing position
        side = pos_info.get("side", "no").upper()
        instrument_id = InstrumentId(
            Symbol(f"{alert.ticker}-{side}"),
            KALSHI_VENUE,
        )
        instrument = self.cache.instrument(instrument_id)
        if instrument is None:
            self.log.error(
                f"EXIT FAILED: instrument {instrument_id} not in cache — position stays open"
            )
            return

        contracts = pos_info.get("contracts", 1)
        # Aggressive sell: cross the spread by 5c to ensure fill
        quote_key = self._quote_key(alert.ticker, side)
        quote = self._latest_quotes.get(quote_key)
        if quote is not None:
            bid_cents = int(round(quote.bid_price.as_double() * 100))
            price = instrument.make_price(max(bid_cents - 5, 1) / 100.0)
        else:
            # Fallback: sell at 1c (will match any buyer)
            price = instrument.make_price(0.01)

        # NOTE: reduce_only=True is ignored by the Kalshi adapter (API doesn't
        # support it). Fix #1 (cancelling resting sells first) prevents the
        # scenario where reduce_only would matter.
        order = self.order_factory.limit(
            instrument_id=instrument_id,
            order_side=OrderSide.SELL,
            quantity=instrument.make_qty(contracts),
            price=price,
            time_in_force=TimeInForce.GTC,
            reduce_only=True,
        )
        self.submit_order(order)

    def _cancel_all_buys_for_ticker(self, ticker: str):
        """Cancel all outstanding BUY orders (Phase 1 + Phase 2) for a ticker."""
        all_buy_ids = set(
            self._open_spread_orders.get(ticker, [])
            + self._ladder_orders.get(ticker, [])
        )
        open_orders = self.cache.orders_open(strategy_id=self.id)
        for order in open_orders:
            if (
                order.client_order_id in all_buy_ids
                and order.side == OrderSide.BUY
            ):
                self.cancel_order(order)
        # Pop AFTER cancellation attempts
        self._open_spread_orders.pop(ticker, None)
        self._ladder_orders.pop(ticker, None)
        self._last_ladder_bid.pop(ticker, None)

    def _cancel_resting_sells_for_ticker(self, ticker: str):
        """Cancel all resting GTC sell orders for a ticker."""
        order_ids = set(self._resting_sells.get(ticker, []))
        open_orders = self.cache.orders_open(strategy_id=self.id)
        for order in open_orders:
            if order.client_order_id in order_ids and order.side == OrderSide.SELL:
                self.cancel_order(order)
                self.log.info(f"Resting sell cancel: {ticker} order {order.client_order_id}")
        self._resting_sells.pop(ticker, None)

    def _cleanup_ticker_state(self, ticker: str):
        """Remove all tracking state for a ticker when its position is fully closed."""
        self._resting_sells.pop(ticker, None)
        self._eligible_signals.pop(ticker, None)
        self._danger_exited.discard(ticker)
        self._open_spread_placed.discard(ticker)
        self._first_tick_time.pop(ticker, None)
        self._last_ladder_bid.pop(ticker, None)

    def on_order_filled(self, event: OrderFilled):
        """Track position changes on fills. On buy fill, place resting sell."""
        self.log.info(f"Order filled: {event}")

        # Extract ticker and side from instrument ID
        inst_str = event.instrument_id.symbol.value  # e.g. "KXHIGHCHI-26MAR01-T72-NO"
        if inst_str.endswith("-YES"):
            ticker = inst_str[:-4]
            side = "yes"
        elif inst_str.endswith("-NO"):
            ticker = inst_str[:-3]
            side = "no"
        else:
            self.log.warning(f"Could not parse ticker from {inst_str}")
            return

        # Ensure position info exists
        if ticker not in self._positions_info:
            parsed = parse_ticker(ticker)
            city = ""
            threshold = 0.0
            if parsed:
                threshold = parsed["threshold"]
                series_to_city = {s: c for s, c in SERIES_CONFIG}
                city = series_to_city.get(parsed["series"], "")

            self._positions_info[ticker] = {
                "side": side,
                "threshold": threshold,
                "city": city,
                "contracts": 0,
            }

        pos = self._positions_info[ticker]
        qty = int(event.last_qty.as_double())

        if event.order_side == OrderSide.BUY:
            pos["contracts"] += qty
            # Immediately place resting sell for filled quantity
            self._place_resting_sell(event.instrument_id, qty)
        else:
            pos["contracts"] -= qty

        # Clean up if position closed
        if pos["contracts"] <= 0:
            self._positions_info.pop(ticker, None)
            self._cleanup_ticker_state(ticker)

        # Sync with FeatureActor
        self._sync_positions_to_actor()

    def _place_resting_sell(self, instrument_id: InstrumentId, qty: int):
        """Place a resting GTC sell at sell_target_cents for the just-filled quantity."""
        instrument = self.cache.instrument(instrument_id)
        if instrument is None:
            self.log.warning(f"Cannot place resting sell: instrument {instrument_id} not in cache")
            return

        price = instrument.make_price(self._cfg.sell_target_cents / 100.0)
        order = self.order_factory.limit(
            instrument_id=instrument_id,
            order_side=OrderSide.SELL,
            quantity=instrument.make_qty(qty),
            price=price,
            time_in_force=TimeInForce.GTC,
        )
        self.submit_order(order)

        # Track resting sell
        inst_str = instrument_id.symbol.value
        if inst_str.endswith("-YES"):
            ticker = inst_str[:-4]
        elif inst_str.endswith("-NO"):
            ticker = inst_str[:-3]
        else:
            ticker = inst_str
        self._resting_sells.setdefault(ticker, []).append(order.client_order_id)
        self.log.info(
            f"Resting sell placed: {ticker} qty={qty} @ {self._cfg.sell_target_cents}c"
        )

    def _on_refresh(self, event=None):
        """Periodic global rebalance: cancel all → sort cheapest → redeploy.

        Ensures capital flows to the cheapest available contracts rather than
        being locked into whichever tickers were scored first.
        """
        if self._is_backoff_window():
            self.log.info("Back-off window: cancelling all resting buy orders")
            self._cancel_all_resting_buys()
            return

        # 1. Cancel ALL existing ladder buy orders
        for ticker in list(self._ladder_orders.keys()):
            self._cancel_ladder_orders(ticker)
        self._last_ladder_bid.clear()

        # 2. Collect candidates with current bids
        candidates = []
        for ticker, signal in list(self._eligible_signals.items()):
            if ticker in self._danger_exited:
                continue
            quote_key = self._quote_key(ticker, signal.side)
            quote = self._latest_quotes.get(quote_key)
            if quote is None:
                continue
            bid_cents = int(round(quote.bid_price.as_double() * 100))
            candidates.append((bid_cents, ticker, signal))

        # 3. Sort cheapest first — capital flows to best-value contracts
        candidates.sort(key=lambda x: x[0])

        # 4. Compute budget from positions only (all buy orders are being cancelled,
        #    but cache still shows them until exchange confirms — don't query cache)
        if self._cfg.max_total_deployed_cents > 0:
            position_cost = sum(
                info.get("contracts", 0) * self._cfg.max_cost_cents
                for info in self._positions_info.values()
            )
            remaining_budget = self._cfg.max_total_deployed_cents - position_cost
        else:
            remaining_budget = None  # disabled

        # 5. Deploy in order, tracking budget ourselves
        for bid_cents, ticker, signal in candidates:
            if remaining_budget is not None and remaining_budget <= 0:
                self.log.info(f"Rebalance budget exhausted, {len(candidates)} candidates remaining")
                break
            deployed = self._deploy_ladder(ticker, signal, budget_remaining=remaining_budget)
            if remaining_budget is not None:
                remaining_budget -= deployed

    def _cancel_ladder_orders(self, ticker: str):
        """Cancel all Phase 2 ladder buy orders for a ticker."""
        order_ids = set(self._ladder_orders.get(ticker, []))
        open_orders = self.cache.orders_open(strategy_id=self.id)
        for order in open_orders:
            if (
                order.client_order_id in order_ids
                and order.side == OrderSide.BUY
            ):
                self.cancel_order(order)
        # Pop AFTER cancellation attempts
        self._ladder_orders.pop(ticker, None)

    def _cancel_all_resting_buys(self):
        """Cancel all open BUY orders across all instruments (back-off)."""
        open_orders = self.cache.orders_open(strategy_id=self.id)
        for order in open_orders:
            if order.side == OrderSide.BUY:
                self.cancel_order(order)
        # Clear tracked buy order lists — forces redeployment after back-off
        self._open_spread_orders.clear()
        self._ladder_orders.clear()
        self._last_ladder_bid.clear()

    def on_stop(self):
        """Log shutdown -- engine handles order cleanup on stop."""
        self.log.info("WeatherStrategy stopping")

    def _sync_positions_to_actor(self):
        """Push current position info to FeatureActor for exit rule evaluation."""
        if self._feature_actor is not None:
            self._feature_actor.update_positions(self._positions_info)

    @property
    def signals_received(self) -> int:
        return self._signals_received

    @property
    def alerts_received(self) -> int:
        return self._alerts_received
