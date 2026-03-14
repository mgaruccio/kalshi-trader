"""Tests for mock Kalshi REST server."""
import asyncio
import json
import time
import threading
import urllib.request
import urllib.error

import pytest
import websockets

from tests.mock_exchange.fixtures import TEST_TICKER
from tests.mock_exchange.server import MockKalshiServer


def _start_server(server: MockKalshiServer, **start_kwargs):
    """Start server in a daemon thread. Returns (loop, thread)."""
    loop = asyncio.new_event_loop()

    def run():
        loop.run_until_complete(server.start(**start_kwargs))
        loop.run_forever()

    t = threading.Thread(target=run, daemon=True)
    t.start()
    return loop, t


def _stop_server(server: MockKalshiServer, loop, t):
    asyncio.run_coroutine_threadsafe(server.stop(), loop).result(timeout=2)
    loop.call_soon_threadsafe(loop.stop)
    t.join(timeout=2)


@pytest.fixture
def server_url():
    """Start mock server (HTTP only), yield base URL, stop after test."""
    server = MockKalshiServer()
    loop, t = _start_server(server, port=0)

    for _ in range(50):
        if server.port is not None:
            break
        time.sleep(0.05)
    assert server.port is not None, "Server failed to start"

    yield f"http://127.0.0.1:{server.port}"

    _stop_server(server, loop, t)


@pytest.fixture
def server_both():
    """Start mock server (HTTP + WS), yield (rest_url, ws_url, server)."""
    server = MockKalshiServer()
    loop, t = _start_server(server, port=0, ws_port=0)

    for _ in range(50):
        if server.port is not None and server.ws_port is not None:
            break
        time.sleep(0.05)
    assert server.port is not None, "HTTP server failed to start"
    assert server.ws_port is not None, "WS server failed to start"

    yield (
        f"http://127.0.0.1:{server.port}",
        f"ws://127.0.0.1:{server.ws_port}",
        server,
    )

    _stop_server(server, loop, t)


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
        assert data["balance"] == 100_000

    def test_get_orders_empty(self, server_url):
        data = _get(f"{server_url}/trade-api/v2/portfolio/orders?status=resting")
        assert data["orders"] == []

    def test_create_order_returns_order_id(self, server_url):
        status, data = _post(f"{server_url}/trade-api/v2/portfolio/orders", {
            "ticker": TEST_TICKER, "action": "buy", "side": "no",
            "count": 5, "type": "limit", "time_in_force": "good_till_canceled",
            "client_order_id": "C-TEST-001", "no_price": 34,
        })
        assert status == 201
        assert "order" in data
        assert data["order"]["order_id"]
        assert data["order"]["status"] == "resting"

    def test_cancel_order(self, server_url):
        _, create_data = _post(f"{server_url}/trade-api/v2/portfolio/orders", {
            "ticker": TEST_TICKER, "action": "buy", "side": "no",
            "count": 1, "type": "limit", "time_in_force": "good_till_canceled",
            "client_order_id": "C-CANCEL-001", "no_price": 10,
        })
        order_id = create_data["order"]["order_id"]
        req = urllib.request.Request(
            f"{server_url}/trade-api/v2/portfolio/orders/{order_id}",
            method="DELETE",
        )
        with urllib.request.urlopen(req) as resp:
            assert resp.status == 200
        # Re-fetch and verify status is canceled
        refetch = _get(f"{server_url}/trade-api/v2/portfolio/orders/{order_id}")
        assert refetch["order"]["status"] == "canceled"

    def test_get_fills_empty(self, server_url):
        data = _get(f"{server_url}/trade-api/v2/portfolio/fills")
        assert data["fills"] == []

    def test_get_positions_empty(self, server_url):
        data = _get(f"{server_url}/trade-api/v2/portfolio/positions")
        assert data["market_exposures"] == []

    def test_batch_cancel_all_orders(self, server_url):
        # Create two resting orders
        _, d1 = _post(f"{server_url}/trade-api/v2/portfolio/orders", {
            "ticker": TEST_TICKER, "action": "buy", "side": "no",
            "count": 1, "type": "limit", "time_in_force": "good_till_canceled",
            "client_order_id": "C-BATCH-001", "no_price": 10,
        })
        _, d2 = _post(f"{server_url}/trade-api/v2/portfolio/orders", {
            "ticker": TEST_TICKER, "action": "buy", "side": "no",
            "count": 1, "type": "limit", "time_in_force": "good_till_canceled",
            "client_order_id": "C-BATCH-002", "no_price": 11,
        })
        # Batch cancel
        req = urllib.request.Request(
            f"{server_url}/trade-api/v2/portfolio/orders/batched",
            method="DELETE",
        )
        with urllib.request.urlopen(req) as resp:
            assert resp.status == 200
        # Both orders should now be canceled
        orders = _get(f"{server_url}/trade-api/v2/portfolio/orders?status=resting")
        assert orders["orders"] == []


class TestMockWebSocketServer:
    def test_ws_connection_and_subscribe(self, server_both):
        rest_url, ws_url, server = server_both
        server.set_initial_book(TEST_TICKER, no_bid_cents=42, yes_bid_cents=55)

        async def _run():
            async with websockets.connect(ws_url) as ws:
                await ws.send(json.dumps({
                    "id": 1, "cmd": "subscribe",
                    "params": {
                        "channels": ["orderbook_delta"],
                        "market_tickers": [TEST_TICKER],
                    },
                }))
                msg = json.loads(await asyncio.wait_for(ws.recv(), timeout=2.0))

            assert msg["type"] == "orderbook_snapshot"
            assert msg["sid"] == 10
            inner = msg["msg"]
            assert inner["market_ticker"] == TEST_TICKER
            yes_prices = [float(lvl[0]) for lvl in inner["yes_dollars_fp"]]
            no_prices = [float(lvl[0]) for lvl in inner["no_dollars_fp"]]
            assert 0.55 in yes_prices
            assert 0.42 in no_prices

        asyncio.run(_run())

    def test_order_creates_fill_and_ws_notifications(self, server_both):
        rest_url, ws_url, server = server_both
        # YES bid=0.55 → NO ask = 1.0 - 0.55 = 0.45
        server.set_initial_book(TEST_TICKER, no_bid_cents=42, yes_bid_cents=55)

        async def _run():
            async with websockets.connect(ws_url) as ws:
                # Subscribe to all channels; snapshot acts as sync barrier
                await ws.send(json.dumps({
                    "id": 1, "cmd": "subscribe",
                    "params": {
                        "channels": ["orderbook_delta", "user_orders", "fill"],
                        "market_tickers": [TEST_TICKER],
                    },
                }))
                snapshot = json.loads(await asyncio.wait_for(ws.recv(), timeout=2.0))
                assert snapshot["type"] == "orderbook_snapshot"

                # Submit market BUY NO in thread to avoid blocking the event loop
                status, data = await asyncio.to_thread(
                    _post,
                    f"{rest_url}/trade-api/v2/portfolio/orders",
                    {
                        "ticker": TEST_TICKER, "action": "buy", "side": "no",
                        "count": 1, "type": "market",
                        "time_in_force": "fill_or_kill",
                        "client_order_id": "C-MKT-001",
                    },
                )
                assert status == 201
                assert data["order"]["status"] == "filled"

                # Collect next two WS messages (user_order + fill, order may vary)
                m1 = json.loads(await asyncio.wait_for(ws.recv(), timeout=2.0))
                m2 = json.loads(await asyncio.wait_for(ws.recv(), timeout=2.0))

            types = {m1["type"], m2["type"]}
            assert types == {"user_order", "fill"}

            user_msg = m1 if m1["type"] == "user_order" else m2
            fill_msg = m1 if m1["type"] == "fill" else m2

            assert user_msg["msg"]["status"] == "filled"
            assert user_msg["msg"]["client_order_id"] == "C-MKT-001"
            assert fill_msg["msg"]["count_fp"] == "1"
            assert fill_msg["msg"]["client_order_id"] == "C-MKT-001"
            assert fill_msg["msg"]["market_ticker"] == TEST_TICKER
            # Fill price should be NO ask = 0.45
            assert abs(float(fill_msg["msg"]["no_price_dollars"]) - 0.45) < 0.001

        asyncio.run(_run())
