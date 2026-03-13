import asyncio
import json
import logging
import time
from typing import Optional, Any
from concurrent.futures import ThreadPoolExecutor

import websockets

import kalshi_python
from kalshi_python.api_client import KalshiAuth
from kalshi_python.api.markets_api import MarketsApi
from kalshi_python.api.portfolio_api import PortfolioApi

from nautilus_trader.core.uuid import UUID4
from nautilus_trader.live.data_client import LiveMarketDataClient
from nautilus_trader.live.execution_client import LiveExecutionClient
from nautilus_trader.execution.messages import (
    SubmitOrder,
    CancelOrder,
    GenerateOrderStatusReports,
)
from nautilus_trader.execution.reports import OrderStatusReport, PositionStatusReport
from nautilus_trader.model.identifiers import (
    Venue,
    InstrumentId,
    Symbol,
    ClientOrderId,
    ClientId,
    AccountId,
    VenueOrderId,
)
from nautilus_trader.model.instruments import CurrencyPair
from nautilus_trader.model.data import QuoteTick
from nautilus_trader.model.objects import Price, Quantity, Currency
from nautilus_trader.model.enums import (
    AccountType,
    OmsType,
    OrderSide,
    OrderType,
    TimeInForce,
    OrderStatus,
    PositionSide,
)
from nautilus_trader.live.factories import LiveDataClientFactory, LiveExecClientFactory
from nautilus_trader.common.providers import InstrumentProvider
from pydantic_settings import BaseSettings, SettingsConfigDict

log = logging.getLogger(__name__)

KALSHI_VENUE = Venue("KALSHI")


def _parse_ts(ts_raw, fallback_ns: int) -> int:
    """Parse Kalshi WebSocket timestamp to nanoseconds.

    Kalshi sends ISO 8601 strings like '2026-03-08T23:37:31.189819Z'.
    Nautilus expects integer nanoseconds.
    """
    if ts_raw is None:
        return fallback_ns
    if isinstance(ts_raw, (int, float)):
        return int(ts_raw * 1_000_000_000)
    if isinstance(ts_raw, str):
        from datetime import datetime, timezone
        try:
            dt = datetime.fromisoformat(ts_raw.replace("Z", "+00:00"))
            return int(dt.timestamp() * 1_000_000_000)
        except ValueError:
            return fallback_ns
    return fallback_ns


class KalshiConfig(BaseSettings):
    api_key_id: str = ""
    private_key_path: str = ""
    rest_host: str = "https://api.elections.kalshi.com/trade-api/v2"
    ws_host: str = "wss://api.elections.kalshi.com/trade-api/ws/v2"
    environment: str = "production"

    model_config = SettingsConfigDict(
        env_prefix="KALSHI_", env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )


def _get_ticker_and_side(instrument_id: InstrumentId):
    val = instrument_id.symbol.value
    if val.endswith("-YES"):
        return val[:-4], "yes"
    elif val.endswith("-NO"):
        return val[:-3], "no"
    else:
        raise ValueError(
            f"Cannot parse side from instrument {val!r}. "
            f"Expected suffix '-YES' or '-NO'."
        )


class KalshiDataClient(LiveMarketDataClient):
    def __init__(self, loop, instrument_provider, config: KalshiConfig, **kwargs):
        super().__init__(
            loop=loop,
            client_id=ClientId("KALSHI-DATA"),
            venue=KALSHI_VENUE,
            instrument_provider=instrument_provider,
            **kwargs,
        )
        self.k_config = config
        self._ws: Any = None
        self._ws_task: Optional[asyncio.Task] = None
        self._subscriptions: set[InstrumentId] = set()
        self._orderbooks: dict[str, dict[str, dict[float, float]]] = {}
        self._auth = KalshiAuth(config.api_key_id, config.private_key_path)

    async def _connect(self):
        self._log.info("Connecting to Kalshi WebSocket data...")
        self._ws_task = asyncio.create_task(self._ws_loop())

    async def _disconnect(self):
        self._log.info("Disconnecting Kalshi Data Client...")
        if self._ws_task:
            self._ws_task.cancel()
        if self._ws:
            await self._ws.close()

    async def _ws_loop(self):
        while True:
            try:
                # No auth strictly required for market data, but we can send it or just connect
                headers = self._auth.create_auth_headers("GET", "/trade-api/ws/v2")
                async with websockets.connect(self.k_config.ws_host, additional_headers=headers) as ws:
                    self._ws = ws
                    self._set_connected()
                    self._log.info("Connected to Kalshi Data WebSocket.")

                    # Resubscribe
                    for instr_id in self._subscriptions:
                        await self._send_subscribe(instr_id)

                    async for msg_str in ws:
                        self._handle_ws_message(msg_str)
            except asyncio.CancelledError:
                break
            except Exception as e:
                self._log.error(f"WebSocket connection error: {e}")
                await asyncio.sleep(5)

    def _handle_ws_message(self, msg_str: str | bytes):
        if isinstance(msg_str, bytes):
            msg_str = msg_str.decode("utf-8")
        try:
            msg = json.loads(msg_str)
            msg_type = msg.get("type")

            if msg_type == "orderbook_snapshot":
                data = msg.get("msg", {})
                ticker = data.get("market_ticker")
                if not ticker:
                    return

                ts = self._clock.timestamp_ns()

                # Kalshi V2 snapshot: yes_dollars_fp / no_dollars_fp (list of [price_str, qty_str])
                yes_raw = data.get("yes_dollars_fp", [])
                no_raw = data.get("no_dollars_fp", [])
                book: dict[str, dict[float, float]] = {"yes": {}, "no": {}}
                for p, q in yes_raw:
                    book["yes"][float(p)] = float(q)
                for p, q in no_raw:
                    book["no"][float(p)] = float(q)
                self._orderbooks[ticker] = book

                self._emit_quote_ticks(ticker, data, ts)

            elif msg_type == "orderbook_delta":
                data = msg.get("msg", {})
                ticker = data.get("market_ticker")
                if not ticker:
                    return

                ts = self._clock.timestamp_ns()

                # Kalshi V2 delta: side, price_dollars (string), delta_fp (string)
                side = data.get("side")  # "yes" or "no"
                price = float(data.get("price_dollars", "0"))
                delta = float(data.get("delta_fp", "0"))

                book = self._orderbooks.setdefault(ticker, {"yes": {}, "no": {}})
                if side in ("yes", "no"):
                    if delta <= 0:
                        book[side].pop(price, None)
                    else:
                        book[side][price] = delta

                self._emit_quote_ticks(ticker, data, ts)

        except Exception as e:
            self._log.error(f"Error handling WS message: {e}")

    def _emit_quote_ticks(self, ticker: str, data: dict, ts: int):
        """Derive and emit QuoteTicks for YES and NO sides from current orderbook state."""
        book = self._orderbooks.get(ticker)
        if not book:
            return

        yes_levels = {p: q for p, q in book["yes"].items() if q > 0}
        no_levels = {p: q for p, q in book["no"].items() if q > 0}

        for side_suffix in ["-YES", "-NO"]:
            instr_id = InstrumentId(Symbol(f"{ticker}{side_suffix}"), KALSHI_VENUE)
            if instr_id not in self._subscriptions:
                continue

            instrument = self._instrument_provider.find(instr_id)
            if not instrument:
                continue

            if not self._cache.instrument(instr_id):
                self._handle_data(instrument)

            # YES: bid = max(yes prices), ask = 1.0 - max(no prices)
            # NO:  bid = max(no prices),  ask = 1.0 - max(yes prices)
            if side_suffix == "-YES":
                best_bid = max(yes_levels.keys()) if yes_levels else None
                best_ask = (1.0 - max(no_levels.keys())) if no_levels else None
                bid_qty = yes_levels.get(best_bid, 0) if best_bid is not None else 0
                ask_qty = no_levels.get(max(no_levels.keys()), 0) if no_levels else 0
            else:
                best_bid = max(no_levels.keys()) if no_levels else None
                best_ask = (1.0 - max(yes_levels.keys())) if yes_levels else None
                bid_qty = no_levels.get(best_bid, 0) if best_bid is not None else 0
                ask_qty = yes_levels.get(max(yes_levels.keys()), 0) if yes_levels else 0

            if best_bid is None or best_ask is None:
                continue  # No complete book yet — do NOT emit garbage

            try:
                ts_event = _parse_ts(data.get("ts"), fallback_ns=int(ts))

                tick = QuoteTick(
                    instrument_id=instr_id,
                    bid_price=instrument.make_price(best_bid),
                    ask_price=instrument.make_price(best_ask),
                    bid_size=instrument.make_qty(int(bid_qty)),
                    ask_size=instrument.make_qty(int(ask_qty)),
                    ts_event=ts_event,
                    ts_init=int(ts),
                )
                self._handle_data(tick)
            except Exception as ex:
                self._log.error(f"Failed to create/emit QuoteTick for {instr_id}: {ex}")

    async def _send_subscribe(self, instrument_id: InstrumentId):
        if not self._ws:
            self._log.warning(f"WS not connected, cannot subscribe {instrument_id}")
            return
        ticker, _ = _get_ticker_and_side(instrument_id)
        sub_msg = {
            "id": int(time.time()),
            "cmd": "subscribe",
            "params": {"channels": ["orderbook_delta"], "market_tickers": [ticker]},
        }
        await self._ws.send(json.dumps(sub_msg))

    async def _subscribe_quote_ticks(self, command) -> None:
        instrument_id = command.instrument_id
        self._subscriptions.add(instrument_id)
        await self._send_subscribe(instrument_id)

        instrument = self._instrument_provider.find(instrument_id)
        if instrument and not self._cache.instrument(instrument_id):
            self._handle_data(instrument)

    async def _unsubscribe_quote_ticks(self, command) -> None:
        instrument_id = command.instrument_id
        if instrument_id in self._subscriptions:
            self._subscriptions.remove(instrument_id)


class KalshiExecutionClient(LiveExecutionClient):
    def __init__(self, loop, instrument_provider, config: KalshiConfig, **kwargs):
        super().__init__(
            loop=loop,
            client_id=ClientId("KALSHI"),
            venue=KALSHI_VENUE,
            oms_type=OmsType.HEDGING,
            account_type=AccountType.MARGIN,
            base_currency=Currency.from_str("USD"),
            instrument_provider=instrument_provider,
            **kwargs,
        )
        self.k_config = config
        self._set_account_id(AccountId("KALSHI-001"))

        self.k_api_config = kalshi_python.Configuration()
        self.k_api_config.host = config.rest_host
        self.k_client = kalshi_python.KalshiClient(self.k_api_config)
        self.k_client.set_kalshi_auth(
            key_id=config.api_key_id, private_key_path=config.private_key_path
        )
        self.k_portfolio = PortfolioApi(self.k_client)
        self.k_markets = MarketsApi(self.k_client)

        self._executor = ThreadPoolExecutor(max_workers=5)
        self._ws: Any = None
        self._ws_task: Optional[asyncio.Task] = None
        self._auth = KalshiAuth(config.api_key_id, config.private_key_path)
        self._accepted_orders: set[ClientOrderId] = set()

    async def _connect(self):
        self._log.info("Connecting to Kalshi Execution...")
        self._ws_task = asyncio.create_task(self._ws_loop())

    async def _disconnect(self):
        if self._ws_task:
            self._ws_task.cancel()
        if self._ws:
            await self._ws.close()
        self._executor.shutdown(wait=False)

    async def _ws_loop(self):
        while True:
            try:
                headers = self._auth.create_auth_headers("GET", "/trade-api/ws/v2")
                async with websockets.connect(
                    self.k_config.ws_host, additional_headers=headers
                ) as ws:
                    self._ws = ws
                    self._set_connected()
                    self._log.info("Connected to Kalshi Execution WebSocket.")

                    sub_msg = {
                        "id": int(time.time()),
                        "cmd": "subscribe",
                        "params": {"channels": ["user_orders", "fill"]},
                    }
                    await ws.send(json.dumps(sub_msg))

                    async for msg_str in ws:
                        self._handle_ws_message(msg_str)
            except asyncio.CancelledError:
                break
            except Exception as e:
                self._log.error(f"Exec WebSocket error: {e}")
                await asyncio.sleep(5)

    def _handle_ws_message(self, msg_str: str | bytes):
        if isinstance(msg_str, bytes):
            msg_str = msg_str.decode("utf-8")
        try:
            msg = json.loads(msg_str)
            msg_type = msg.get("type")
            ts = self._clock.timestamp_ns()

            if msg_type == "user_order":
                data = msg.get("msg", {})
                status = data.get("status")
                c_oid = ClientOrderId(data.get("client_order_id"))
                v_oid = data.get("order_id")

                ts_event = _parse_ts(data.get("ts"), fallback_ns=ts)

                ticker = data.get("ticker")
                side = data.get("side", "yes")
                instrument_id = InstrumentId(
                    Symbol(f"{ticker}-{side.upper()}"), KALSHI_VENUE
                )

                if status == "canceled":
                    self.generate_order_canceled(
                        strategy_id=None,
                        instrument_id=instrument_id,
                        client_order_id=c_oid,
                        venue_order_id=VenueOrderId(v_oid),
                        ts_event=ts_event,
                    )
                elif status == "resting":
                    if c_oid not in self._accepted_orders:
                        self.generate_order_accepted(
                            strategy_id=None,
                            instrument_id=instrument_id,
                            client_order_id=c_oid,
                            venue_order_id=VenueOrderId(v_oid),
                            ts_event=ts_event,
                        )
                        self._accepted_orders.add(c_oid)
                elif status == "rejected":
                    self.generate_order_rejected(
                        strategy_id=None,
                        instrument_id=instrument_id,
                        client_order_id=c_oid,
                        reason="Exchange rejected",
                        ts_event=ts_event,
                    )

            elif msg_type == "fill":
                data = msg.get("msg", {})
                c_oid = ClientOrderId(data.get("client_order_id"))
                v_oid = data.get("order_id")
                trade_id = data.get("trade_id")
                action = data.get("action")
                ticker = data.get("ticker")
                side = data.get("side", "yes")
                instrument_id = InstrumentId(
                    Symbol(f"{ticker}-{side.upper()}"), KALSHI_VENUE
                )
                
                ts_event = _parse_ts(data.get("ts"), fallback_ns=ts)

                instrument = self._instrument_provider.find(instrument_id)
                if not instrument:
                    self._log.error(f"FILL DROPPED: instrument {instrument_id} not in provider — POSITION MISMATCH RISK")
                    return

                if c_oid not in self._accepted_orders:
                    self.generate_order_accepted(
                        strategy_id=None,
                        instrument_id=instrument_id,
                        client_order_id=c_oid,
                        venue_order_id=VenueOrderId(v_oid),
                        ts_event=ts_event,
                    )
                    self._accepted_orders.add(c_oid)

                price_cents = data.get("price", 0)
                qty = data.get("count", 0)

                self.generate_order_filled(
                    strategy_id=None,
                    instrument_id=instrument_id,
                    client_order_id=c_oid,
                    venue_order_id=VenueOrderId(v_oid),
                    venue_position_id=None,
                    trade_id=trade_id,
                    order_side=OrderSide.BUY if action == "buy" else OrderSide.SELL,
                    order_type=OrderType.LIMIT,
                    last_qty=instrument.make_qty(qty),
                    last_px=instrument.make_price(price_cents / 100.0),
                    quote_currency=Symbol("USD"),
                    commission=Quantity(0, precision=2),
                    liquidity_side=None,
                    ts_event=ts_event,
                )
        except Exception as e:
            self._log.error(f"Error handling execution WS message: {e}")

    def _fetch_resting_orders(self):
        return self.k_portfolio.get_orders(status="resting")

    async def generate_order_status_reports(
        self, command: GenerateOrderStatusReports
    ) -> list[OrderStatusReport]:
        reports = []
        try:
            resp = await asyncio.get_running_loop().run_in_executor(
                self._executor, self._fetch_resting_orders
            )
            ts = self._clock.timestamp_ns()
            for order in getattr(resp, "orders", None) or []:
                instrument_id = InstrumentId(
                    Symbol(f"{order.ticker}-{order.side.upper()}"), KALSHI_VENUE
                )
                instrument = self._instrument_provider.find(instrument_id)
                if not instrument:
                    continue

                count = getattr(order, "remaining_count", None) or getattr(order, "count", None) or 0
                if count <= 0:
                    continue

                # Resolve price: NO orders have no_price, YES orders have yes_price
                price = None
                if order.side == "yes" and getattr(order, "yes_price", None):
                    price = instrument.make_price(order.yes_price / 100.0)
                elif order.side == "no" and getattr(order, "no_price", None):
                    price = instrument.make_price(order.no_price / 100.0)

                reports.append(
                    OrderStatusReport(
                        account_id=self.account_id,
                        instrument_id=instrument_id,
                        venue_order_id=VenueOrderId(order.order_id),
                        order_side=OrderSide.BUY
                        if order.action == "buy"
                        else OrderSide.SELL,
                        order_type=OrderType.LIMIT
                        if order.type == "limit"
                        else OrderType.MARKET,
                        time_in_force=TimeInForce.GTC,
                        order_status=OrderStatus.ACCEPTED,
                        quantity=instrument.make_qty(count),
                        filled_qty=instrument.make_qty(0),
                        report_id=UUID4(),
                        ts_accepted=ts,
                        ts_last=ts,
                        ts_init=ts,
                        client_order_id=ClientOrderId(order.client_order_id)
                        if order.client_order_id
                        else None,
                        price=price,
                    )
                )
        except Exception as e:
            self._log.error(f"Error fetching active orders: {e}")
        return reports

    def _fetch_positions_raw(self) -> list[dict]:
        """Two-step position query using raw HTTP responses.

        The kalshi_python SDK drops event_positions from the deserialized model,
        so we parse raw_data directly.  Step 1: get event tickers with exposure.
        Step 2: query per-event for individual market positions.
        """
        resp = self.k_portfolio.get_positions_with_http_info()
        data = json.loads(resp.raw_data)
        event_positions = data.get("event_positions", [])

        positions = []
        for ep in event_positions:
            event_ticker = ep.get("event_ticker")
            if not event_ticker:
                continue
            # Skip events with zero exposure (settled)
            exposure = float(ep.get("event_exposure_dollars", 0))
            if exposure == 0:
                continue
            detail_resp = self.k_portfolio.get_positions_with_http_info(
                event_ticker=event_ticker
            )
            detail = json.loads(detail_resp.raw_data)
            for mp in detail.get("market_positions", []):
                positions.append(mp)
        return positions

    async def generate_position_status_reports(self, command) -> list:
        reports = []
        try:
            positions = await asyncio.get_running_loop().run_in_executor(
                self._executor, self._fetch_positions_raw
            )
            ts = self._clock.timestamp_ns()
            for pos in positions:
                ticker = pos.get("ticker")
                position = int(float(pos.get("position_fp", 0)))
                if not ticker or position == 0:
                    continue
                side = "YES" if position > 0 else "NO"
                inst_id = InstrumentId(Symbol(f"{ticker}-{side}"), KALSHI_VENUE)
                instrument = self._instrument_provider.find(inst_id)
                if not instrument:
                    self._log.warning(f"Position {ticker}-{side} not in instrument cache, skipping")
                    continue
                reports.append(
                    PositionStatusReport(
                        account_id=self.account_id,
                        instrument_id=inst_id,
                        position_side=PositionSide.LONG,
                        quantity=instrument.make_qty(abs(position)),
                        report_id=UUID4(),
                        ts_last=ts,
                        ts_init=ts,
                    )
                )
                self._log.info(f"Position report: {ticker} {side} qty={abs(position)}")
        except Exception as e:
            self._log.error(f"Error fetching positions: {e}")
        return reports

    async def generate_fill_reports(self, command) -> list:
        return []

    def _do_submit_order(self, command: SubmitOrder):
        instrument_id = command.instrument_id
        ticker, side = _get_ticker_and_side(instrument_id)
        action = "buy" if command.order.side == OrderSide.BUY else "sell"
        count = int(command.order.quantity.as_double())
        order_type = (
            "market" if command.order.order_type == OrderType.MARKET else "limit"
        )
        client_order_id = command.order.client_order_id.value

        kwargs = dict(
            ticker=ticker,
            action=action,
            side=side,
            count=count,
            type=order_type,
            client_order_id=client_order_id,
        )

        # Don't pass limit prices for market orders
        if order_type == "limit" and command.order.price:
            price_cents = int(command.order.price.as_decimal() * 100)
            if side == "yes":
                kwargs["yes_price"] = price_cents
            else:
                kwargs["no_price"] = price_cents

        # kwargs["_request_timeout"] = 5
        resp = self.k_portfolio.create_order(**kwargs)
        return resp

    async def _submit_order(self, command: SubmitOrder) -> None:
        try:
            resp = await asyncio.get_running_loop().run_in_executor(
                self._executor, self._do_submit_order, command
            )
            ts = self._clock.timestamp_ns()
            # Fast-path acceptance if not already accepted via WS
            if command.order.client_order_id not in self._accepted_orders:
                self.generate_order_accepted(
                    strategy_id=command.strategy_id,
                    instrument_id=command.instrument_id,
                    client_order_id=command.order.client_order_id,
                    venue_order_id=VenueOrderId(resp.order.order_id),
                    ts_event=ts,
                )
                self._accepted_orders.add(command.order.client_order_id)

        except Exception as e:
            self._log.error(f"Submit error: {e}")
            self.generate_order_rejected(
                strategy_id=command.strategy_id,
                instrument_id=command.instrument_id,
                client_order_id=command.order.client_order_id,
                reason=str(e),
                ts_event=self._clock.timestamp_ns(),
            )

    def _do_cancel_order(self, command: CancelOrder):
        resp = self.k_portfolio.cancel_order(
            order_id=command.venue_order_id.value
        )
        return resp

    async def _cancel_order(self, command: CancelOrder) -> None:
        try:
            await asyncio.get_running_loop().run_in_executor(
                self._executor, self._do_cancel_order, command
            )
            # Actual cancel confirmation will arrive via WebSocket `user_order` stream
        except Exception as e:
            self._log.error(f"Cancel error: {e}")
            self.generate_order_cancel_rejected(
                strategy_id=command.strategy_id,
                instrument_id=command.instrument_id,
                client_order_id=command.client_order_id,
                venue_order_id=command.venue_order_id,
                reason=str(e),
                ts_event=self._clock.timestamp_ns(),
            )


class KalshiDataClientFactory(LiveDataClientFactory):
    @staticmethod
    def create(loop, name, config, msgbus, cache, clock):
        # We expect a KalshiConfig passed or inside dictionary
        if isinstance(config, KalshiConfig):
            k_config = config
        elif isinstance(config, dict):
            k_config = KalshiConfig(**config)
        else:
            k_config = KalshiConfig()

        # Instrument provider should be singleton or populated earlier
        provider = KalshiInstrumentProvider(k_config)
        return KalshiDataClient(
            loop=loop,
            instrument_provider=provider,
            config=k_config,
            msgbus=msgbus,
            cache=cache,
            clock=clock,
        )


class KalshiExecutionClientFactory(LiveExecClientFactory):
    @staticmethod
    def create(loop, name, config, msgbus, cache, clock):
        if isinstance(config, KalshiConfig):
            k_config = config
        elif isinstance(config, dict):
            k_config = KalshiConfig(**config)
        else:
            k_config = KalshiConfig()

        provider = KalshiInstrumentProvider(k_config)
        return KalshiExecutionClient(
            loop=loop,
            instrument_provider=provider,
            config=k_config,
            msgbus=msgbus,
            cache=cache,
            clock=clock,
        )


_SHARED_PROVIDER = None

class KalshiInstrumentProvider(InstrumentProvider):
    def __new__(cls, *args, **kwargs):
        global _SHARED_PROVIDER
        if _SHARED_PROVIDER is None:
            _SHARED_PROVIDER = super().__new__(cls)
            _SHARED_PROVIDER._initialized = False
        return _SHARED_PROVIDER

    def __init__(self, config: KalshiConfig):
        if getattr(self, '_initialized', False):
            return
        super().__init__()
        self._initialized = True
        self.config = config

        self.k_api_config = kalshi_python.Configuration()
        self.k_api_config.host = config.rest_host
        self.k_client = kalshi_python.KalshiClient(self.k_api_config)
        self.k_client.set_kalshi_auth(
            key_id=config.api_key_id, private_key_path=config.private_key_path
        )
        self.k_api = MarketsApi(self.k_client)

    def load_all(self, filters: dict | None = None):
        self._api_healthy = True
        series_ticker = (filters or {}).get("series_ticker")

        # Kalshi API series_ticker filter needs per-city series (e.g. "KXHIGHCHI"),
        # not the umbrella prefix ("KXHIGH"). Expand via SERIES_CONFIG if available.
        series_tickers = [series_ticker] if series_ticker else [None]
        if series_ticker:
            try:
                from kalshi_weather_ml.markets import SERIES_CONFIG
                expanded = [s for s, _ in SERIES_CONFIG if s.startswith(series_ticker)]
                if expanded:
                    series_tickers = expanded
            except ImportError:
                pass

        try:
            for status in ("open", "unopened"):
                for st in series_tickers:
                    cursor = None
                    while True:
                        resp = self.k_api.get_markets(
                            limit=200, cursor=cursor, status=status,
                            series_ticker=st,
                        )
                        for m in getattr(resp, "markets", None) or []:
                            if m.status not in ("active", "open", "unopened"):
                                continue
                            # Only load T (threshold) contracts, skip B (bracket)
                            ticker = getattr(m, "ticker", "")
                            parts = ticker.split("-")
                            if not any(p.startswith("T") and len(p) > 1 for p in parts):
                                continue
                            self.add(self._build_instrument(m, "YES"))
                            self.add(self._build_instrument(m, "NO"))

                        cursor = getattr(resp, "cursor", None)
                        if not cursor:
                            break
        except Exception as e:
            self._api_healthy = False
            log.error(f"API failure during load_all — returning empty instrument list: {e}")

    def _build_instrument(self, market, side: str) -> CurrencyPair:
        from decimal import Decimal

        return CurrencyPair(
            instrument_id=InstrumentId(Symbol(f"{market.ticker}-{side}"), KALSHI_VENUE),
            raw_symbol=Symbol(f"{market.ticker}-{side}"),
            base_currency=Currency.from_str("CONTRACT"),
            quote_currency=Currency.from_str("USD"),
            price_precision=2,  # PRD: prices reflect cents (2 decimal places)
            size_precision=0,  # PRD: size must be integral
            price_increment=Price(0.01, precision=2),
            size_increment=Quantity(1, precision=0),
            lot_size=None,
            max_quantity=Quantity(10000, precision=0),
            min_quantity=Quantity(1, precision=0),
            max_notional=None,
            min_notional=None,
            max_price=Price(0.99, precision=2),
            min_price=Price(0.01, precision=2),
            # PRD: Reflect fully-collateralized nature
            margin_init=Decimal("1.0"),
            margin_maint=Decimal("1.0"),
            maker_fee=Decimal("0.0"),
            taker_fee=Decimal("0.0"),
            ts_event=0,
            ts_init=0,
        )
