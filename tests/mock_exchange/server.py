"""Mock Kalshi REST/WebSocket server for confidence tests."""
from __future__ import annotations

import asyncio
import dataclasses
import json
import time
import uuid
from typing import Any

from websockets.asyncio.server import serve as ws_serve


@dataclasses.dataclass
class MockOrder:
    order_id: str
    ticker: str
    side: str          # "yes" or "no"
    action: str        # "buy" or "sell"
    type: str          # "limit" or "market"
    status: str        # "resting", "filled", "canceled"
    count: int
    client_order_id: str
    yes_price: int = 0
    no_price: int = 0
    time_in_force: str = "good_till_canceled"


_STATUS_PHRASES = {200: "OK", 201: "Created", 404: "Not Found", 500: "Internal Server Error"}

_SID_USER_ORDER = 1
_SID_FILL = 2
_SID_ORDERBOOK = 10


class MockKalshiServer:
    """Mock Kalshi REST + WebSocket server for adapter confidence tests.

    REST: asyncio.start_server on rest_port (full HTTP method support).
    WS:   websockets.serve on ws_port (separate port, optional).
    """

    def __init__(self) -> None:
        self._orders: dict[str, MockOrder] = {}
        self._fills: list[dict] = []
        self._balance: int = 100_000  # $1000.00 in cents

        # REST server
        self._http_server: asyncio.AbstractServer | None = None
        self.port: int | None = None

        # WS server
        self._ws_server = None
        self.ws_port: int | None = None

        # Active WS connections: ws → {"channels": set[str], "tickers": set[str]}
        self._subscriptions: dict = {}

        # Orderbook state: ticker → {"yes": float_dollars, "no": float_dollars}
        self._books: dict[str, dict[str, float]] = {}

        # Per-SID sequence counter (global — first message per connection is accepted)
        self._seq: dict[int, int] = {}

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self, port: int = 0, ws_port: int | None = None) -> None:
        """Bind servers. port=0 / ws_port=0 let the OS pick free ports.
        ws_port=None skips starting the WS server.
        """
        self._http_server = await asyncio.start_server(
            self._handle_http, "127.0.0.1", port,
        )
        self.port = self._http_server.sockets[0].getsockname()[1]

        if ws_port is not None:
            self._ws_server = await ws_serve(
                self._ws_handler, "127.0.0.1", ws_port,
            )
            self.ws_port = self._ws_server.sockets[0].getsockname()[1]

    async def stop(self) -> None:
        if self._ws_server:
            self._ws_server.close()
            await self._ws_server.wait_closed()
        if self._http_server:
            self._http_server.close()
            await self._http_server.wait_closed()

    # ------------------------------------------------------------------
    # WS: sequence helpers
    # ------------------------------------------------------------------

    def _next_seq(self, sid: int) -> int:
        seq = self._seq.get(sid, 0) + 1
        self._seq[sid] = seq
        return seq

    # ------------------------------------------------------------------
    # WS: send helpers
    # ------------------------------------------------------------------

    async def _send_ws_msg(self, ws, type_: str, sid: int, msg: dict) -> None:
        envelope = {"type": type_, "sid": sid, "seq": self._next_seq(sid), "msg": msg}
        try:
            await ws.send(json.dumps(envelope))
        except Exception:
            pass  # Connection already closed

    async def _broadcast_ws(
        self,
        type_: str,
        sid: int,
        msg: dict,
        channel: str | None = None,
    ) -> None:
        """Broadcast to all connected subscribers.
        If channel is given, only sends to connections subscribed to that channel.
        """
        for ws, sub in list(self._subscriptions.items()):
            if channel and channel not in sub["channels"]:
                continue
            await self._send_ws_msg(ws, type_, sid, msg)

    # ------------------------------------------------------------------
    # WS: orderbook state
    # ------------------------------------------------------------------

    def set_initial_book(
        self,
        ticker: str,
        no_bid_cents: int,
        yes_bid_cents: int,
    ) -> None:
        """Set the mock orderbook for a ticker (in cents)."""
        self._books[ticker] = {
            "yes": yes_bid_cents / 100.0,
            "no": no_bid_cents / 100.0,
        }

    async def _publish_orderbook_snapshot(self, ticker: str, ws) -> None:
        book = self._books.get(ticker, {"yes": 0.0, "no": 0.0})
        yes_bid = book.get("yes", 0.0)
        no_bid = book.get("no", 0.0)
        msg = {
            "market_ticker": ticker,
            "yes_dollars_fp": [[f"{yes_bid:.2f}", "100"]] if yes_bid > 0 else [],
            "no_dollars_fp": [[f"{no_bid:.2f}", "100"]] if no_bid > 0 else [],
        }
        await self._send_ws_msg(ws, "orderbook_snapshot", _SID_ORDERBOOK, msg)

    async def _publish_orderbook_delta(
        self,
        ticker: str,
        side: str,
        price_dollars: str,
        delta_fp: str,
    ) -> None:
        """Update local book state and broadcast delta to subscribed connections."""
        book = self._books.setdefault(ticker, {"yes": 0.0, "no": 0.0})
        price = float(price_dollars)
        delta = float(delta_fp)
        if delta > 0:
            book[side] = price
        elif abs(book.get(side, 0.0) - price) < 1e-9:
            book[side] = 0.0

        msg = {
            "market_ticker": ticker,
            "price_dollars": price_dollars,
            "delta_fp": delta_fp,
            "side": side,
        }
        await self._broadcast_ws(
            "orderbook_delta", _SID_ORDERBOOK, msg, channel="orderbook_delta",
        )

    # ------------------------------------------------------------------
    # WS: handler
    # ------------------------------------------------------------------

    async def _ws_handler(self, websocket) -> None:
        self._subscriptions[websocket] = {"channels": set(), "tickers": set()}
        try:
            async for raw in websocket:
                try:
                    cmd = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                if cmd.get("cmd") == "subscribe":
                    params = cmd.get("params", {})
                    channels = set(params.get("channels", []))
                    tickers = set(params.get("market_tickers", []))
                    self._subscriptions[websocket]["channels"].update(channels)
                    self._subscriptions[websocket]["tickers"].update(tickers)
                    # Send orderbook snapshot for each ticker if subscribing to orderbook_delta
                    if "orderbook_delta" in channels:
                        for ticker in tickers:
                            await self._publish_orderbook_snapshot(ticker, websocket)
        except Exception:
            pass
        finally:
            self._subscriptions.pop(websocket, None)

    # ------------------------------------------------------------------
    # Matching engine
    # ------------------------------------------------------------------

    def _compute_fill(self, order: MockOrder) -> tuple[bool, float]:
        """Returns (should_fill, fill_price_dollars). Uses bids-only book."""
        book = self._books.get(order.ticker, {"yes": 0.0, "no": 0.0})
        yes_bid = book.get("yes", 0.0)
        no_bid = book.get("no", 0.0)

        if order.action == "buy":
            if order.side == "no":
                no_ask = round(1.0 - yes_bid, 10) if yes_bid > 0 else 1.0
                if order.type == "market":
                    return True, no_ask
                limit = order.no_price / 100.0
                return limit >= no_ask, no_ask
            else:  # yes
                yes_ask = round(1.0 - no_bid, 10) if no_bid > 0 else 1.0
                if order.type == "market":
                    return True, yes_ask
                limit = order.yes_price / 100.0
                return limit >= yes_ask, yes_ask
        else:  # sell
            if order.side == "no":
                if order.type == "market":
                    return True, no_bid
                raise NotImplementedError("limit sell not implemented")
            else:
                if order.type == "market":
                    return True, yes_bid
                raise NotImplementedError("limit sell not implemented")

        return False, 0.0

    async def _send_fill_notifications(self, order: MockOrder, fill_price: float) -> None:
        """Broadcast user_order(filled) + fill WS messages; store fill for REST."""
        # user_order message
        user_msg: dict[str, Any] = {
            "order_id": order.order_id,
            "ticker": order.ticker,
            "status": "filled",
            "side": order.side,
            "action": order.action,
            "client_order_id": order.client_order_id,
            "fill_count_fp": f"{order.count:.2f}",
            "initial_count_fp": f"{order.count:.2f}",
            "remaining_count_fp": "0.00",
        }
        if order.side == "yes":
            user_msg["yes_price_dollars"] = f"{fill_price:.2f}"
        else:
            user_msg["no_price_dollars"] = f"{fill_price:.2f}"

        # fill message — WS format uses _dollars strings; both fields always present
        trade_id = str(uuid.uuid4())
        complement = round(1.0 - fill_price, 10)
        fill_msg: dict[str, Any] = {
            "trade_id": trade_id,
            "order_id": order.order_id,
            "market_ticker": order.ticker,
            "side": order.side,
            "action": order.action,
            "count_fp": f"{order.count:.2f}",
            "is_taker": True,
            "ts": int(time.time() * 1_000_000_000),
            "client_order_id": order.client_order_id,
            "fee_cost": "0.00",
            "yes_price_dollars": f"{fill_price:.2f}" if order.side == "yes" else f"{complement:.2f}",
            "no_price_dollars": f"{fill_price:.2f}" if order.side == "no" else f"{complement:.2f}",
        }

        # REST fills use int-cent format (execution.py:523-525 reads yes_price/no_price in cents)
        rest_fill = {
            "trade_id": trade_id,
            "order_id": order.order_id,
            "market_ticker": order.ticker,
            "side": order.side,
            "action": order.action,
            "count_fp": f"{order.count:.2f}",
            "is_taker": True,
            "client_order_id": order.client_order_id,
            "fee_cost": "0.00",
            "yes_price": int(round((fill_price if order.side == "yes" else complement) * 100)),
            "no_price": int(round((fill_price if order.side == "no" else complement) * 100)),
        }
        self._fills.append(rest_fill)

        await self._broadcast_ws("user_order", _SID_USER_ORDER, user_msg, channel="user_orders")
        await self._broadcast_ws("fill", _SID_FILL, fill_msg, channel="fill")

    async def check_resting_orders(self) -> None:
        """After a book update, fill any resting limits that now cross the spread."""
        for order in list(self._orders.values()):
            if order.status != "resting":
                continue
            should_fill, fill_price = self._compute_fill(order)
            if should_fill:
                order.status = "filled"
                await self._send_fill_notifications(order, fill_price)

    # ------------------------------------------------------------------
    # Scenario runner
    # ------------------------------------------------------------------

    async def run_scenario(
        self,
        steps: list[tuple[str, str, str, str, float]],
    ) -> None:
        """Apply orderbook update steps sequentially.

        Each step: (ticker, side, price_dollars, delta_fp, delay_secs).
        delay_secs is applied BEFORE the step.
        """
        for ticker, side, price_dollars, delta_fp, delay in steps:
            if delay > 0:
                await asyncio.sleep(delay)
            await self._publish_orderbook_delta(ticker, side, price_dollars, delta_fp)
            await self.check_resting_orders()

    # ------------------------------------------------------------------
    # Raw HTTP protocol
    # ------------------------------------------------------------------

    async def _handle_http(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        try:
            request_line = (await reader.readline()).decode(errors="replace").strip()
            if not request_line:
                writer.close()
                return

            parts = request_line.split(" ", 2)
            if len(parts) < 2:
                writer.close()
                return
            method, full_path = parts[0], parts[1]

            content_length = 0
            while True:
                line = (await reader.readline()).decode(errors="replace").strip()
                if not line:
                    break
                if line.lower().startswith("content-length:"):
                    content_length = int(line.split(":", 1)[1].strip())

            body = b""
            if content_length > 0:
                body = await reader.readexactly(content_length)

            status_code, data = await self._route(method, full_path, body)
            await self._write_response(writer, status_code, data)

        except Exception as exc:
            await self._write_response(writer, 500, {"error": str(exc)})
        finally:
            try:
                writer.close()
                await writer.wait_closed()
            except Exception:
                pass

    async def _write_response(
        self,
        writer: asyncio.StreamWriter,
        status: int,
        data: Any,
    ) -> None:
        body = json.dumps(data).encode()
        phrase = _STATUS_PHRASES.get(status, "Unknown")
        header = (
            f"HTTP/1.1 {status} {phrase}\r\n"
            f"Content-Type: application/json\r\n"
            f"Content-Length: {len(body)}\r\n"
            f"Connection: close\r\n"
            f"\r\n"
        ).encode()
        writer.write(header + body)
        await writer.drain()

    # ------------------------------------------------------------------
    # Routing
    # ------------------------------------------------------------------

    async def _route(
        self,
        method: str,
        full_path: str,
        body: bytes,
    ) -> tuple[int, Any]:
        path = full_path.split("?")[0]

        prefix = "/trade-api/v2"
        if path.startswith(prefix):
            path = path[len(prefix):]

        if method == "GET" and path == "/markets":
            return 200, self._handle_get_markets()

        if method == "GET" and path == "/portfolio/balance":
            return 200, self._handle_get_balance()

        if method == "GET" and path == "/portfolio/orders":
            return 200, self._handle_get_orders(full_path)

        if method == "POST" and path == "/portfolio/orders":
            payload = json.loads(body) if body else {}
            return 201, await self._handle_create_order(payload)

        if method == "DELETE" and path == "/portfolio/orders/batched":
            return 200, await self._handle_cancel_all_orders()

        if method == "GET" and path.startswith("/portfolio/orders/"):
            order_id = path[len("/portfolio/orders/"):]
            o = self._orders.get(order_id)
            if not o:
                return 404, {"error": "not found"}
            return 200, {"order": self._order_to_dict(o)}

        if method == "DELETE" and path.startswith("/portfolio/orders/"):
            order_id = path[len("/portfolio/orders/"):]
            return 200, await self._handle_cancel_order(order_id)

        if method == "GET" and path == "/portfolio/fills":
            return 200, self._handle_get_fills()

        if method == "GET" and path == "/portfolio/positions":
            return 200, self._handle_get_positions()

        return 404, {"error": f"Not found: {method} {path}"}

    # ------------------------------------------------------------------
    # REST handlers
    # ------------------------------------------------------------------

    def _handle_get_markets(self) -> dict:
        from tests.mock_exchange.fixtures import (
            TEST_TICKER, TEST_TITLE, TEST_CLOSE_TIME, TEST_SERIES,
        )
        return {
            "markets": [
                {
                    "ticker": TEST_TICKER,
                    "event_ticker": f"{TEST_SERIES}-26MAR15",
                    "series_ticker": TEST_SERIES,
                    "title": TEST_TITLE,
                    "status": "active",
                    "close_time": TEST_CLOSE_TIME,
                    "yes_bid": 55,
                    "yes_ask": 58,
                    "no_bid": 42,
                    "no_ask": 45,
                    "volume_24h": 1000,
                }
            ],
            "cursor": "",
        }

    def _handle_get_balance(self) -> dict:
        return {"balance": self._balance}

    def _handle_get_orders(self, full_path: str) -> dict:
        orders = list(self._orders.values())
        if "status=resting" in full_path:
            orders = [o for o in orders if o.status == "resting"]
        elif "status=filled" in full_path:
            orders = [o for o in orders if o.status == "filled"]
        return {"orders": [self._order_to_dict(o) for o in orders]}

    async def _handle_create_order(self, data: dict) -> dict:
        order = MockOrder(
            order_id=str(uuid.uuid4()),
            ticker=data.get("ticker", ""),
            side=data.get("side", "yes"),
            action=data.get("action", "buy"),
            type=data.get("type", "limit"),
            status="resting",
            count=data.get("count", 1),
            client_order_id=data.get("client_order_id", ""),
            yes_price=data.get("yes_price", 0),
            no_price=data.get("no_price", 0),
            time_in_force=data.get("time_in_force", "good_till_canceled"),
        )
        self._orders[order.order_id] = order

        should_fill, fill_price = self._compute_fill(order)
        if should_fill:
            order.status = "filled"
            await self._send_fill_notifications(order, fill_price)
        else:
            # Notify resting via WS
            user_msg: dict[str, Any] = {
                "order_id": order.order_id,
                "ticker": order.ticker,
                "status": "resting",
                "side": order.side,
                "action": order.action,
                "client_order_id": order.client_order_id,
                "fill_count_fp": "0.00",
                "initial_count_fp": f"{order.count:.2f}",
                "remaining_count_fp": f"{order.count:.2f}",
            }
            await self._broadcast_ws(
                "user_order", _SID_USER_ORDER, user_msg, channel="user_orders",
            )

        return {"order": self._order_to_dict(order)}

    async def _handle_cancel_order(self, order_id: str) -> dict:
        o = self._orders.get(order_id)
        if o and o.status == "resting":
            o.status = "canceled"
            user_msg = {
                "order_id": o.order_id,
                "ticker": o.ticker,
                "status": "canceled",
                "side": o.side,
                "action": o.action,
                "client_order_id": o.client_order_id,
            }
            await self._broadcast_ws(
                "user_order", _SID_USER_ORDER, user_msg, channel="user_orders",
            )
            return {"order": self._order_to_dict(o)}
        return {"order": self._order_to_dict(o) if o else {}}

    async def _handle_cancel_all_orders(self) -> dict:
        canceled = []
        for o in self._orders.values():
            if o.status == "resting":
                o.status = "canceled"
                canceled.append(self._order_to_dict(o))
                user_msg = {
                    "order_id": o.order_id,
                    "ticker": o.ticker,
                    "status": "canceled",
                    "side": o.side,
                    "action": o.action,
                    "client_order_id": o.client_order_id,
                }
                await self._broadcast_ws(
                    "user_order", _SID_USER_ORDER, user_msg, channel="user_orders",
                )
        return {"orders": canceled}

    def _handle_get_fills(self) -> dict:
        return {"fills": self._fills}

    def _handle_get_positions(self) -> dict:
        return {"market_exposures": []}

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _order_to_dict(self, order: MockOrder) -> dict:
        return {
            "order_id": order.order_id,
            "ticker": order.ticker,
            "side": order.side,
            "action": order.action,
            "type": order.type,
            "status": order.status,
            "count": order.count,
            "client_order_id": order.client_order_id,
            "yes_price": order.yes_price,
            "no_price": order.no_price,
            "time_in_force": order.time_in_force,
        }

    # ------------------------------------------------------------------
    # Test helpers
    # ------------------------------------------------------------------

    def get_orders(self) -> dict[str, MockOrder]:
        return self._orders

    def add_fill(self, fill: dict) -> None:
        self._fills.append(fill)

    def set_balance(self, cents: int) -> None:
        self._balance = cents
