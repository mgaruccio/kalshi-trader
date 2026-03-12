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
from nautilus_trader.model.identifiers import ClientId, InstrumentId, Symbol
from nautilus_trader.model.enums import OrderSide, TimeInForce
from nautilus_trader.trading.strategy import Strategy, StrategyConfig

from kalshi_weather_ml.markets import parse_ticker, SERIES_CONFIG

from adapter import KALSHI_VENUE
from data_types import ModelSignal, DangerAlert

CLIMATE_CLIENT = ClientId("CLIMATE")

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
        self._ladder_deployed: set[str] = set()  # tickers with at least one successful ladder

        # Counters
        self._signals_received: int = 0
        self._alerts_received: int = 0
        self._feature_actor = None  # set after construction

    def set_feature_actor(self, actor):
        """Wire the FeatureActor reference for position updates."""
        self._feature_actor = actor

    def on_start(self):
        """Subscribe to ModelSignal, DangerAlert, and quote ticks. Start refresh timer."""
        self.subscribe_data(DataType(ModelSignal), client_id=CLIMATE_CLIENT)
        self.subscribe_data(DataType(DangerAlert), client_id=CLIMATE_CLIENT)

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
        """Track latest quotes for ladder pricing.

        On first quote for an eligible ticker that hasn't had a ladder deployed,
        trigger deployment. This handles the case where signals arrive before
        quotes (e.g., BacktestNode chunk boundary ordering).
        """
        key = tick.instrument_id.value
        self._latest_quotes[key] = tick

        # Extract ticker from instrument ID (e.g. "KXHIGHCHI-26MAR10-T64-NO.KALSHI" -> "KXHIGHCHI-26MAR10-T64")
        # Only check NO instruments (we're NO-only)
        if "-NO." not in key:
            return
        ticker = key.split("-NO.")[0]
        if ticker in self._ladder_deployed or ticker in self._danger_exited:
            return
        signal = self._eligible_signals.get(ticker)
        if signal is not None:
            self._deploy_ladder(ticker, signal)

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

        # Phase 2: Stable ladder
        if signal.p_win < self._cfg.stable_min_p_win:
            self.log.info(
                f"Skip {signal.ticker}: p_win={signal.p_win:.3f} < {self._cfg.stable_min_p_win}"
            )
            return

        # Store qualifying signal for periodic refresh
        self._eligible_signals[signal.ticker] = signal

        # Deploy Phase 2 ladder
        self._deploy_ladder(signal.ticker, signal)

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
            self.log.info(
                f"Phase1 spread: {signal.ticker} {signal.side} "
                f"p_win={signal.p_win:.3f} price={price_cents}c qty={self._cfg.open_spread_size}"
            )

        self._open_spread_placed.add(signal.ticker)
        self._open_spread_orders[signal.ticker] = order_ids

        # Set one-shot timer to cancel unfilled spread orders after window expires
        window_ns = self._cfg.open_spread_window_minutes * 60 * 1_000_000_000
        cancel_time_ns = signal.ts_event + window_ns
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
        order_ids = set(self._open_spread_orders.pop(ticker, []))
        open_orders = self.cache.orders_open(strategy_id=self.id)
        for order in open_orders:
            if order.client_order_id in order_ids and order.side == OrderSide.BUY:
                self.cancel_order(order)
                self.log.info(f"Phase1 spread cancel: {ticker} order {order.client_order_id}")

    def _deploy_ladder(self, ticker: str, signal: ModelSignal):
        """Place Phase 2 ladder buy orders relative to current bid."""
        quote_key = self._quote_key(ticker, signal.side)
        quote = self._latest_quotes.get(quote_key)
        if quote is None:
            self.log.warning(f"No quote for {quote_key}, skipping Phase 2 ladder")
            return

        bid_cents = int(round(quote.bid_price.as_double() * 100))

        instrument_id = InstrumentId(
            Symbol(f"{ticker}-{signal.side.upper()}"),
            KALSHI_VENUE,
        )
        instrument = self.cache.instrument(instrument_id)
        if instrument is None:
            self.log.warning(f"Instrument {instrument_id} not in cache, skipping Phase 2")
            return

        # Check remaining capacity
        existing = self._positions_info.get(ticker, {})
        current_contracts = existing.get("contracts", 0)
        capacity = self._cfg.max_position_per_ticker - current_contracts

        # Count pending buy order contracts
        pending = self._count_pending_buys(instrument_id)
        capacity -= pending
        if capacity <= 0:
            self.log.info(f"Skip ladder {ticker}: no capacity (pos={current_contracts}, pending={pending})")
            return

        order_ids = []
        for offset in self._cfg.stable_ladder_offsets_cents:
            price_cents = max(1, bid_cents - offset)
            price = instrument.make_price(price_cents / 100.0)
            size = min(self._cfg.stable_size, max(0, capacity))
            if size <= 0:
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
            self.log.info(
                f"Phase2 ladder: {ticker} {signal.side} "
                f"p_win={signal.p_win:.3f} bid={bid_cents}c offset={offset}c price={price_cents}c qty={size}"
            )
            capacity -= size

        if order_ids:
            self._ladder_orders.setdefault(ticker, []).extend(order_ids)
            self._ladder_deployed.add(ticker)

    def _count_pending_buys(self, instrument_id: InstrumentId) -> int:
        """Count contracts in outstanding BUY orders for an instrument."""
        total = 0
        target_value = instrument_id.value
        open_orders = self.cache.orders_open(strategy_id=self.id)
        for order in open_orders:
            if order.instrument_id.value == target_value and order.side == OrderSide.BUY:
                total += int(order.quantity.as_double())
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
        # Sell at best bid (market sell via aggressive limit)
        quote_key = self._quote_key(alert.ticker, side)
        quote = self._latest_quotes.get(quote_key)
        if quote is not None:
            price = quote.bid_price
        else:
            # Fallback: sell at 1c (will match any buyer)
            price = instrument.make_price(0.01)

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
        # Cancel via the order list we track
        all_buy_ids = set(
            self._open_spread_orders.pop(ticker, [])
            + self._ladder_orders.pop(ticker, [])
        )
        open_orders = self.cache.orders_open(strategy_id=self.id)
        for order in open_orders:
            if (
                order.client_order_id in all_buy_ids
                and order.side == OrderSide.BUY
            ):
                self.cancel_order(order)

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
        """Periodic ladder refresh. Handles back-off and capital redeployment."""
        if self._is_backoff_window():
            self.log.info("Back-off window: cancelling all resting buy orders")
            self._cancel_all_resting_buys()
            return

        # Refresh ladder for each eligible signal
        for ticker, signal in list(self._eligible_signals.items()):
            if ticker in self._danger_exited:
                continue

            # Cancel existing unfilled buys for this ticker
            self._cancel_ladder_orders(ticker)

            # Redeploy ladder at current prices
            self._deploy_ladder(ticker, signal)

    def _cancel_ladder_orders(self, ticker: str):
        """Cancel all Phase 2 ladder buy orders for a ticker."""
        order_ids = set(self._ladder_orders.pop(ticker, []))
        open_orders = self.cache.orders_open(strategy_id=self.id)
        for order in open_orders:
            if (
                order.client_order_id in order_ids
                and order.side == OrderSide.BUY
            ):
                self.cancel_order(order)

    def _cancel_all_resting_buys(self):
        """Cancel all open BUY orders across all instruments (back-off)."""
        open_orders = self.cache.orders_open(strategy_id=self.id)
        for order in open_orders:
            if order.side == OrderSide.BUY:
                self.cancel_order(order)
        # Clear tracked buy order lists
        self._open_spread_orders.clear()
        self._ladder_orders.clear()

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
