"""Mock Kalshi REST/WebSocket server for confidence tests."""
from __future__ import annotations

import asyncio
import dataclasses
import json
import uuid
from typing import Any


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


class MockKalshiServer:
    """Minimal mock of the Kalshi REST API for adapter confidence tests.

    REST is handled by asyncio.start_server (full HTTP method support).
    WebSocket will be added in Task 4 via websockets.serve on a separate port.
    """

    def __init__(self) -> None:
        self._orders: dict[str, MockOrder] = {}
        self._fills: list[dict] = []
        self._balance: int = 100_000  # $1000.00 in cents
        self._http_server: asyncio.AbstractServer | None = None
        self.port: int | None = None        # set after start()
        self.ws_port: int | None = None     # set by Task 4

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self, port: int = 0) -> None:
        """Bind the HTTP server. port=0 lets the OS pick a free port."""
        self._http_server = await asyncio.start_server(
            self._handle_http, "127.0.0.1", port,
        )
        self.port = self._http_server.sockets[0].getsockname()[1]

    async def stop(self) -> None:
        if self._http_server:
            self._http_server.close()
            await self._http_server.wait_closed()

    @property
    def rest_url(self) -> str:
        """Base URL for adapter REST config (e.g. http://127.0.0.1:PORT)."""
        return f"http://127.0.0.1:{self.port}"

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

            # Parse headers
            content_length = 0
            while True:
                line = (await reader.readline()).decode(errors="replace").strip()
                if not line:
                    break
                if line.lower().startswith("content-length:"):
                    content_length = int(line.split(":", 1)[1].strip())

            # Read body
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
        # Strip query string
        path = full_path.split("?")[0]

        # Strip /trade-api/v2 prefix used by the SDK
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
            return 201, self._handle_create_order(payload)

        if method == "DELETE" and path == "/portfolio/orders/batched":
            return 200, self._handle_cancel_all_orders()

        if method == "GET" and path.startswith("/portfolio/orders/"):
            order_id = path[len("/portfolio/orders/"):]
            return 200, self._handle_get_single_order(order_id)

        if method == "DELETE" and path.startswith("/portfolio/orders/"):
            order_id = path[len("/portfolio/orders/"):]
            return 200, self._handle_cancel_order(order_id)

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

    def _handle_get_single_order(self, order_id: str) -> dict:
        o = self._orders.get(order_id)
        if not o:
            return {"error": "not found"}
        return {"order": self._order_to_dict(o)}

    def _handle_create_order(self, data: dict) -> dict:
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
        return {"order": self._order_to_dict(order)}

    def _handle_cancel_order(self, order_id: str) -> dict:
        o = self._orders.get(order_id)
        if o:
            o.status = "canceled"
            return {"order": self._order_to_dict(o)}
        return {"order": {}}

    def _handle_cancel_all_orders(self) -> dict:
        canceled = []
        for o in self._orders.values():
            if o.status == "resting":
                o.status = "canceled"
                canceled.append(self._order_to_dict(o))
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
        """Return the live orders dict (for test assertions)."""
        return self._orders

    def add_fill(self, fill: dict) -> None:
        """Inject a fill into the server's fill list."""
        self._fills.append(fill)

    def set_balance(self, cents: int) -> None:
        self._balance = cents

    # ------------------------------------------------------------------
    # WebSocket placeholder (implemented in Task 4)
    # ------------------------------------------------------------------

    async def _ws_handler(self, websocket) -> None:
        """WebSocket handler — placeholder, implemented in Task 4."""
        async for _ in websocket:
            pass
