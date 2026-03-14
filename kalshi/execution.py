"""Kalshi execution client — order management, fills, and reconciliation."""

import asyncio
import json
import logging
import time
from collections import OrderedDict
from decimal import Decimal

import kalshi_python
from kalshi_python.api.portfolio_api import PortfolioApi
from kalshi_python.exceptions import ApiException
from kalshi_python.models.create_order_request import CreateOrderRequest

from nautilus_trader.core.uuid import UUID4
from nautilus_trader.execution.reports import (
    FillReport,
    OrderStatusReport,
    PositionStatusReport,
)
from nautilus_trader.live.execution_client import LiveExecutionClient
from nautilus_trader.live.retry import RetryManagerPool
from nautilus_trader.model.currencies import USD, USDC
from nautilus_trader.model.enums import (
    AccountType,
    LiquiditySide,
    OmsType,
    OrderSide,
    OrderStatus,
    OrderType,
    PositionSide,
    TimeInForce,
)
from nautilus_trader.model.identifiers import (
    AccountId,
    ClientId,
    ClientOrderId,
    InstrumentId,
    Symbol,
    TradeId,
    VenueOrderId,
)
from nautilus_trader.model.objects import (
    AccountBalance,
    Currency,
    Money,
    Price,
    Quantity,
)

from kalshi.common.constants import KALSHI_VENUE
from kalshi.common.errors import should_retry
from kalshi.providers import parse_instrument_id
from kalshi.websocket.client import KalshiWebSocketClient
from kalshi.websocket.types import FillMsg, UserOrderMsg, decode_ws_msg

log = logging.getLogger(__name__)

# Full time-in-force strings required by Kalshi (Correction #8)
_TIF_MAP = {
    TimeInForce.GTC: "good_till_canceled",
    TimeInForce.FOK: "fill_or_kill",
    TimeInForce.IOC: "immediate_or_cancel",
    TimeInForce.GTD: "good_till_canceled",
}

_DEDUP_MAX = 10_000  # Correction #22: bounded dedup cache


def _order_to_kalshi_params(order, instrument_id: InstrumentId) -> dict:
    """Translate a NautilusTrader order to Kalshi create_order kwargs.

    Correction #8: Use full time_in_force strings, not abbreviations.
    """
    ticker, side = parse_instrument_id(instrument_id)
    action = "buy" if order.side == OrderSide.BUY else "sell"
    count = int(order.quantity.as_double())
    tif = _TIF_MAP.get(order.time_in_force, "good_till_canceled")

    params: dict = {
        "ticker": ticker,
        "action": action,
        "side": side,
        "count": count,
        "type": "limit" if order.order_type == OrderType.LIMIT else "market",
        "time_in_force": tif,
        "client_order_id": order.client_order_id.value,
    }

    if order.order_type == OrderType.LIMIT and order.price:
        price_cents = int(round(float(order.price.as_decimal()) * 100))
        if side == "yes":
            params["yes_price"] = price_cents
        else:
            params["no_price"] = price_cents

    return params


def _parse_fill_commission(fee_cost: str | None) -> Money:
    """Parse fee_cost string to Money using USDC (8dp) for sub-cent precision.

    USD has only 2 decimal places — Kalshi fees like $0.0056 would be rounded
    to $0.01. USDC preserves the full fee amount from the exchange.
    """
    if fee_cost is None:
        return Money(0.0, USDC)
    return Money(float(fee_cost), USDC)


class TokenBucket:
    """Async token-bucket rate limiter (Correction #9: 10 writes/sec Kalshi Basic tier)."""

    def __init__(self, rate: float = 10.0, capacity: float = 10.0) -> None:
        self._rate = rate
        self._capacity = capacity
        self._tokens: float = capacity
        # Use monotonic clock for consistent elapsed-time calculation.
        # Safe to call at construction — no running loop required.
        self._updated_at: float = time.monotonic()

    async def acquire(self, cost: float = 1.0) -> None:
        while True:
            now = time.monotonic()
            elapsed = now - self._updated_at
            self._tokens = min(self._capacity, self._tokens + elapsed * self._rate)
            self._updated_at = now
            if self._tokens >= cost:
                self._tokens -= cost
                return
            sleep_time = (cost - self._tokens) / self._rate
            await asyncio.sleep(sleep_time)


class KalshiExecutionClient(LiveExecutionClient):
    """Kalshi live execution client.

    Handles order submission (with retry), fill processing from WebSocket,
    and reconciliation via REST API.
    """

    def __init__(
        self,
        loop: asyncio.AbstractEventLoop,
        name: str | None,
        config,
        instrument_provider,
        msgbus,
        cache,
        clock,
    ):
        super().__init__(
            loop=loop,
            client_id=ClientId(name or "KALSHI"),  # Correction #26
            venue=KALSHI_VENUE,
            oms_type=OmsType.HEDGING,  # YES/NO are separate instruments
            account_type=AccountType.CASH,
            base_currency=Currency.from_str("USD"),
            instrument_provider=instrument_provider,
            msgbus=msgbus,
            cache=cache,
            clock=clock,
            config=config,
        )
        self._config = config

        # Correction #27: set account_id in __init__ so generate_account_state works
        # NT reconciliation requires account_id to be non-None before first account state.
        self._set_account_id(AccountId(f"KALSHI-001"))

        # SDK REST client (Correction #19: timeout=10)
        api_config = kalshi_python.Configuration()
        api_config.host = config.rest_url
        api_config.request_timeout = 10
        self._k_client = kalshi_python.KalshiClient(api_config)
        self._k_client.set_kalshi_auth(
            key_id=config.api_key_id,
            private_key_path=config.private_key_path,
        )
        self._portfolio = PortfolioApi(self._k_client)

        # Retry pool (Correction #10: shutdown() in _stop)
        self._retry_manager_pool = RetryManagerPool(
            pool_size=100,
            max_retries=config.max_retries or 3,
            delay_initial_ms=config.retry_delay_initial_ms or 1_000,
            delay_max_ms=config.retry_delay_max_ms or 10_000,
            backoff_factor=2,
            logger=self._log,
            exc_types=(ApiException,),
            retry_check=should_retry,
        )

        # Token-bucket rate limiter (Correction #9: 10 writes/sec Basic tier)
        self._rate_limiter = TokenBucket(rate=10.0, capacity=10.0)

        # WebSocket for user_orders + fill channels
        self._ws_client = KalshiWebSocketClient(
            clock=clock,
            base_url=config.ws_url,
            channels=["user_orders", "fill"],
            handler=self._handle_ws_message,
            api_key_id=config.api_key_id,
            private_key_path=config.private_key_path,
            loop=loop,
        )

        # ACK synchronization (Correction #20)
        self._ack_events: dict[str, asyncio.Event] = {}
        self._accepted_orders: dict[ClientOrderId, VenueOrderId] = {}

        # Bounded deduplication cache (Correction #22)
        self._seen_trade_ids: OrderedDict[str, None] = OrderedDict()

    async def _connect(self) -> None:
        await self._instrument_provider.initialize()
        await self._ws_client.connect()
        await self._update_account_state()
        self._set_connected()

    async def _disconnect(self) -> None:
        await self._ws_client.disconnect()
        self._set_disconnected()

    def _stop(self) -> None:
        """Correction #10: shut down retry pool on stop."""
        self._retry_manager_pool.shutdown()

    # ------------------------------------------------------------------
    # Order routing
    # ------------------------------------------------------------------

    async def _submit_order(self, command) -> None:
        order = command.order
        params = _order_to_kalshi_params(order, command.instrument_id)

        # Correction #9: rate-limit write operations
        await self._rate_limiter.acquire()

        # Correction #32: kalshi_python SDK v3 requires a CreateOrderRequest
        # model; individual kwargs (including time_in_force, which no longer
        # exists in the schema) are not accepted.
        req = CreateOrderRequest(
            ticker=params["ticker"],
            side=params["side"],
            action=params["action"],
            count=params["count"],
            type=params["type"],
            client_order_id=params.get("client_order_id"),
            yes_price=params.get("yes_price"),
            no_price=params.get("no_price"),
        )

        retry_manager = await self._retry_manager_pool.acquire()
        try:
            # Correction #31: NT 1.224 RetryManager.run() API is
            # run(name, details, func, *args, **kwargs); pass asyncio.to_thread
            # as the callable so each retry creates a fresh coroutine.
            response = await retry_manager.run(
                "create_order",
                None,
                asyncio.to_thread,
                self._portfolio.create_order_with_http_info,
                req,
            )
            # Correction #3: None response → rejected
            if response is None:
                reason = retry_manager.message or "create_order returned None"
                self.generate_order_rejected(
                    strategy_id=command.strategy_id,
                    instrument_id=command.instrument_id,
                    client_order_id=order.client_order_id,
                    reason=reason,
                    ts_event=self._clock.timestamp_ns(),
                )
                return

            # Register ACK event and wait for WS confirmation (Correction #20)
            event = asyncio.Event()
            self._ack_events[order.client_order_id.value] = event
            try:
                await asyncio.wait_for(
                    event.wait(), timeout=self._config.ack_timeout_secs
                )
            except asyncio.TimeoutError:
                # REST fallback: check order status
                await self._ack_fallback(command, response)
        finally:
            await self._retry_manager_pool.release(retry_manager)

    async def _ack_fallback(self, command, create_response) -> None:
        """Correction #20: On ACK timeout, query REST for order status."""
        # Remove stale ACK event to prevent memory leak
        self._ack_events.pop(command.order.client_order_id.value, None)
        try:
            create_data = json.loads(create_response.raw_data)
            order_id = create_data["order"]["order_id"]
            resp = await asyncio.to_thread(
                self._portfolio.get_order_with_http_info, order_id=order_id
            )
            data = json.loads(resp.raw_data)
            order_data = data.get("order", {})
            # Correction #33: Kalshi v3 API uses "executed" for filled orders
            if order_data.get("status") in ("resting", "filled", "executed"):
                c_oid = command.order.client_order_id
                if c_oid not in self._accepted_orders:
                    self.generate_order_accepted(
                        strategy_id=command.strategy_id,
                        instrument_id=command.instrument_id,
                        client_order_id=c_oid,
                        venue_order_id=VenueOrderId(order_id),
                        ts_event=self._clock.timestamp_ns(),
                    )
                    self._accepted_orders[c_oid] = VenueOrderId(order_id)
        except Exception as e:
            log.error(f"ACK fallback failed: {e}")
            # Escalate: strategy must know the order's fate (not silently lost)
            self.generate_order_rejected(
                strategy_id=command.strategy_id,
                instrument_id=command.instrument_id,
                client_order_id=command.order.client_order_id,
                reason=f"ACK fallback failed: {e}",
                ts_event=self._clock.timestamp_ns(),
            )

    async def _cancel_order(self, command) -> None:
        # Correction #9: rate-limit write operations
        await self._rate_limiter.acquire()
        try:
            await asyncio.to_thread(
                self._portfolio.cancel_order_with_http_info,
                order_id=command.venue_order_id.value,
            )
        except ApiException as e:
            self.generate_order_cancel_rejected(
                strategy_id=command.strategy_id,
                instrument_id=command.instrument_id,
                client_order_id=command.client_order_id,
                venue_order_id=command.venue_order_id,
                reason=str(e),
                ts_event=self._clock.timestamp_ns(),
            )

    async def _cancel_all_orders(self, command) -> None:
        try:
            await asyncio.to_thread(self._portfolio.batch_cancel_orders_with_http_info)
        except ApiException as e:
            log.error(f"cancel_all_orders failed: {e}")

    # ------------------------------------------------------------------
    # WebSocket message handling
    # ------------------------------------------------------------------

    def _handle_ws_message(self, raw: bytes) -> None:
        try:
            msg_type, sid, seq, msg = decode_ws_msg(raw)
            ts = self._clock.timestamp_ns()

            # Sequence gap → missed fills are catastrophic; trigger reconciliation
            if not self._ws_client._check_sequence(sid=sid, seq=seq):
                log.warning(
                    f"Execution WS sequence gap sid={sid}, triggering reconciliation"
                )
                asyncio.create_task(self.generate_order_status_reports(None))
                asyncio.create_task(self.generate_fill_reports(None))
                return

            if msg_type == "user_order" and isinstance(msg, UserOrderMsg):
                self._handle_user_order(msg, ts)
            elif msg_type == "fill" and isinstance(msg, FillMsg):
                self._handle_fill(msg, ts)
        except Exception as e:
            log.error(f"Error handling exec WS message: {e}")

    def _strategy_id_for(self, c_oid: ClientOrderId | None):
        """Correction #35: look up StrategyId from cache for WS-driven events.

        NT generate_* methods require a concrete StrategyId, not None.
        """
        if c_oid is None:
            return None
        order = self._cache.order(c_oid)
        return order.strategy_id if order is not None else None

    def _handle_user_order(self, msg: UserOrderMsg, ts: int) -> None:
        c_oid = ClientOrderId(msg.client_order_id) if msg.client_order_id else None
        v_oid = VenueOrderId(msg.order_id)
        instrument_id = InstrumentId(
            Symbol(f"{msg.ticker}-{msg.side.upper()}"), KALSHI_VENUE
        )

        # Signal ACK event
        if c_oid and c_oid.value in self._ack_events:
            self._ack_events.pop(c_oid.value).set()

        sid = self._strategy_id_for(c_oid)
        if msg.status == "resting":
            if c_oid and c_oid not in self._accepted_orders and sid is not None:
                self.generate_order_accepted(
                    strategy_id=sid,
                    instrument_id=instrument_id,
                    client_order_id=c_oid,
                    venue_order_id=v_oid,
                    ts_event=ts,
                )
                self._accepted_orders[c_oid] = v_oid
        elif msg.status == "canceled":
            if c_oid and sid is not None:
                self.generate_order_canceled(
                    strategy_id=sid,
                    instrument_id=instrument_id,
                    client_order_id=c_oid,
                    venue_order_id=v_oid,
                    ts_event=ts,
                )
        elif msg.status == "filled":
            # Immediately-filled orders (market orders, limit crossing spread) arrive
            # with status="filled" — no "resting" status is sent. Accept if needed;
            # the fill details come separately via the fill channel.
            if c_oid and c_oid not in self._accepted_orders and sid is not None:
                self.generate_order_accepted(
                    strategy_id=sid,
                    instrument_id=instrument_id,
                    client_order_id=c_oid,
                    venue_order_id=v_oid,
                    ts_event=ts,
                )
                self._accepted_orders[c_oid] = v_oid
        elif msg.status == "rejected":
            if c_oid and sid is not None:
                self.generate_order_rejected(
                    strategy_id=sid,
                    instrument_id=instrument_id,
                    client_order_id=c_oid,
                    reason="Exchange rejected",
                    ts_event=ts,
                )

    def _handle_fill(self, msg: FillMsg, ts: int) -> None:
        # Correction #4: Settlement fills (no ticker) → reconcile positions
        if not msg.market_ticker:
            asyncio.create_task(self.generate_position_status_reports(None))
            return

        # Correction #22: Bounded dedup cache
        if msg.trade_id in self._seen_trade_ids:
            return
        self._seen_trade_ids[msg.trade_id] = None
        if len(self._seen_trade_ids) >= _DEDUP_MAX:
            self._seen_trade_ids.popitem(last=False)

        c_oid = ClientOrderId(msg.client_order_id) if msg.client_order_id else None
        v_oid = VenueOrderId(msg.order_id)
        instrument_id = InstrumentId(
            Symbol(f"{msg.market_ticker}-{msg.side.upper()}"), KALSHI_VENUE
        )
        instrument = self._instrument_provider.find(instrument_id)
        if not instrument:
            log.error(
                f"Fill for unknown instrument {instrument_id} — triggering reconciliation"
            )
            asyncio.create_task(self.generate_position_status_reports(None))
            return

        fill_sid = self._strategy_id_for(c_oid)
        if fill_sid is None:
            log.error(
                f"Fill for order {c_oid} not in cache — "
                "triggering reconciliation to recover position state"
            )
            asyncio.create_task(self.generate_fill_reports(None))
            asyncio.create_task(self.generate_position_status_reports(None))
            return

        # Generate accepted first if not yet done
        if c_oid and c_oid not in self._accepted_orders:
            self.generate_order_accepted(
                strategy_id=fill_sid,
                instrument_id=instrument_id,
                client_order_id=c_oid,
                venue_order_id=v_oid,
                ts_event=ts,
            )
            self._accepted_orders[c_oid] = v_oid

        price_dollars = float(msg.yes_price_dollars or msg.no_price_dollars or "0")
        qty = float(msg.count_fp)
        commission = _parse_fill_commission(msg.fee_cost)
        liquidity = LiquiditySide.TAKER if msg.is_taker else LiquiditySide.MAKER
        order_side = OrderSide.BUY if msg.action == "buy" else OrderSide.SELL

        self.generate_order_filled(
            strategy_id=fill_sid,
            instrument_id=instrument_id,
            client_order_id=c_oid,
            venue_order_id=v_oid,
            venue_position_id=None,
            trade_id=TradeId(msg.trade_id),
            order_side=order_side,
            order_type=OrderType.LIMIT,
            last_qty=instrument.make_qty(qty),
            last_px=instrument.make_price(price_dollars),
            quote_currency=USD,
            commission=commission,
            liquidity_side=liquidity,
            ts_event=ts,
        )

    # ------------------------------------------------------------------
    # Account state (Correction #16: use balance field)
    # ------------------------------------------------------------------

    async def _update_account_state(self) -> None:
        try:
            resp = await asyncio.to_thread(self._portfolio.get_balance_with_http_info)
            data = json.loads(resp.raw_data)
            balance = data.get("balance")
            if balance is None:
                return
            balance_dollars = float(balance) / 100.0  # Kalshi balance is in cents
            # Correction #28: generate_account_state requires AccountBalance, not Money
            balance_money = Money(balance_dollars, USD)
            account_balance = AccountBalance(
                total=balance_money,
                locked=Money(0.0, USD),
                free=balance_money,
            )
            self.generate_account_state(
                balances=[account_balance],
                margins=[],
                reported=True,
                ts_event=self._clock.timestamp_ns(),
            )
        except Exception as e:
            log.error(f"Failed to update account state: {e}")

    # ------------------------------------------------------------------
    # Reconciliation
    # ------------------------------------------------------------------

    async def generate_order_status_reports(self, command) -> list[OrderStatusReport]:
        reports = []
        try:
            resp = await asyncio.to_thread(
                self._portfolio.get_orders_with_http_info, status="resting"
            )
            data = json.loads(resp.raw_data)
            ts = self._clock.timestamp_ns()
            for order in data.get("orders", []) or []:
                ticker = order.get("ticker")
                side = order.get("side", "yes")
                if not ticker:
                    continue
                instrument_id = InstrumentId(
                    Symbol(f"{ticker}-{side.upper()}"), KALSHI_VENUE
                )
                instrument = self._instrument_provider.find(instrument_id)
                if not instrument:
                    continue

                yes_price = order.get("yes_price")
                no_price = order.get("no_price")
                price = None
                if side == "yes" and yes_price:
                    price = instrument.make_price(yes_price / 100.0)
                elif side == "no" and no_price:
                    price = instrument.make_price(no_price / 100.0)

                client_order_id_val = order.get("client_order_id")
                reports.append(
                    OrderStatusReport(
                        account_id=self.account_id,
                        instrument_id=instrument_id,
                        venue_order_id=VenueOrderId(order["order_id"]),
                        order_side=OrderSide.BUY
                        if order.get("action") == "buy"
                        else OrderSide.SELL,
                        order_type=OrderType.LIMIT,
                        # Resting orders are always GTC — FOK/IOC execute or
                        # cancel immediately and never appear in status=resting.
                        time_in_force=TimeInForce.GTC,
                        order_status=OrderStatus.ACCEPTED,
                        quantity=instrument.make_qty(order.get("count", 0)),
                        filled_qty=instrument.make_qty(0),
                        price=price,
                        client_order_id=ClientOrderId(client_order_id_val)
                        if client_order_id_val
                        else None,
                        report_id=UUID4(),
                        ts_accepted=ts,
                        ts_last=ts,
                        ts_init=ts,
                    )
                )
        except Exception as e:
            log.error(f"generate_order_status_reports failed: {e}")
        return reports

    async def generate_fill_reports(self, command) -> list[FillReport]:
        reports = []
        try:
            resp = await asyncio.to_thread(self._portfolio.get_fills_with_http_info)
            data = json.loads(resp.raw_data)
            ts = self._clock.timestamp_ns()
            for fill in data.get("fills", []) or []:
                ticker = fill.get("market_ticker")
                side = fill.get("side", "yes")
                if not ticker:
                    continue
                instrument_id = InstrumentId(
                    Symbol(f"{ticker}-{side.upper()}"), KALSHI_VENUE
                )
                instrument = self._instrument_provider.find(instrument_id)
                if not instrument:
                    continue

                yes_price = fill.get("yes_price")
                no_price = fill.get("no_price")
                price_dollars = (yes_price or no_price or 0) / 100.0
                fee = fill.get("fee_cost")
                client_order_id_val = fill.get("client_order_id")

                reports.append(
                    FillReport(
                        account_id=self.account_id,
                        instrument_id=instrument_id,
                        venue_order_id=VenueOrderId(fill["order_id"]),
                        trade_id=TradeId(fill["trade_id"]),
                        order_side=OrderSide.BUY
                        if fill.get("action") == "buy"
                        else OrderSide.SELL,
                        last_qty=instrument.make_qty(float(fill.get("count_fp", 0))),
                        last_px=instrument.make_price(price_dollars),
                        commission=_parse_fill_commission(
                            str(fee) if fee is not None else None
                        ),
                        liquidity_side=LiquiditySide.TAKER
                        if fill.get("is_taker", False)
                        else LiquiditySide.MAKER,
                        client_order_id=ClientOrderId(client_order_id_val)
                        if client_order_id_val
                        else None,
                        report_id=UUID4(),
                        ts_event=ts,
                        ts_init=ts,
                    )
                )
        except Exception as e:
            log.error(f"generate_fill_reports failed: {e}")
        return reports

    async def generate_position_status_reports(
        self, command
    ) -> list[PositionStatusReport]:
        reports = []
        try:
            resp = await asyncio.to_thread(self._portfolio.get_positions_with_http_info)
            data = json.loads(resp.raw_data)
            ts = self._clock.timestamp_ns()
            for pos in data.get("market_exposures", []) or []:
                ticker = pos.get("market_id") or pos.get("ticker")
                position_fp = float(pos.get("position_fp", 0) or 0)
                if not ticker or position_fp == 0:
                    continue
                side = "YES" if position_fp > 0 else "NO"
                instrument_id = InstrumentId(Symbol(f"{ticker}-{side}"), KALSHI_VENUE)
                instrument = self._instrument_provider.find(instrument_id)
                if not instrument:
                    continue
                reports.append(
                    PositionStatusReport(
                        account_id=self.account_id,
                        instrument_id=instrument_id,
                        position_side=PositionSide.LONG,
                        quantity=instrument.make_qty(abs(position_fp)),
                        report_id=UUID4(),
                        ts_last=ts,
                        ts_init=ts,
                    )
                )
        except Exception as e:
            log.error(f"generate_position_status_reports failed: {e}")
        return reports
