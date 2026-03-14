"""Kalshi execution client — order management, fills, and reconciliation."""
import asyncio
import logging
from collections import OrderedDict
from decimal import Decimal

import kalshi_python
from kalshi_python.api.portfolio_api import PortfolioApi
from kalshi_python.exceptions import ApiException

from nautilus_trader.core.uuid import UUID4
from nautilus_trader.execution.reports import FillReport, OrderStatusReport, PositionStatusReport
from nautilus_trader.live.execution_client import LiveExecutionClient
from nautilus_trader.live.retry import RetryManagerPool
from nautilus_trader.model.currencies import USD, USDC
from nautilus_trader.model.enums import (
    AccountType, LiquiditySide, OmsType, OrderSide, OrderStatus, OrderType,
    PositionSide, TimeInForce,
)
from nautilus_trader.model.identifiers import (
    AccountId, ClientId, ClientOrderId, InstrumentId, Symbol, TradeId, VenueOrderId,
)
from nautilus_trader.model.objects import Currency, Money, Price, Quantity

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

        retry_manager = await self._retry_manager_pool.acquire()
        try:
            response = await retry_manager.run(
                "create_order",
                asyncio.to_thread(self._portfolio.create_order, **params),
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
                await asyncio.wait_for(event.wait(), timeout=self._config.ack_timeout_secs)
            except asyncio.TimeoutError:
                # REST fallback: check order status
                await self._ack_fallback(command, response)
        finally:
            await self._retry_manager_pool.release(retry_manager)

    async def _ack_fallback(self, command, create_response) -> None:
        """Correction #20: On ACK timeout, query REST for order status."""
        try:
            order_id = create_response.order.order_id
            resp = await asyncio.to_thread(
                self._portfolio.get_order, order_id=order_id
            )
            order_data = getattr(resp, "order", None)
            if order_data and order_data.status in ("resting", "filled"):
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

    async def _cancel_order(self, command) -> None:
        try:
            await asyncio.to_thread(
                self._portfolio.cancel_order,
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
            await asyncio.to_thread(self._portfolio.cancel_all_orders)
        except ApiException as e:
            log.error(f"cancel_all_orders failed: {e}")

    # ------------------------------------------------------------------
    # WebSocket message handling
    # ------------------------------------------------------------------

    def _handle_ws_message(self, raw: bytes) -> None:
        try:
            msg_type, sid, seq, msg = decode_ws_msg(raw)
            ts = self._clock.timestamp_ns()

            if msg_type == "user_order" and isinstance(msg, UserOrderMsg):
                self._handle_user_order(msg, ts)
            elif msg_type == "fill" and isinstance(msg, FillMsg):
                self._handle_fill(msg, ts)
        except Exception as e:
            log.error(f"Error handling exec WS message: {e}")

    def _handle_user_order(self, msg: UserOrderMsg, ts: int) -> None:
        c_oid = ClientOrderId(msg.client_order_id) if msg.client_order_id else None
        v_oid = VenueOrderId(msg.order_id)
        instrument_id = InstrumentId(
            Symbol(f"{msg.ticker}-{msg.side.upper()}"), KALSHI_VENUE
        )

        # Signal ACK event
        if c_oid and c_oid.value in self._ack_events:
            self._ack_events.pop(c_oid.value).set()

        if msg.status == "resting":
            if c_oid and c_oid not in self._accepted_orders:
                self.generate_order_accepted(
                    strategy_id=None,
                    instrument_id=instrument_id,
                    client_order_id=c_oid,
                    venue_order_id=v_oid,
                    ts_event=ts,
                )
                self._accepted_orders[c_oid] = v_oid
        elif msg.status == "canceled":
            if c_oid:
                self.generate_order_canceled(
                    strategy_id=None,
                    instrument_id=instrument_id,
                    client_order_id=c_oid,
                    venue_order_id=v_oid,
                    ts_event=ts,
                )
        elif msg.status == "rejected":
            if c_oid:
                self.generate_order_rejected(
                    strategy_id=None,
                    instrument_id=instrument_id,
                    client_order_id=c_oid,
                    reason="Exchange rejected",
                    ts_event=ts,
                )

    def _handle_fill(self, msg: FillMsg, ts: int) -> None:
        # Correction #4: Settlement fills (no ticker) → reconcile positions
        if not msg.market_ticker:
            asyncio.ensure_future(self.generate_position_status_reports(None))
            return

        # Correction #22: Bounded dedup cache
        if msg.trade_id in self._seen_trade_ids:
            return
        self._seen_trade_ids[msg.trade_id] = None
        if len(self._seen_trade_ids) > _DEDUP_MAX:
            self._seen_trade_ids.popitem(last=False)

        c_oid = ClientOrderId(msg.client_order_id) if msg.client_order_id else None
        v_oid = VenueOrderId(msg.order_id)
        instrument_id = InstrumentId(
            Symbol(f"{msg.market_ticker}-{msg.side.upper()}"), KALSHI_VENUE
        )
        instrument = self._instrument_provider.find(instrument_id)
        if not instrument:
            log.warning(f"Fill for unknown instrument {instrument_id}")
            return

        # Generate accepted first if not yet done
        if c_oid and c_oid not in self._accepted_orders:
            self.generate_order_accepted(
                strategy_id=None,
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
            strategy_id=None,
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
            resp = await asyncio.to_thread(self._portfolio.get_balance)
            balance = getattr(resp, "balance", None)
            if balance is None:
                return
            balance_dollars = float(balance) / 100.0  # Kalshi balance is in cents
            self.generate_account_state(
                balances=[Money(balance_dollars, USD)],
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
                self._portfolio.get_orders, status="resting"
            )
            ts = self._clock.timestamp_ns()
            for order in getattr(resp, "orders", []) or []:
                ticker = getattr(order, "ticker", None)
                side = getattr(order, "side", "yes")
                if not ticker:
                    continue
                instrument_id = InstrumentId(
                    Symbol(f"{ticker}-{side.upper()}"), KALSHI_VENUE
                )
                instrument = self._instrument_provider.find(instrument_id)
                if not instrument:
                    continue

                yes_price = getattr(order, "yes_price", None)
                no_price = getattr(order, "no_price", None)
                price = None
                if side == "yes" and yes_price:
                    price = instrument.make_price(yes_price / 100.0)
                elif side == "no" and no_price:
                    price = instrument.make_price(no_price / 100.0)

                reports.append(
                    OrderStatusReport(
                        account_id=self.account_id,
                        instrument_id=instrument_id,
                        venue_order_id=VenueOrderId(order.order_id),
                        order_side=OrderSide.BUY if order.action == "buy" else OrderSide.SELL,
                        order_type=OrderType.LIMIT,
                        time_in_force=TimeInForce.GTC,
                        order_status=OrderStatus.ACCEPTED,
                        quantity=instrument.make_qty(getattr(order, "count", 0)),
                        filled_qty=instrument.make_qty(0),
                        price=price,
                        client_order_id=ClientOrderId(order.client_order_id) if getattr(order, "client_order_id", None) else None,
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
            resp = await asyncio.to_thread(self._portfolio.get_fills)
            ts = self._clock.timestamp_ns()
            for fill in getattr(resp, "fills", []) or []:
                ticker = getattr(fill, "market_ticker", None)
                side = getattr(fill, "side", "yes")
                if not ticker:
                    continue
                instrument_id = InstrumentId(
                    Symbol(f"{ticker}-{side.upper()}"), KALSHI_VENUE
                )
                instrument = self._instrument_provider.find(instrument_id)
                if not instrument:
                    continue

                yes_price = getattr(fill, "yes_price", None)
                no_price = getattr(fill, "no_price", None)
                price_dollars = (yes_price or no_price or 0) / 100.0
                fee = getattr(fill, "fee_cost", None)

                reports.append(
                    FillReport(
                        account_id=self.account_id,
                        instrument_id=instrument_id,
                        venue_order_id=VenueOrderId(fill.order_id),
                        trade_id=TradeId(fill.trade_id),
                        order_side=OrderSide.BUY if fill.action == "buy" else OrderSide.SELL,
                        last_qty=instrument.make_qty(float(getattr(fill, "count_fp", 0))),
                        last_px=instrument.make_price(price_dollars),
                        commission=_parse_fill_commission(str(fee) if fee is not None else None),
                        liquidity_side=LiquiditySide.TAKER if getattr(fill, "is_taker", False) else LiquiditySide.MAKER,
                        client_order_id=ClientOrderId(fill.client_order_id) if getattr(fill, "client_order_id", None) else None,
                        report_id=UUID4(),
                        ts_event=ts,
                        ts_init=ts,
                    )
                )
        except Exception as e:
            log.error(f"generate_fill_reports failed: {e}")
        return reports

    async def generate_position_status_reports(self, command) -> list[PositionStatusReport]:
        reports = []
        try:
            resp = await asyncio.to_thread(self._portfolio.get_positions)
            ts = self._clock.timestamp_ns()
            for pos in getattr(resp, "market_exposures", []) or []:
                ticker = getattr(pos, "market_id", None) or getattr(pos, "ticker", None)
                position_fp = float(getattr(pos, "position_fp", 0) or 0)
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
