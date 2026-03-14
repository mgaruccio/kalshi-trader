# Adapter Confidence Test Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use forge:executing-plans to implement this plan task-by-task.

**Goal:** Prove the full Kalshi adapter loop works under a real NautilusTrader event loop — factory boot, REST serialization, WebSocket message flow, order routing, fills, and position tracking — without hitting the real Kalshi API.

**Architecture:** A local mock Kalshi server (HTTP + WebSocket on one port using `websockets` 16.0 dual-protocol support) provides a deterministic exchange simulator. A real `TradingNode` boots with the production adapter factories pointed at `localhost`. A test strategy places market and limit orders on NO weather contracts, chasing the bid by 1c on each quote update. Deterministic price scripts drive 4 named scenarios, each asserting specific adapter behaviors.

**Tech Stack:** `websockets` 16.0 (dual HTTP/WS server), `nautilus_trader` 1.224+ (TradingNode, Strategy, factories), `pytest` + `pytest-asyncio`, `cryptography` (dummy RSA key for auth).

**Required Skills:**
- `forge:writing-tests`: Invoke before Task 5 — covers test design, assertion quality, and TDD discipline
- `forge:verification-before-completion`: Invoke before Task 9 (final milestone) — evidence before success claims

## Context for Executor

### Key Files
- `kalshi/execution.py:112-580` — KalshiExecutionClient: order submit via `_submit_order()` (line 206), fill handling via `_handle_fill()` (line 370), ACK sync (line 233), dedup cache (line 377). REST calls go through `kalshi_python.PortfolioApi` with URLs constructed from `config.rest_url`.
- `kalshi/data.py:60-215` — KalshiDataClient: subscribes to `orderbook_delta` WS channel, builds per-ticker bids-only books, emits `QuoteTick` via `_emit_quotes()` (line 182). Quote derivation at `_derive_quotes()` (line 22): YES ask = 1.0 - max(NO bids).
- `kalshi/providers.py:36-158` — KalshiInstrumentProvider: loads markets via `MarketsApi.get_markets()`, creates `BinaryOption` instruments with YES/NO sides. Parses observation date from ticker (line 144).
- `kalshi/websocket/client.py:12-133` — KalshiWebSocketClient: wraps `nautilus_pyo3.WebSocketClient` (Rust). Sends subscribe commands as JSON (`{"id": N, "cmd": "subscribe", "params": {"channels": [...], "market_tickers": [...]}}`). Auth via `KalshiAuth` RSA-PSS headers (line 62).
- `kalshi/websocket/types.py:1-82` — msgspec Struct schemas: `OrderbookSnapshotMsg`, `OrderbookDeltaMsg`, `UserOrderMsg`, `FillMsg`. Two-pass decode via `WsEnvelope` → typed msg.
- `kalshi/config.py:12-57` — Config classes with `base_url_http` and `base_url_ws` override fields (lines 17-18, 41-42). When set, these bypass the environment-based URL selection.
- `kalshi/factories.py:32-67` — `KalshiLiveDataClientFactory` and `KalshiLiveExecClientFactory`. Shared singleton `KalshiInstrumentProvider` (line 13).
- `kalshi/common/constants.py:1-22` — `KALSHI_VENUE = Venue("KALSHI")`, REST/WS URLs, rate limit costs.
- `tests/sim/ws_factory.py:1-131` — `WsMessageFactory` for canned WS messages. Reference for message format.
- `tests/sim/test_scenarios.py:20-74` — `_make_adapter_stub()` pattern: SimpleNamespace + MethodType for testing Cython-descriptor classes.
- `tests/helpers.py:1-38` — `make_mock_order()`, `make_instrument_id()` helpers.
- `archive/main.py:52-99` — Reference `TradingNode` boot pattern: config → node → add factories → build → add strategy → run.

### Research Findings

**REST API paths** (SDK appends to `config.rest_url` which includes `/trade-api/v2`):
- `POST /portfolio/orders` — create order. Request: `{ticker, action, side, count, type, time_in_force, client_order_id, yes_price?, no_price?}`. Response (201): `{order: {order_id, ticker, status, ...}}`.
- `DELETE /portfolio/orders/{order_id}` — cancel order. Response (200): `{order: {...}}`.
- `GET /portfolio/orders?status=resting` — list resting orders (reconciliation). Response: `{orders: [...], cursor: ""}`.
- `GET /portfolio/orders/{order_id}` — single order (ACK fallback). Response: `{order: {order_id, status, ...}}`.
- `GET /portfolio/balance` — account balance in cents. Response: `{balance: 50000}`.
- `GET /markets?status=open&limit=200` — list markets. Response: `{markets: [...], cursor: ""}`.
- `GET /portfolio/fills` — list fills (reconciliation). Response: `{fills: [...], cursor: ""}`.
- `GET /portfolio/positions` — list positions (reconciliation). Response: `{market_exposures: [...]}`.

**URL construction**: SDK sets `Configuration.host = config.rest_url`. If `base_url_http = "http://localhost:PORT"`, the SDK calls `http://localhost:PORT/portfolio/orders`. So the mock server should serve paths WITHOUT the `/trade-api/v2` prefix, OR set `base_url_http = "http://localhost:PORT/trade-api/v2"` and serve WITH the prefix. The latter is more realistic — use `/trade-api/v2/...` paths.

**WebSocket protocol**: Client connects to `base_url_ws` directly (e.g. `ws://localhost:PORT/trade-api/ws/v2`). Sends subscribe commands as JSON. Server sends messages as JSON with envelope: `{type, sid, seq, msg}`. SID 1 = user_order channel, SID 2 = fill channel. Orderbook messages use their own SIDs.

**websockets 16.0 dual-protocol**: Use `process_request` callback in `websockets.serve()`. Return `None` for WebSocket upgrades (path = `/trade-api/ws/v2`), return `Response` for HTTP requests. The `Response` dataclass is at `websockets.http11.Response` with fields: `status_code`, `reason_phrase`, `headers`, `body`.

**Auth**: `KalshiAuth(key_id, private_key_path)` loads RSA PEM on init. Signs requests with RSA-PSS SHA256. Mock server ignores auth headers. Test needs a valid dummy RSA PEM file.

**No `_modify_order`**: The adapter has no order amendment method. Strategy must cancel + re-submit to chase bid.

**Orderbook is bids-only**: The adapter maintains separate YES and NO bid books. Asks are derived: `NO ask = 1.0 - max(YES bids)`. The mock server sends `orderbook_snapshot` and `orderbook_delta` messages that set bid levels for both YES and NO sides.

**`TradingNode.run()` blocks**: It starts an asyncio event loop and runs until `node.stop()`. For testing, start the mock server in a daemon thread with its own event loop, then run the node in the main thread. Schedule a stop callback from within the strategy when the scenario completes.

### Relevant Patterns
- `tests/sim/ws_factory.py` — Follow this pattern for constructing WS messages in the mock server
- `tests/sim/test_scenarios.py:24-74` — Follow SimpleNamespace + MethodType pattern if needed for stubbing
- `archive/main.py:52-99` — Follow this pattern for TradingNode configuration and boot

## Execution Architecture

**Team:** 2 devs, 1 spec reviewer, 1 quality reviewer
**Task dependencies:**
  - Task 1 (fixtures) and Task 2 (mock server) are independent — can run in parallel
  - Task 4 (WS + matching) depends on Task 2 (REST server)
  - Task 5 (scenarios) depends on Task 4
  - Task 6 (strategy) depends on nothing (independent design)
  - Task 7 (test runner) depends on Tasks 1, 4, 5, 6
**Phases:**
  - Phase 1: Tasks 1-3 (mock server foundation + review)
  - Phase 2: Tasks 4-6 (WS/matching/scenarios + review)
  - Phase 3: Tasks 7-9 (strategy + integration tests + review)
**Milestones:**
  - After Task 3 (mock server serves REST and unit-tested standalone)
  - After Task 6 (mock exchange handles full order lifecycle)
  - After Task 9 (all 4 scenarios pass end-to-end)

---

### Task 1: Create test fixtures and scaffolding

**Files:**
- Create: `tests/mock_exchange/__init__.py`
- Create: `tests/mock_exchange/fixtures.py`

**Step 1: Create empty `__init__.py`**

```python
# tests/mock_exchange/__init__.py
```

**Step 2: Write fixture module with dummy RSA key and test constants**

```python
# tests/mock_exchange/fixtures.py
"""Test fixtures for mock exchange — dummy RSA key and shared constants."""
import tempfile
import os

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa


# Test market constants
TEST_TICKER = "KXHIGHCHI-26MAR15-T42"
TEST_SERIES = "KXHIGH"
TEST_TITLE = "Will the high temperature in Chicago be 42°F or above on March 15?"
TEST_CLOSE_TIME = "2026-03-16T00:00:00Z"


def create_dummy_rsa_key() -> str:
    """Create a temporary RSA private key PEM file. Returns file path.

    Caller is responsible for cleanup (or use as context manager with tempfile).
    """
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    fd, path = tempfile.mkstemp(suffix=".pem")
    os.write(fd, pem)
    os.close(fd)
    return path
```

**Step 3: Write a smoke test for the fixture**

```python
# In tests/mock_exchange/test_fixtures.py
import os
from tests.mock_exchange.fixtures import create_dummy_rsa_key


def test_dummy_rsa_key_creates_valid_pem():
    path = create_dummy_rsa_key()
    try:
        assert os.path.exists(path)
        with open(path, "rb") as f:
            data = f.read()
        assert b"BEGIN PRIVATE KEY" in data
    finally:
        os.unlink(path)
```

**Step 4: Run the test**

Run: `cd /home/mike/code/kalshi-trader && uv run pytest tests/mock_exchange/test_fixtures.py -v`
Expected: PASS

**Step 5: Commit**

```bash
git add tests/mock_exchange/__init__.py tests/mock_exchange/fixtures.py tests/mock_exchange/test_fixtures.py
git commit -m "test: add mock exchange fixtures — dummy RSA key and test constants"
```

---

### Task 2: Implement mock REST server

**Files:**
- Create: `tests/mock_exchange/server.py`

The mock server uses `websockets` 16.0 dual-protocol: `process_request` intercepts HTTP requests and returns JSON responses; WebSocket connections proceed normally. This task implements only the REST (HTTP) handling. Task 4 adds WebSocket support.

**Step 1: Write a test for the REST endpoints**

```python
# In tests/mock_exchange/test_server.py
"""Tests for mock Kalshi REST server."""
import asyncio
import json
import os
import urllib.request

import pytest

from tests.mock_exchange.fixtures import create_dummy_rsa_key, TEST_TICKER
from tests.mock_exchange.server import MockKalshiServer


@pytest.fixture
def rsa_key_path():
    path = create_dummy_rsa_key()
    yield path
    os.unlink(path)


@pytest.fixture
def server_url():
    """Start mock server, yield base URL, stop after test."""
    server = MockKalshiServer()
    loop = asyncio.new_event_loop()

    import threading
    def run():
        loop.run_until_complete(server.start(port=0))
        loop.run_forever()
    t = threading.Thread(target=run, daemon=True)
    t.start()

    # Wait for server to be ready
    for _ in range(50):
        if server.port is not None:
            break
        import time; time.sleep(0.05)
    assert server.port is not None, "Server failed to start"

    yield f"http://localhost:{server.port}"

    loop.call_soon_threadsafe(loop.stop)
    t.join(timeout=2)


def _get(url: str) -> dict:
    req = urllib.request.Request(url)
    with urllib.request.urlopen(req) as resp:
        return json.loads(resp.read())


def _post(url: str, data: dict) -> tuple[int, dict]:
    body = json.dumps(data).encode()
    req = urllib.request.Request(url, data=body, method="POST")
    req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


class TestMockRESTServer:
    def test_get_markets_returns_test_instrument(self, server_url):
        data = _get(f"{server_url}/trade-api/v2/markets?status=open&limit=200")
        assert "markets" in data
        tickers = [m["ticker"] for m in data["markets"]]
        assert TEST_TICKER in tickers

    def test_get_balance_returns_cents(self, server_url):
        data = _get(f"{server_url}/trade-api/v2/portfolio/balance")
        assert "balance" in data
        assert isinstance(data["balance"], int)
        assert data["balance"] > 0

    def test_get_orders_empty(self, server_url):
        data = _get(f"{server_url}/trade-api/v2/portfolio/orders?status=resting")
        assert data["orders"] == []

    def test_create_order_returns_order_id(self, server_url):
        status, data = _post(f"{server_url}/trade-api/v2/portfolio/orders", {
            "ticker": TEST_TICKER,
            "action": "buy",
            "side": "no",
            "count": 5,
            "type": "limit",
            "time_in_force": "good_till_canceled",
            "client_order_id": "C-TEST-001",
            "no_price": 34,
        })
        assert status == 201
        assert "order" in data
        assert data["order"]["order_id"]
        assert data["order"]["status"] in ("resting", "filled")

    def test_cancel_order(self, server_url):
        # Create first
        _, create_data = _post(f"{server_url}/trade-api/v2/portfolio/orders", {
            "ticker": TEST_TICKER,
            "action": "buy",
            "side": "no",
            "count": 1,
            "type": "limit",
            "time_in_force": "good_till_canceled",
            "client_order_id": "C-CANCEL-001",
            "no_price": 10,
        })
        order_id = create_data["order"]["order_id"]

        # Cancel
        req = urllib.request.Request(
            f"{server_url}/trade-api/v2/portfolio/orders/{order_id}",
            method="DELETE",
        )
        with urllib.request.urlopen(req) as resp:
            assert resp.status == 200

    def test_get_fills_empty(self, server_url):
        data = _get(f"{server_url}/trade-api/v2/portfolio/fills")
        assert data["fills"] == []

    def test_get_positions_empty(self, server_url):
        data = _get(f"{server_url}/trade-api/v2/portfolio/positions")
        assert data["market_exposures"] == []
```

**Step 2: Run to verify tests fail (server doesn't exist yet)**

Run: `cd /home/mike/code/kalshi-trader && uv run pytest tests/mock_exchange/test_server.py -v 2>&1 | head -20`
Expected: ImportError — `cannot import name 'MockKalshiServer'`

**Step 3: Implement the mock server**

Create `tests/mock_exchange/server.py`:

```python
"""Mock Kalshi server — HTTP + WebSocket on a single port via websockets 16.0.

HTTP requests are intercepted by process_request() and return JSON.
WebSocket connections at /trade-api/ws/v2 proceed to the handler.
"""
from __future__ import annotations

import asyncio
import json
import re
import uuid
from dataclasses import dataclass, field
from urllib.parse import urlparse, parse_qs

import websockets
from websockets.http11 import Response
from websockets.datastructures import Headers

from tests.mock_exchange.fixtures import (
    TEST_TICKER, TEST_SERIES, TEST_TITLE, TEST_CLOSE_TIME,
)

# Default starting balance: $500 in cents
DEFAULT_BALANCE_CENTS = 50_000


@dataclass
class MockOrder:
    order_id: str
    ticker: str
    side: str  # "yes" or "no"
    action: str  # "buy" or "sell"
    order_type: str  # "limit" or "market"
    count: int
    status: str  # "resting", "filled", "canceled"
    client_order_id: str | None = None
    yes_price: int | None = None  # cents
    no_price: int | None = None  # cents
    time_in_force: str = "good_till_canceled"


class MockKalshiServer:
    """Mock Kalshi exchange with REST + WebSocket support.

    REST endpoints serve market data and order management.
    WebSocket connections receive orderbook updates, fills, and order ACKs.
    """

    def __init__(self) -> None:
        self.port: int | None = None
        self.orders: dict[str, MockOrder] = {}
        self.fills: list[dict] = []
        self.balance_cents: int = DEFAULT_BALANCE_CENTS
        self._ws_connections: list[websockets.ServerConnection] = []
        self._server = None

    # ------------------------------------------------------------------
    # Server lifecycle
    # ------------------------------------------------------------------

    async def start(self, port: int = 0) -> None:
        self._server = await websockets.serve(
            self._ws_handler,
            "localhost",
            port,
            process_request=self._process_http,
        )
        # Extract actual port (needed when port=0)
        for sock in self._server.sockets:
            addr = sock.getsockname()
            self.port = addr[1]
            break

    async def stop(self) -> None:
        if self._server:
            self._server.close()
            await self._server.wait_closed()

    # ------------------------------------------------------------------
    # HTTP request routing (process_request intercept)
    # ------------------------------------------------------------------

    def _process_http(self, connection, request):
        """Route HTTP requests. Return Response for REST, None for WebSocket."""
        path = request.path
        # Strip query string for path matching
        path_clean = path.split("?")[0]

        # WebSocket upgrade — let it through
        if path_clean == "/trade-api/ws/v2":
            return None

        # Parse query params
        parsed = urlparse(path)
        params = parse_qs(parsed.query)

        # REST routing
        method = request.headers.get("Method", "GET")
        # websockets doesn't expose HTTP method in process_request for non-upgrade
        # requests. We infer from content: if there's a body, it's POST.
        # For DELETE, we check the path pattern.
        # Actually, websockets' Request object doesn't have a method field for
        # process_request. We route based on path patterns instead.

        # Order of matching matters — specific paths before general
        # DELETE /trade-api/v2/portfolio/orders/{order_id}
        delete_match = re.match(
            r"/trade-api/v2/portfolio/orders/([a-zA-Z0-9_-]+)$", path_clean
        )

        if path_clean == "/trade-api/v2/portfolio/orders" and request.body:
            return self._handle_create_order(request)
        elif path_clean == "/trade-api/v2/portfolio/orders" and not request.body:
            return self._handle_get_orders(params)
        elif delete_match and not request.body:
            return self._handle_cancel_order(delete_match.group(1))
        elif delete_match and request.body:
            # Could be GET single order (ACK fallback) — check for body
            return self._handle_get_single_order(delete_match.group(1))
        elif path_clean == "/trade-api/v2/portfolio/balance":
            return self._handle_get_balance()
        elif path_clean == "/trade-api/v2/markets":
            return self._handle_get_markets(params)
        elif path_clean == "/trade-api/v2/portfolio/fills":
            return self._handle_get_fills()
        elif path_clean == "/trade-api/v2/portfolio/positions":
            return self._handle_get_positions()
        else:
            return self._json_response(404, {"error": f"Not found: {path_clean}"})

    # ------------------------------------------------------------------
    # REST handlers
    # ------------------------------------------------------------------

    def _handle_get_markets(self, params: dict) -> Response:
        markets = [
            {
                "ticker": TEST_TICKER,
                "event_ticker": TEST_TICKER.rsplit("-", 1)[0],
                "series_ticker": TEST_SERIES,
                "title": TEST_TITLE,
                "status": "open",
                "close_time": TEST_CLOSE_TIME,
                "yes_bid": 0,
                "no_bid": 0,
                "volume_24h": 100,
            }
        ]
        return self._json_response(200, {"markets": markets, "cursor": ""})

    def _handle_get_balance(self) -> Response:
        return self._json_response(200, {"balance": self.balance_cents})

    def _handle_get_orders(self, params: dict) -> Response:
        status_filter = params.get("status", [None])[0]
        orders = []
        for o in self.orders.values():
            if status_filter and o.status != status_filter:
                continue
            orders.append(self._order_to_dict(o))
        return self._json_response(200, {"orders": orders, "cursor": ""})

    def _handle_get_single_order(self, order_id: str) -> Response:
        order = self.orders.get(order_id)
        if not order:
            return self._json_response(404, {"error": "Order not found"})
        return self._json_response(200, {"order": self._order_to_dict(order)})

    def _handle_create_order(self, request) -> Response:
        body = json.loads(request.body)
        order_id = f"ORD-{uuid.uuid4().hex[:8]}"
        order = MockOrder(
            order_id=order_id,
            ticker=body["ticker"],
            side=body.get("side", "yes"),
            action=body.get("action", "buy"),
            order_type=body.get("type", "limit"),
            count=body.get("count", 1),
            status="resting",
            client_order_id=body.get("client_order_id"),
            yes_price=body.get("yes_price"),
            no_price=body.get("no_price"),
            time_in_force=body.get("time_in_force", "good_till_canceled"),
        )

        # Market orders fill immediately
        if order.order_type == "market":
            order.status = "filled"

        self.orders[order_id] = order
        return self._json_response(201, {"order": self._order_to_dict(order)})

    def _handle_cancel_order(self, order_id: str) -> Response:
        order = self.orders.get(order_id)
        if not order:
            return self._json_response(404, {"error": "Order not found"})
        order.status = "canceled"
        return self._json_response(200, {"order": self._order_to_dict(order)})

    def _handle_get_fills(self) -> Response:
        return self._json_response(200, {"fills": self.fills, "cursor": ""})

    def _handle_get_positions(self) -> Response:
        return self._json_response(200, {"market_exposures": []})

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _order_to_dict(order: MockOrder) -> dict:
        d = {
            "order_id": order.order_id,
            "ticker": order.ticker,
            "side": order.side,
            "action": order.action,
            "type": order.order_type,
            "status": order.status,
            "count": order.count,
            "client_order_id": order.client_order_id,
            "time_in_force": order.time_in_force,
        }
        if order.yes_price is not None:
            d["yes_price"] = order.yes_price
        if order.no_price is not None:
            d["no_price"] = order.no_price
        return d

    @staticmethod
    def _json_response(status: int, body: dict) -> Response:
        data = json.dumps(body).encode()
        headers = Headers([
            ("Content-Type", "application/json"),
            ("Content-Length", str(len(data))),
        ])
        reason = "OK" if status < 400 else ("Not Found" if status == 404 else "Error")
        if status == 201:
            reason = "Created"
        return Response(status, reason, headers, data)

    # ------------------------------------------------------------------
    # WebSocket handler (placeholder — implemented in Task 4)
    # ------------------------------------------------------------------

    async def _ws_handler(self, websocket):
        """Handle WebSocket connections. Full implementation in Task 4."""
        self._ws_connections.append(websocket)
        try:
            async for msg in websocket:
                pass  # Subscribe commands handled in Task 4
        finally:
            self._ws_connections.remove(websocket)
```

**Important implementation note for the executor:** The `websockets` 16.0 `process_request` callback receives a `Request` object. The `Request` object does NOT have an HTTP method field — `websockets` is a WebSocket library that only sees the initial HTTP upgrade request. For non-upgrade requests intercepted by `process_request`, we must infer the method from context (presence of body = POST, path pattern with ID and no body = DELETE, otherwise GET). This is a known limitation. Verify by reading the actual `Request` class at `.venv/lib/python3.12/site-packages/websockets/http11.py`.

**If `process_request` doesn't support non-GET HTTP methods:** Fall back to a separate `asyncio.start_server()` TCP handler for REST on a different port. The config has separate `base_url_http` and `base_url_ws` fields, so they can point to different ports. This is the backup plan — try the single-port approach first.

**Step 4: Run the tests**

Run: `cd /home/mike/code/kalshi-trader && uv run pytest tests/mock_exchange/test_server.py -v`
Expected: All 7 tests PASS

**Step 5: Commit**

```bash
git add tests/mock_exchange/server.py tests/mock_exchange/test_server.py
git commit -m "test: mock Kalshi REST server — markets, orders, balance endpoints"
```

---

### Task 3: Review Tasks 1-2 + Milestone

**Trigger:** Both reviewers start simultaneously when Tasks 1-2 complete.

**Spec review — verify these specific items:**
- [ ] `create_dummy_rsa_key()` produces a file that `kalshi_python.api_client.KalshiAuth(key_id, path)` can load without error — test this explicitly by importing `KalshiAuth` and constructing one
- [ ] Mock server's `GET /markets` response includes fields used by `KalshiInstrumentProvider._build_instrument()` at `providers.py:92-141`: `ticker`, `status`, `title`, `close_time`. Missing fields will crash instrument loading.
- [ ] Mock server's `POST /portfolio/orders` response wraps the order in `{"order": {...}}` — the adapter parses `create_data["order"]["order_id"]` at `execution.py:250`
- [ ] Mock server's `GET /portfolio/balance` returns `{"balance": N}` where N is an int in cents — adapter divides by 100 at `execution.py:439`
- [ ] Path routing handles all 8 REST endpoints listed in the Research Findings section

**Code quality review — verify these specific items:**
- [ ] `MockOrder` dataclass fields match the JSON keys the adapter expects in REST responses (check against `execution.py:456-500` for `generate_order_status_reports`)
- [ ] Server startup correctly captures the assigned port when `port=0` is used
- [ ] No resource leaks — temp files cleaned up in tests, server stopped after tests
- [ ] `_json_response` sets `Content-Type: application/json` (SDK may check this)

**Validation Data:**
- Compare mock server's `/markets` response fields against the fields accessed in `providers.py:80-141` (`.ticker`, `.status`, `.title`, `.close_time`)
- Compare mock server's `/portfolio/orders` response fields against fields accessed in `execution.py:461-500` (`.ticker`, `.side`, `.action`, `.status`, `.count`, `.yes_price`, `.no_price`, `.order_id`, `.client_order_id`)

**Resolution:** Findings queue for dev; all must resolve before milestone.

**Milestone — Mock REST server operational:**

**Present to user:**
- Test fixtures (dummy RSA key) working
- Mock REST server handles all required endpoints
- REST response formats match what the adapter expects
- Unit tests verify each endpoint independently

**Wait for user response before proceeding to Task 4.**

---

### Task 4: Implement mock WebSocket server with orderbook publishing and matching engine

**Files:**
- Modify: `tests/mock_exchange/server.py` — extend `_ws_handler` and add matching/publishing logic

**Step 1: Write tests for WebSocket behavior**

```python
# Append to tests/mock_exchange/test_server.py

import websockets


class TestMockWSServer:
    def test_ws_connection_and_subscribe(self, server_url):
        """Connect via WS, send subscribe, receive orderbook snapshot."""
        ws_url = server_url.replace("http://", "ws://") + "/trade-api/ws/v2"

        async def _test():
            async with websockets.connect(ws_url) as ws:
                # Send subscribe
                cmd = {
                    "id": 1,
                    "cmd": "subscribe",
                    "params": {
                        "channels": ["orderbook_delta"],
                        "market_tickers": [TEST_TICKER],
                    },
                }
                await ws.send(json.dumps(cmd))

                # Should receive orderbook snapshot
                raw = await asyncio.wait_for(ws.recv(), timeout=5)
                msg = json.loads(raw)
                assert msg["type"] == "orderbook_snapshot"
                assert msg["msg"]["market_ticker"] == TEST_TICKER

        asyncio.get_event_loop().run_until_complete(_test())

    def test_order_creates_fill_and_ws_notifications(self, server_url):
        """Submit order via REST, receive fill + user_order via WS."""
        ws_url = server_url.replace("http://", "ws://") + "/trade-api/ws/v2"

        async def _test():
            async with websockets.connect(ws_url) as ws:
                # Subscribe to fill + user_order channels
                cmd = {
                    "id": 1,
                    "cmd": "subscribe",
                    "params": {
                        "channels": ["user_orders", "fill"],
                        "market_tickers": [TEST_TICKER],
                    },
                }
                await ws.send(json.dumps(cmd))

                # Create a market order via REST (fills immediately)
                _, data = _post(f"{server_url}/trade-api/v2/portfolio/orders", {
                    "ticker": TEST_TICKER,
                    "action": "buy",
                    "side": "no",
                    "count": 1,
                    "type": "market",
                    "client_order_id": "C-WS-001",
                })
                assert data["order"]["status"] == "filled"

                # Should receive user_order + fill WS messages
                messages = []
                for _ in range(2):
                    raw = await asyncio.wait_for(ws.recv(), timeout=5)
                    messages.append(json.loads(raw))

                types = {m["type"] for m in messages}
                assert "user_order" in types
                assert "fill" in types

        asyncio.get_event_loop().run_until_complete(_test())
```

**Step 2: Run to verify tests fail**

Run: `cd /home/mike/code/kalshi-trader && uv run pytest tests/mock_exchange/test_server.py::TestMockWSServer -v 2>&1 | head -20`
Expected: FAIL — WS handler is a placeholder

**Step 3: Implement WebSocket handler, orderbook publishing, and matching engine**

Extend `MockKalshiServer` in `server.py` with:

1. **`_ws_handler`**: Parse subscribe commands, track subscribed channels per connection, send initial orderbook snapshot on subscribe.

2. **Orderbook state**: Maintain YES/NO bid levels per ticker. Initial state comes from the scenario (set externally). Default: NO bid=35c (size=100), YES bid=58c (size=100).

3. **`publish_orderbook_delta(ticker, side, price, delta)`**: Send `orderbook_delta` message to all subscribed WS connections. Sequence numbers auto-increment per SID.

4. **`publish_orderbook_snapshot(ticker, ws)`**: Send full snapshot to a specific connection on subscribe.

5. **Matching engine in `_handle_create_order`**: When an order is created:
   - Market orders: fill immediately at current ask. Create fill record. Send `user_order` (status=filled) and `fill` WS messages.
   - Limit orders: if limit price ≥ ask (buy) or ≤ bid (sell), fill immediately. Otherwise, rest.
   - Send `user_order` WS message (status=resting or filled).

6. **`check_resting_orders()`**: Called after each orderbook update. Check if any resting limit orders now cross the ask/bid. If so, fill them and send WS notifications.

Key implementation details for WS messages (must match `websockets/types.py` schemas):

```python
# Orderbook snapshot envelope
{
    "type": "orderbook_snapshot",
    "sid": 10,  # arbitrary SID for orderbook channel
    "seq": N,
    "msg": {
        "market_ticker": "KXHIGHCHI-26MAR15-T42",
        "yes_dollars_fp": [["0.58", "100"]],  # [[price_str, size_str], ...]
        "no_dollars_fp": [["0.35", "100"]],
    }
}

# Orderbook delta envelope
{
    "type": "orderbook_delta",
    "sid": 10,
    "seq": N,
    "msg": {
        "market_ticker": "KXHIGHCHI-26MAR15-T42",
        "price_dollars": "0.35",
        "delta_fp": "50",  # new size at this level (0 = remove)
        "side": "no",
    }
}

# User order envelope
{
    "type": "user_order",
    "sid": 1,
    "seq": N,
    "msg": {
        "order_id": "ORD-xxx",
        "ticker": "KXHIGHCHI-26MAR15-T42",
        "status": "resting",  # or "filled", "canceled"
        "side": "no",
        "action": "buy",
        "client_order_id": "C-xxx",
    }
}

# Fill envelope
{
    "type": "fill",
    "sid": 2,
    "seq": N,
    "msg": {
        "trade_id": "TRD-xxx",
        "order_id": "ORD-xxx",
        "market_ticker": "KXHIGHCHI-26MAR15-T42",
        "side": "no",
        "action": "buy",
        "count_fp": "1.00",
        "is_taker": true,
        "ts": 1000000000,
        "client_order_id": "C-xxx",
        "yes_price_dollars": "0.58",
        "no_price_dollars": "0.42",
    }
}
```

**Matching engine price logic:**
- Current NO ask = 1.0 - max(YES bids) (mirrors adapter's `_derive_quotes`)
- BUY NO limit fills when: NO ask ≤ limit_no_price / 100
- BUY YES limit fills when: YES ask ≤ limit_yes_price / 100
- Market BUY NO fills at current NO ask
- Market BUY YES fills at current YES ask

**Step 4: Run the tests**

Run: `cd /home/mike/code/kalshi-trader && uv run pytest tests/mock_exchange/test_server.py -v`
Expected: All tests PASS (both REST and WS tests)

**Step 5: Commit**

```bash
git add tests/mock_exchange/server.py tests/mock_exchange/test_server.py
git commit -m "test: mock WebSocket server with orderbook publishing and matching engine"
```

---

### Task 5: Implement scenario definitions

**Files:**
- Create: `tests/mock_exchange/scenarios.py`

**Step 1: Define the scenario data structure and 4 scenarios**

```python
# tests/mock_exchange/scenarios.py
"""Deterministic price scripts for confidence test scenarios.

Each scenario defines a sequence of orderbook states. The mock server
publishes these as orderbook_delta messages at specified intervals.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class BookUpdate:
    """A single orderbook state change."""
    delay_ms: int  # milliseconds to wait before publishing
    no_bid_cents: int  # best NO bid in cents
    yes_bid_cents: int  # best YES bid in cents
    no_bid_size: int = 100  # size at best NO bid
    yes_bid_size: int = 100  # size at best YES bid


@dataclass(frozen=True)
class Scenario:
    name: str
    description: str
    initial_no_bid_cents: int  # starting NO bid
    initial_yes_bid_cents: int  # starting YES bid
    updates: list[BookUpdate]
    # Expected outcomes
    expect_market_fill: bool = True
    expect_limit_fill: bool = False
    expect_limit_fill_price_cents: int | None = None
    expect_cancel_count: int = 0


STEADY_FILL = Scenario(
    name="steady_fill",
    description="NO bid sits at 35c. Limit at 34c. YES bid rises to 66c → NO ask drops to 34c → limit fills.",
    initial_no_bid_cents=35,
    initial_yes_bid_cents=58,
    updates=[
        BookUpdate(delay_ms=200, no_bid_cents=35, yes_bid_cents=60),
        BookUpdate(delay_ms=200, no_bid_cents=35, yes_bid_cents=63),
        BookUpdate(delay_ms=200, no_bid_cents=35, yes_bid_cents=66),  # NO ask = 34c → fills limit
    ],
    expect_market_fill=True,
    expect_limit_fill=True,
    expect_limit_fill_price_cents=34,
)

CHASE_UP = Scenario(
    name="chase_up",
    description="NO bid walks 30→35c. Strategy chases with limit at bid-1c. Bid reverses → limit fills.",
    initial_no_bid_cents=30,
    initial_yes_bid_cents=58,
    updates=[
        BookUpdate(delay_ms=200, no_bid_cents=31, yes_bid_cents=58),
        BookUpdate(delay_ms=200, no_bid_cents=32, yes_bid_cents=58),
        BookUpdate(delay_ms=200, no_bid_cents=33, yes_bid_cents=58),
        BookUpdate(delay_ms=200, no_bid_cents=34, yes_bid_cents=58),
        BookUpdate(delay_ms=200, no_bid_cents=35, yes_bid_cents=58),
        # Bid reverses — YES bid rises so NO ask drops to match last limit
        BookUpdate(delay_ms=200, no_bid_cents=34, yes_bid_cents=66),  # NO ask = 34c → fills
    ],
    expect_market_fill=True,
    expect_limit_fill=True,
    expect_limit_fill_price_cents=34,
    expect_cancel_count=5,  # cancel + re-place at each bid change
)

MARKET_ORDER_IMMEDIATE = Scenario(
    name="market_order_immediate",
    description="Market order fills immediately at NO ask. No limit order behavior tested.",
    initial_no_bid_cents=35,
    initial_yes_bid_cents=58,
    updates=[],  # No book changes needed — market order fills on initial state
    expect_market_fill=True,
    expect_limit_fill=False,
)

NO_FILL_TIMEOUT = Scenario(
    name="no_fill_timeout",
    description="Limit at 34c but NO ask never drops below 40c. Limit stays resting.",
    initial_no_bid_cents=35,
    initial_yes_bid_cents=58,
    updates=[
        BookUpdate(delay_ms=200, no_bid_cents=36, yes_bid_cents=58),  # NO ask = 42c
        BookUpdate(delay_ms=200, no_bid_cents=37, yes_bid_cents=57),  # NO ask = 43c
        BookUpdate(delay_ms=200, no_bid_cents=38, yes_bid_cents=56),  # NO ask = 44c — moving away
    ],
    expect_market_fill=True,
    expect_limit_fill=False,
    expect_cancel_count=3,  # chases bid up: 35→36→37
)

ALL_SCENARIOS = [STEADY_FILL, CHASE_UP, MARKET_ORDER_IMMEDIATE, NO_FILL_TIMEOUT]
```

**Step 2: Write a sanity test for scenario invariants**

```python
# tests/mock_exchange/test_scenarios.py
from tests.mock_exchange.scenarios import ALL_SCENARIOS


class TestScenarioInvariants:
    def test_all_scenarios_have_valid_prices(self):
        for s in ALL_SCENARIOS:
            assert 1 <= s.initial_no_bid_cents <= 99, f"{s.name}: invalid initial NO bid"
            assert 1 <= s.initial_yes_bid_cents <= 99, f"{s.name}: invalid initial YES bid"
            assert s.initial_no_bid_cents + s.initial_yes_bid_cents <= 100, (
                f"{s.name}: YES+NO bids exceed 100c"
            )
            for u in s.updates:
                assert 1 <= u.no_bid_cents <= 99
                assert 1 <= u.yes_bid_cents <= 99

    def test_all_scenarios_have_unique_names(self):
        names = [s.name for s in ALL_SCENARIOS]
        assert len(names) == len(set(names))
```

**Step 3: Run tests**

Run: `cd /home/mike/code/kalshi-trader && uv run pytest tests/mock_exchange/test_scenarios.py -v`
Expected: PASS

**Step 4: Commit**

```bash
git add tests/mock_exchange/scenarios.py tests/mock_exchange/test_scenarios.py
git commit -m "test: scenario definitions — 4 deterministic price scripts for confidence tests"
```

---

### Task 6: Review Tasks 4-5 + Milestone

**Trigger:** Both reviewers start simultaneously when Tasks 4-5 complete.

**Spec review — verify these specific items:**
- [ ] WS `orderbook_snapshot` message format matches `OrderbookSnapshotMsg` schema in `websocket/types.py:10-14`: fields `market_ticker`, `yes_dollars_fp` (list of `[price_str, size_str]`), `no_dollars_fp`
- [ ] WS `orderbook_delta` message format matches `OrderbookDeltaMsg` schema in `websocket/types.py:17-21`: fields `market_ticker`, `price_dollars`, `delta_fp`, `side`
- [ ] WS `fill` message format matches `FillMsg` schema in `websocket/types.py:40-53`: includes `trade_id`, `order_id`, `market_ticker`, `side`, `action`, `count_fp`, `is_taker`, `ts`, and optional `client_order_id`, `yes_price_dollars`, `no_price_dollars`
- [ ] WS `user_order` message format matches `UserOrderMsg` schema in `websocket/types.py:24-37`: includes `order_id`, `ticker`, `status`, `side`, `action`, and optional `client_order_id`
- [ ] Matching engine uses same price derivation as adapter: NO ask = 1.0 - max(YES bids) — compare with `data.py:43`
- [ ] SID values for WS channels are consistent across all messages (SID 1 = user_order, SID 2 = fill — matching `ws_factory.py:13-16`)
- [ ] Sequence numbers increment per SID, not globally
- [ ] Scenario `steady_fill` correctly ends with NO ask = 34c when YES bid = 66c (100 - 66 = 34)
- [ ] Scenario `chase_up` cancel count matches the number of bid changes before the fill

**Code quality review — verify these specific items:**
- [ ] No race conditions between REST order creation and WS message delivery — the order must exist before WS notifications reference it
- [ ] Matching engine correctly handles the case where a limit order is placed and the current ask already crosses it (immediate fill, not resting)
- [ ] Scenario data is immutable (frozen dataclass) — no accidental mutation between test runs
- [ ] WS connections are properly cleaned up when clients disconnect

**Validation Data:**
- Decode a mock `orderbook_snapshot` message through the production `decode_ws_msg()` function and verify it produces an `OrderbookSnapshotMsg` with correct field values
- Decode a mock `fill` message through `decode_ws_msg()` and verify it produces a `FillMsg`

**Resolution:** Findings queue for dev; all must resolve before milestone.

**Milestone — Mock exchange handles full order lifecycle:**

**Present to user:**
- Mock WebSocket server accepts connections and responds to subscribe commands
- Orderbook snapshots and deltas published to subscribed clients
- Matching engine fills market orders immediately and limit orders on price cross
- 4 deterministic scenarios defined with clear expected outcomes
- WS message formats validated against production decoder

**Wait for user response before proceeding to Task 7.**

---

### Task 7: Implement confidence test strategy

**Files:**
- Create: `tests/mock_exchange/strategy.py`

**Step 1: Write the test strategy**

The strategy:
1. On start: subscribes to quotes for the NO side of the test ticker
2. On first quote: places a market buy NO order + a limit buy NO order at (NO bid - 1c)
3. On subsequent quotes: if NO bid changed, cancels existing limit order and places new one at (NO bid - 1c)
4. Tracks all events (fills, accepts, cancels) in lists for assertion
5. Signals completion when: (a) limit fills, OR (b) scenario updates are exhausted + grace period

```python
# tests/mock_exchange/strategy.py
"""Confidence test strategy — places market + limit orders, chases bid by 1c."""
from __future__ import annotations

import asyncio
from decimal import Decimal

from nautilus_trader.model.data import QuoteTick
from nautilus_trader.model.enums import OrderSide, TimeInForce
from nautilus_trader.model.identifiers import InstrumentId, Symbol
from nautilus_trader.model.objects import Price, Quantity
from nautilus_trader.trading.strategy import Strategy

from kalshi.common.constants import KALSHI_VENUE


class ConfidenceTestStrategy(Strategy):
    """Minimal strategy for adapter confidence testing.

    Places a market order on first quote, then a limit order at bid - 1c.
    Chases the bid by canceling and re-placing the limit on each quote update.
    Records all events for post-test assertion.
    """

    def __init__(self, ticker: str, done_callback=None):
        super().__init__()
        self._ticker = ticker
        self._no_instrument_id = InstrumentId(
            Symbol(f"{ticker}-NO"), KALSHI_VENUE,
        )
        self._done_callback = done_callback

        # State
        self._market_order_placed = False
        self._current_limit_client_oid = None
        self._current_limit_venue_oid = None
        self._last_no_bid_cents: int | None = None
        self._limit_order_count = 0

        # Event tracking for assertions
        self.accepted_orders: list = []
        self.filled_orders: list = []
        self.canceled_orders: list = []
        self.rejected_orders: list = []
        self.quotes_received: list = []
        self.done = False

    def on_start(self):
        self.subscribe_quote_ticks(self._no_instrument_id)
        self.log.info(f"Subscribed to {self._no_instrument_id}")

    def on_quote_tick(self, tick: QuoteTick):
        self.quotes_received.append(tick)
        no_bid_cents = int(round(float(tick.bid_price) * 100))

        # Place market order on first quote
        if not self._market_order_placed:
            self._market_order_placed = True
            self._place_market_order()

            # Also place initial limit at bid - 1c
            if no_bid_cents > 1:
                self._place_limit_order(no_bid_cents - 1)
                self._last_no_bid_cents = no_bid_cents
            return

        # Chase bid: if bid changed, cancel old limit and place new one
        if no_bid_cents != self._last_no_bid_cents and no_bid_cents > 1:
            self._cancel_and_replace_limit(no_bid_cents - 1)
            self._last_no_bid_cents = no_bid_cents

    def _place_market_order(self):
        from nautilus_trader.model.orders import MarketOrder
        order = self.order_factory.market(
            instrument_id=self._no_instrument_id,
            order_side=OrderSide.BUY,
            quantity=Quantity.from_int(1),
            time_in_force=TimeInForce.FOK,
        )
        self.submit_order(order)
        self.log.info(f"Market order submitted: {order.client_order_id}")

    def _place_limit_order(self, price_cents: int):
        from nautilus_trader.model.orders import LimitOrder
        order = self.order_factory.limit(
            instrument_id=self._no_instrument_id,
            order_side=OrderSide.BUY,
            quantity=Quantity.from_int(1),
            price=Price(Decimal(f"{price_cents / 100:.2f}"), precision=2),
            time_in_force=TimeInForce.GTC,
        )
        self._current_limit_client_oid = order.client_order_id
        self._limit_order_count += 1
        self.submit_order(order)
        self.log.info(f"Limit order at {price_cents}c: {order.client_order_id}")

    def _cancel_and_replace_limit(self, new_price_cents: int):
        if self._current_limit_client_oid:
            order = self.cache.order(self._current_limit_client_oid)
            if order and order.is_open:
                self.cancel_order(order)
        self._place_limit_order(new_price_cents)

    def on_order_accepted(self, event):
        self.accepted_orders.append(event)

    def on_order_filled(self, event):
        self.filled_orders.append(event)
        self.log.info(f"Fill: {event.client_order_id} at {event.last_px}")

        # Check if this was a limit fill (not the market order)
        # Market order is the first one placed; limit fills come after
        if len(self.filled_orders) >= 2:
            self._signal_done()

    def on_order_canceled(self, event):
        self.canceled_orders.append(event)

    def on_order_rejected(self, event):
        self.rejected_orders.append(event)
        self.log.warning(f"Rejected: {event.client_order_id} — {event.reason}")

    def _signal_done(self):
        if not self.done:
            self.done = True
            if self._done_callback:
                self._done_callback()

    def on_stop(self):
        self.log.info(
            f"Strategy done. Fills={len(self.filled_orders)}, "
            f"Cancels={len(self.canceled_orders)}, "
            f"Quotes={len(self.quotes_received)}"
        )
```

**Step 2: Write a unit test for the strategy logic (without TradingNode)**

This test uses the existing SimpleNamespace + MethodType pattern from `test_scenarios.py` to verify the strategy's decision logic in isolation, before wiring it to the full node.

```python
# tests/mock_exchange/test_strategy_logic.py
"""Unit tests for ConfidenceTestStrategy decision logic."""
from unittest.mock import MagicMock

from tests.mock_exchange.strategy import ConfidenceTestStrategy
from tests.mock_exchange.fixtures import TEST_TICKER


def test_strategy_init():
    s = ConfidenceTestStrategy(ticker=TEST_TICKER)
    assert s._ticker == TEST_TICKER
    assert not s._market_order_placed
    assert s.done is False
    assert s.filled_orders == []
```

**Step 3: Run the test**

Run: `cd /home/mike/code/kalshi-trader && uv run pytest tests/mock_exchange/test_strategy_logic.py -v`
Expected: PASS

**Step 4: Commit**

```bash
git add tests/mock_exchange/strategy.py tests/mock_exchange/test_strategy_logic.py
git commit -m "test: confidence test strategy — market + limit orders, bid chasing"
```

---

### Task 8: Implement TradingNode bootstrap and scenario test runner

**Files:**
- Create: `tests/test_live_confidence.py`
- Modify: `tests/mock_exchange/server.py` — add `run_scenario()` method

**Step 1: Add scenario runner to MockKalshiServer**

Add a method `run_scenario(scenario)` that publishes orderbook updates according to the scenario's price script:

```python
# In MockKalshiServer:
async def run_scenario(self, scenario: Scenario) -> None:
    """Publish orderbook updates from a scenario's price script."""
    for update in scenario.updates:
        await asyncio.sleep(update.delay_ms / 1000)
        # Update YES bids
        await self._publish_orderbook_delta(
            ticker=TEST_TICKER,
            side="yes",
            price_dollars=f"{update.yes_bid_cents / 100:.2f}",
            delta_fp=str(update.yes_bid_size),
        )
        # Update NO bids
        await self._publish_orderbook_delta(
            ticker=TEST_TICKER,
            side="no",
            price_dollars=f"{update.no_bid_cents / 100:.2f}",
            delta_fp=str(update.no_bid_size),
        )
        # Check if any resting orders now fill
        self._check_resting_orders()
```

**Step 2: Write the integration test runner**

```python
# tests/test_live_confidence.py
"""End-to-end confidence tests: real TradingNode + mock Kalshi exchange.

Each test boots a TradingNode with the production adapter factories,
connects to a local mock server, runs a test strategy through a
deterministic price scenario, and asserts the adapter correctly
handled quotes, orders, fills, and positions.

Run: uv run pytest tests/test_live_confidence.py -v
"""
import asyncio
import os
import threading
import time

import pytest

from nautilus_trader.live.node import TradingNode
from nautilus_trader.config import TradingNodeConfig

from kalshi.config import KalshiDataClientConfig, KalshiExecClientConfig
from kalshi.factories import (
    KalshiLiveDataClientFactory,
    KalshiLiveExecClientFactory,
    _SHARED_PROVIDER,
)
import kalshi.factories as factories_module

from tests.mock_exchange.fixtures import create_dummy_rsa_key, TEST_TICKER
from tests.mock_exchange.server import MockKalshiServer
from tests.mock_exchange.strategy import ConfidenceTestStrategy
from tests.mock_exchange.scenarios import (
    ALL_SCENARIOS,
    STEADY_FILL,
    CHASE_UP,
    MARKET_ORDER_IMMEDIATE,
    NO_FILL_TIMEOUT,
    Scenario,
)


def _start_mock_server() -> tuple[MockKalshiServer, threading.Thread, asyncio.AbstractEventLoop]:
    """Start mock server in a daemon thread. Returns (server, thread, loop)."""
    server = MockKalshiServer()
    loop = asyncio.new_event_loop()

    def run():
        asyncio.set_event_loop(loop)
        loop.run_until_complete(server.start(port=0))
        loop.run_forever()

    t = threading.Thread(target=run, daemon=True)
    t.start()

    # Wait for port assignment
    for _ in range(100):
        if server.port is not None:
            break
        time.sleep(0.05)
    assert server.port is not None, "Mock server failed to start"
    return server, t, loop


def _run_scenario_test(scenario: Scenario):
    """Boot TradingNode, run scenario, return strategy for assertions."""
    # Create dummy RSA key
    key_path = create_dummy_rsa_key()

    # Start mock server
    server, server_thread, server_loop = _start_mock_server()

    # Reset shared provider singleton (each test needs fresh state)
    factories_module._SHARED_PROVIDER = None

    # Set initial book state on server
    server.set_initial_book(
        TEST_TICKER,
        no_bid_cents=scenario.initial_no_bid_cents,
        yes_bid_cents=scenario.initial_yes_bid_cents,
    )

    base_http = f"http://localhost:{server.port}/trade-api/v2"
    base_ws = f"ws://localhost:{server.port}/trade-api/ws/v2"

    try:
        # Configure TradingNode
        config = TradingNodeConfig(
            trader_id="CONFIDENCE-TEST",
            data_clients={
                "KALSHI": KalshiDataClientConfig(
                    api_key_id="test-key",
                    private_key_path=key_path,
                    base_url_http=base_http,
                    base_url_ws=base_ws,
                ),
            },
            exec_clients={
                "KALSHI": KalshiExecClientConfig(
                    api_key_id="test-key",
                    private_key_path=key_path,
                    base_url_http=base_http,
                    base_url_ws=base_ws,
                    ack_timeout_secs=2.0,
                ),
            },
            timeout_connection=10_000,  # ms
            timeout_disconnection=5_000,
        )

        node = TradingNode(config=config)
        node.add_data_client_factory("KALSHI", KalshiLiveDataClientFactory)
        node.add_exec_client_factory("KALSHI", KalshiLiveExecClientFactory)

        # Create strategy with done callback that stops the node
        strategy = ConfidenceTestStrategy(
            ticker=TEST_TICKER,
            done_callback=lambda: node.stop(),
        )
        node.add_strategy(strategy)
        node.build()

        # Schedule scenario on mock server's event loop
        async def _run_on_server():
            # Wait for connections to establish
            await asyncio.sleep(1.0)
            await server.run_scenario(scenario)
            # Grace period for strategy to process final events
            await asyncio.sleep(2.0)
            # If strategy hasn't signaled done, stop anyway
            if not strategy.done:
                node.stop()

        asyncio.run_coroutine_threadsafe(_run_on_server(), server_loop)

        # Run node (blocks until stop)
        node.run()

        return strategy

    finally:
        # Cleanup
        server_loop.call_soon_threadsafe(server_loop.stop)
        server_thread.join(timeout=5)
        os.unlink(key_path)
        factories_module._SHARED_PROVIDER = None


class TestSteadyFill:
    def test_market_and_limit_orders_fill(self):
        strategy = _run_scenario_test(STEADY_FILL)
        # Market order should have filled
        assert len(strategy.filled_orders) >= 1, "Market order did not fill"
        # Limit order should also have filled
        assert len(strategy.filled_orders) >= 2, "Limit order did not fill"
        assert strategy.quotes_received, "No quotes received"


class TestChaseUp:
    def test_limit_chases_bid_and_fills(self):
        strategy = _run_scenario_test(CHASE_UP)
        assert len(strategy.filled_orders) >= 2, "Expected market + limit fill"
        assert len(strategy.canceled_orders) >= 3, (
            f"Expected ≥3 cancels for bid chasing, got {len(strategy.canceled_orders)}"
        )


class TestMarketOrderImmediate:
    def test_market_order_fills_on_first_quote(self):
        strategy = _run_scenario_test(MARKET_ORDER_IMMEDIATE)
        assert len(strategy.filled_orders) >= 1, "Market order did not fill"


class TestNoFillTimeout:
    def test_limit_stays_resting(self):
        strategy = _run_scenario_test(NO_FILL_TIMEOUT)
        assert len(strategy.filled_orders) == 1, (
            f"Expected only market fill, got {len(strategy.filled_orders)} fills"
        )
        assert not strategy.done, "Strategy should not have signaled done"
```

**Important implementation notes for the executor:**

1. **`TradingNodeConfig` constructor**: The exact kwargs may differ in NT 1.224+. Read the actual `TradingNodeConfig` class definition to verify field names (`timeout_connection` vs `connect_timeout_ms`, etc.). Check `.venv/lib/python3.12/site-packages/nautilus_trader/config/`.

2. **Factory singleton reset**: `factories_module._SHARED_PROVIDER = None` must be called between tests to prevent stale state. Each test needs its own instrument provider pointing to the current mock server port.

3. **`node.add_strategy(strategy)`**: In NT 1.224+, the API may be `node.trader.add_strategy(strategy)` (see `archive/main.py:80`). Check the actual `TradingNode` class to confirm.

4. **`node.stop()` from callback**: The `done_callback` is called from within the strategy's event handler, which runs on the NT event loop. `node.stop()` should be safe to call from this context, but verify.

5. **If `process_request` can't handle POST/DELETE**: The mock server may need to run REST on a separate port. Update `base_url_http` to point to the REST port and `base_url_ws` to the WS port.

**Step 3: Run the tests**

Run: `cd /home/mike/code/kalshi-trader && uv run pytest tests/test_live_confidence.py -v --timeout=60`
Expected: All 4 scenario tests PASS

**Step 4: Commit**

```bash
git add tests/test_live_confidence.py tests/mock_exchange/server.py
git commit -m "test: end-to-end confidence tests — TradingNode + mock exchange, 4 scenarios"
```

---

### Task 9: Review Tasks 7-8 + Final Milestone

**Trigger:** Both reviewers start simultaneously when Tasks 7-8 complete.

**Spec review — verify these specific items:**
- [ ] Strategy subscribes to the NO instrument (not YES) — ticker format is `{ticker}-NO` matching the `InstrumentId` pattern in `providers.py:95-97`
- [ ] Strategy uses `order_factory.market()` and `order_factory.limit()` (NT's built-in order factory), not custom order construction
- [ ] Strategy's `on_order_filled` correctly distinguishes market fill from limit fill (market is first, limit is second)
- [ ] `_cancel_and_replace_limit` checks `order.is_open` before canceling — prevents canceling already-filled or already-canceled orders
- [ ] TradingNode config uses `base_url_http` and `base_url_ws` fields (not `rest_url`/`ws_url` which are derived properties)
- [ ] Integration test asserts SPECIFIC values: fill count, cancel count, quote receipt — not just "no errors"
- [ ] `MARKET_ORDER_IMMEDIATE` scenario still places a limit order (the strategy always does on first quote) — the assertion only checks that the market order filled, but the limit order should be resting
- [ ] `NO_FILL_TIMEOUT` correctly expects exactly 1 fill (market only) — verify the limit order stays resting by checking cache

**Code quality review — verify these specific items:**
- [ ] No timing-dependent flakiness: delays in scenarios are long enough for the adapter to process events (200ms minimum between updates)
- [ ] Factory singleton (`_SHARED_PROVIDER`) is properly reset between tests — stale provider will cause port mismatch
- [ ] Mock server cleanup is guaranteed even on test failure (try/finally in `_run_scenario_test`)
- [ ] Strategy doesn't import anything from the mock server (it's a production-style strategy, not coupled to test infrastructure)
- [ ] Temporary RSA key file is deleted on cleanup

**Validation Data:**
- For `STEADY_FILL`: verify `strategy.filled_orders[1].last_px` equals `Price("0.34")` (34 cents)
- For `CHASE_UP`: verify `strategy.canceled_orders` count is ≥ 3 (bid moved 5 times, but first limit doesn't get canceled until second quote)
- For `NO_FILL_TIMEOUT`: verify `len(strategy.filled_orders) == 1` and the single fill is from the market order

**Resolution:** Findings queue for dev; all must resolve before final milestone.

**Final Milestone — All 4 scenarios pass end-to-end:**

**Present to user:**
- Complete coverage matrix (as specified in the design doc) validated
- All 4 scenarios execute and pass assertions:
  - `steady_fill`: market + limit fill
  - `chase_up`: bid chasing with cancels → eventual fill
  - `market_order_immediate`: instant market fill
  - `no_fill_timeout`: market fill, limit stays resting
- Real TradingNode boots with production factories
- Real adapter code exercises: REST serialization, WS subscription, quote derivation, order routing, fill handling, position tracking
- No modifications to production code

**Wait for user response.**
