"""Tests for mock Kalshi REST server."""
import asyncio
import json
import time
import threading
import urllib.request
import urllib.error

import pytest

from tests.mock_exchange.fixtures import TEST_TICKER
from tests.mock_exchange.server import MockKalshiServer


@pytest.fixture
def server_url():
    """Start mock server, yield base URL, stop after test."""
    server = MockKalshiServer()
    loop = asyncio.new_event_loop()

    def run():
        loop.run_until_complete(server.start(port=0))
        loop.run_forever()

    t = threading.Thread(target=run, daemon=True)
    t.start()

    for _ in range(50):
        if server.port is not None:
            break
        time.sleep(0.05)
    assert server.port is not None, "Server failed to start"

    yield f"http://127.0.0.1:{server.port}"

    asyncio.run_coroutine_threadsafe(server.stop(), loop).result(timeout=2)
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
