"""Live end-to-end confidence tests — TradingNode + mock Kalshi exchange.

Each test class boots a real TradingNode with unmodified production adapter
factories, connects it to the mock Kalshi exchange, runs a named scenario,
and asserts the expected fills / cancels.

Architecture
------------
  ┌──────────────────────────┐   HTTP/WS    ┌──────────────────────────┐
  │  TradingNode (thread 2)  │ ───────────► │  MockKalshiServer (thr1) │
  │  KalshiLiveDataClient    │             │  REST: orders / fills     │
  │  KalshiLiveExecClient    │◄─────────── │  WS:  orderbook / fills   │
  └──────────────────────────┘             └──────────────────────────┘
         │ QuoteTick / fills
         ▼
  ConfidenceTestStrategy  ─── done_event (threading.Event)
         │
         ▼
  test asserts (fills, cancels, done flag)
"""

from __future__ import annotations

import asyncio
import logging
import os
import threading
import time

import pytest

import kalshi.factories
from kalshi.config import KalshiDataClientConfig, KalshiExecClientConfig
from kalshi.factories import KalshiLiveDataClientFactory, KalshiLiveExecClientFactory
from nautilus_trader.live.config import LiveExecEngineConfig, TradingNodeConfig
from nautilus_trader.live.node import TradingNode
from tests.mock_exchange.fixtures import TEST_SERIES, TEST_TICKER, create_dummy_rsa_key
from tests.mock_exchange.scenarios import (
    CHASE_UP,
    MARKET_ORDER_IMMEDIATE,
    NO_FILL_TIMEOUT,
    STEADY_FILL,
    Scenario,
)
from tests.mock_exchange.server import MockKalshiServer
from tests.mock_exchange.strategy import ConfidenceTestStrategy

log = logging.getLogger(__name__)

# NautilusTrader checks for a running event loop when pytest is detected.
# Provide one so TradingNode can initialize in the test process.
_test_loop = asyncio.new_event_loop()
asyncio.set_event_loop(_test_loop)

_CONNECT_TIMEOUT_SECS = 15.0  # max wait for first QuoteTick after node.run()
_FILL_WAIT_SECS = 5.0  # max wait for first fill in market-only scenarios
_DONE_WAIT_SECS = 10.0  # max wait for strategy.done (2-fill scenarios)
_SCENARIO_STEP_BUFFER = 5.0  # extra seconds beyond summed delays in fut.result()


# ---------------------------------------------------------------------------
# Infrastructure helpers
# ---------------------------------------------------------------------------


def _start_mock_server() -> tuple[
    MockKalshiServer, asyncio.AbstractEventLoop, threading.Thread
]:
    """Start the mock exchange in a daemon thread. Returns (server, loop, thread)."""
    server = MockKalshiServer()
    loop = asyncio.new_event_loop()

    def _run() -> None:
        loop.run_until_complete(server.start(port=0, ws_port=0))
        loop.run_forever()

    t = threading.Thread(target=_run, daemon=True)
    t.start()

    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline:
        if server.port is not None and server.ws_port is not None:
            break
        time.sleep(0.05)

    assert server.port is not None, "Mock REST server failed to start"
    assert server.ws_port is not None, "Mock WS server failed to start"
    return server, loop, t


def _stop_mock_server(
    server: MockKalshiServer,
    loop: asyncio.AbstractEventLoop,
    t: threading.Thread,
) -> None:
    asyncio.run_coroutine_threadsafe(server.stop(), loop).result(timeout=3)
    loop.call_soon_threadsafe(loop.stop)
    t.join(timeout=3)


def _build_scenario_steps(ticker: str, scenario: Scenario) -> list[tuple]:
    """Convert Scenario.updates into server.run_scenario() step tuples.

    Each BookUpdate → two steps: YES delta (carries delay_ms), NO delta (no
    additional delay).  Publishing YES first ensures NO ask (= 1 − YES bid)
    updates before check_resting_orders fires — critical for limit-fill timing.

    Step format: (ticker, side, price_dollars, delta_fp, delay_secs)
    """
    steps = []
    for upd in scenario.updates:
        delay = upd.delay_ms / 1000.0
        steps.append(
            (
                ticker,
                "yes",
                f"{upd.yes_bid_cents / 100:.2f}",
                str(upd.yes_bid_size),
                delay,
            )
        )
        steps.append(
            (ticker, "no", f"{upd.no_bid_cents / 100:.2f}", str(upd.no_bid_size), 0.0)
        )
    return steps


# ---------------------------------------------------------------------------
# Core scenario runner
# ---------------------------------------------------------------------------


def _run_scenario_test(scenario: Scenario) -> ConfidenceTestStrategy:
    """Boot a TradingNode against the mock exchange, run the scenario.

    Returns the strategy for the caller to assert outcomes.  All resources
    (node, server, key file) are cleaned up in the finally block.
    """
    # 1. Start mock exchange -------------------------------------------------
    server, server_loop, server_thread = _start_mock_server()
    rest_url = f"http://127.0.0.1:{server.port}"
    ws_url = f"ws://127.0.0.1:{server.ws_port}"

    server.set_initial_book(
        TEST_TICKER,
        no_bid_cents=scenario.initial_no_bid_cents,
        yes_bid_cents=scenario.initial_yes_bid_cents,
    )

    # 2. Credentials and singleton reset ------------------------------------
    pem_path = create_dummy_rsa_key()
    kalshi.factories._SHARED_PROVIDER = None

    # 3. Strategy with done signal ------------------------------------------
    done_event = threading.Event()
    strategy = ConfidenceTestStrategy(
        ticker=TEST_TICKER,
        done_callback=lambda: done_event.set(),
    )

    # 4. Build TradingNode --------------------------------------------------
    data_cfg = KalshiDataClientConfig(
        base_url_http=rest_url,
        base_url_ws=ws_url,
        api_key_id="test-key",
        private_key_path=pem_path,
        series_ticker=TEST_SERIES,
    )
    exec_cfg = KalshiExecClientConfig(
        base_url_http=rest_url,
        base_url_ws=ws_url,
        api_key_id="test-key",
        private_key_path=pem_path,
        max_retries=1,
        retry_delay_initial_ms=100,
        ack_timeout_secs=10.0,
    )
    node_cfg = TradingNodeConfig(
        trader_id="CONF-TESTER",
        timeout_connection=10.0,
        exec_engine=LiveExecEngineConfig(
            reconciliation_startup_delay_secs=0.0,  # no delay in tests
        ),
        data_clients={"KALSHI": data_cfg},
        exec_clients={"KALSHI": exec_cfg},
    )
    node = TradingNode(config=node_cfg)
    node.add_data_client_factory("KALSHI", KalshiLiveDataClientFactory)
    node.add_exec_client_factory("KALSHI", KalshiLiveExecClientFactory)
    node.build()
    node.trader.add_strategy(strategy)

    # 5. Run node in background --------------------------------------------
    node_thread = threading.Thread(target=node.run, daemon=True)
    node_thread.start()

    # 6. Wait for first QuoteTick (proves node connected + strategy subscribed)
    connect_deadline = time.monotonic() + _CONNECT_TIMEOUT_SECS
    while time.monotonic() < connect_deadline and not strategy.quotes_received:
        time.sleep(0.1)

    assert strategy.quotes_received, (
        f"Strategy never received a QuoteTick — TradingNode failed to connect "
        f"to mock exchange at {rest_url} / {ws_url}"
    )

    try:
        # 7. Publish scenario book updates on the server's event loop ------
        steps = _build_scenario_steps(TEST_TICKER, scenario)
        if steps:
            total_delay = sum(s[4] for s in steps)
            fut = asyncio.run_coroutine_threadsafe(
                server.run_scenario(steps), server_loop
            )
            fut.result(timeout=total_delay + _SCENARIO_STEP_BUFFER)

        # 8. Wait for the scenario's expected terminal state ---------------
        if scenario.expect_limit_fill:
            # Strategy signals done() when it has ≥2 fills (market + limit)
            done_event.wait(timeout=_DONE_WAIT_SECS)
        else:
            # Market-only scenarios: wait for at least one fill then settle
            fill_deadline = time.monotonic() + _FILL_WAIT_SECS
            while time.monotonic() < fill_deadline and not strategy.filled_orders:
                time.sleep(0.05)
            time.sleep(0.5)  # brief grace for in-flight WS messages

    finally:
        # 9. Tear down -------------------------------------------------------
        node.stop()
        node_thread.join(timeout=5.0)
        _stop_mock_server(server, server_loop, server_thread)
        os.unlink(pem_path)
        kalshi.factories._SHARED_PROVIDER = None

    return strategy


# ---------------------------------------------------------------------------
# Module-level fixtures — one per scenario, class-scoped to run once per class
# ---------------------------------------------------------------------------


@pytest.fixture(scope="class")
def steady_strategy():
    return _run_scenario_test(STEADY_FILL)


@pytest.fixture(scope="class")
def chase_up_strategy():
    return _run_scenario_test(CHASE_UP)


@pytest.fixture(scope="class")
def market_immediate_strategy():
    return _run_scenario_test(MARKET_ORDER_IMMEDIATE)


@pytest.fixture(scope="class")
def no_fill_timeout_strategy():
    return _run_scenario_test(NO_FILL_TIMEOUT)


# ---------------------------------------------------------------------------
# Test classes
# ---------------------------------------------------------------------------


class TestSteadyFill:
    """STEADY_FILL: YES bid walks 58→66c, NO ask drops to 34c → limit fills."""

    def test_market_fill_received(self, steady_strategy):
        """Market FOK order fills on first quote."""
        assert steady_strategy.filled_orders, (
            "Expected at least one fill (market order)"
        )

    def test_limit_fill_received(self, steady_strategy):
        """Limit BUY NO at 34c fills when NO ask reaches 34c."""
        assert len(steady_strategy.filled_orders) >= 2, (
            f"Expected ≥2 fills (market + limit), got {len(steady_strategy.filled_orders)}"
        )

    def test_cancel_count(self, steady_strategy):
        """No limit cancels in steady-fill — NO bid does not move until fill."""
        assert (
            len(steady_strategy.canceled_orders) == STEADY_FILL.expect_cancel_count
        ), (
            f"Expected {STEADY_FILL.expect_cancel_count} cancels, "
            f"got {len(steady_strategy.canceled_orders)}"
        )

    def test_done_flag_set(self, steady_strategy):
        """Strategy signals done after receiving both fills."""
        assert steady_strategy.done, "Strategy done flag must be set after 2 fills"

    def test_no_rejections(self, steady_strategy):
        assert not steady_strategy.rejected_orders, (
            f"Unexpected order rejections: {steady_strategy.rejected_orders}"
        )


class TestChaseUp:
    """CHASE_UP: NO bid walks 30→35c (5 cancels), then YES→66c → limit fill."""

    def test_market_and_limit_fills(self, chase_up_strategy):
        assert len(chase_up_strategy.filled_orders) >= 2, (
            f"Expected ≥2 fills (market + limit), got {len(chase_up_strategy.filled_orders)}"
        )

    def test_cancel_count(self, chase_up_strategy):
        """Strategy cancels limit once per NO-bid step (5 steps)."""
        assert len(chase_up_strategy.canceled_orders) == CHASE_UP.expect_cancel_count, (
            f"Expected {CHASE_UP.expect_cancel_count} cancels, "
            f"got {len(chase_up_strategy.canceled_orders)}"
        )

    def test_done_flag_set(self, chase_up_strategy):
        assert chase_up_strategy.done

    def test_no_rejections(self, chase_up_strategy):
        assert not chase_up_strategy.rejected_orders, (
            f"Unexpected order rejections: {chase_up_strategy.rejected_orders}"
        )


class TestMarketOrderImmediate:
    """MARKET_ORDER_IMMEDIATE: no book updates, market order fills immediately."""

    def test_market_fill_received(self, market_immediate_strategy):
        assert market_immediate_strategy.filled_orders, (
            "Expected market order to fill immediately"
        )

    def test_limit_not_filled(self, market_immediate_strategy):
        """Limit order should remain resting — no price movement to trigger fill."""
        assert not market_immediate_strategy.done, (
            "Strategy done flag must NOT be set (limit order should not fill)"
        )

    def test_no_cancels(self, market_immediate_strategy):
        """No book updates → no cancel-and-replace cycles."""
        assert (
            len(market_immediate_strategy.canceled_orders)
            == MARKET_ORDER_IMMEDIATE.expect_cancel_count
        ), (
            f"Expected {MARKET_ORDER_IMMEDIATE.expect_cancel_count} cancels, "
            f"got {len(market_immediate_strategy.canceled_orders)}"
        )

    def test_no_rejections(self, market_immediate_strategy):
        assert not market_immediate_strategy.rejected_orders, (
            f"Unexpected order rejections: {market_immediate_strategy.rejected_orders}"
        )


class TestNoFillTimeout:
    """NO_FILL_TIMEOUT: NO ask stays > 40c throughout; limit canceled 3 times."""

    def test_market_fill_received(self, no_fill_timeout_strategy):
        assert no_fill_timeout_strategy.filled_orders, "Expected market order to fill"

    def test_limit_not_filled(self, no_fill_timeout_strategy):
        """NO ask never reaches the limit price — strategy should not signal done."""
        assert not no_fill_timeout_strategy.done, (
            "Strategy done flag must NOT be set (limit never fills in this scenario)"
        )

    def test_cancel_count(self, no_fill_timeout_strategy):
        """Strategy cancels limit once per NO-bid step (3 steps)."""
        assert (
            len(no_fill_timeout_strategy.canceled_orders)
            == NO_FILL_TIMEOUT.expect_cancel_count
        ), (
            f"Expected {NO_FILL_TIMEOUT.expect_cancel_count} cancels, "
            f"got {len(no_fill_timeout_strategy.canceled_orders)}"
        )

    def test_no_rejections(self, no_fill_timeout_strategy):
        assert not no_fill_timeout_strategy.rejected_orders, (
            f"Unexpected order rejections: {no_fill_timeout_strategy.rejected_orders}"
        )


# ---------------------------------------------------------------------------
# Additional price / quantity assertions (review finding: expect_limit_fill_price_cents
# was defined in Scenario but never asserted — correction added post-review).
# ---------------------------------------------------------------------------


class TestSteadyFillPrice:
    """Fill price / quantity assertions for STEADY_FILL."""

    def test_limit_fill_price(self, steady_strategy):
        """Limit fill price must be exactly 34c — validates the full price round-trip."""
        assert len(steady_strategy.filled_orders) >= 2, "Need ≥2 fills to check limit price"
        limit_fill = steady_strategy.filled_orders[-1]
        actual_cents = round(float(limit_fill.last_px) * 100)
        assert actual_cents == STEADY_FILL.expect_limit_fill_price_cents, (
            f"Limit fill price {actual_cents}c != expected "
            f"{STEADY_FILL.expect_limit_fill_price_cents}c"
        )

    def test_fill_quantities(self, steady_strategy):
        """Each fill must be for exactly 1 contract."""
        for event in steady_strategy.filled_orders:
            assert int(event.last_qty) == 1, (
                f"Fill {event.client_order_id} qty {event.last_qty} != 1"
            )


class TestChaseUpPrice:
    """Fill price / quantity assertions for CHASE_UP."""

    def test_limit_fill_price(self, chase_up_strategy):
        """Limit fill after chase must be exactly 34c — validates price round-trip."""
        assert len(chase_up_strategy.filled_orders) >= 2, "Need ≥2 fills to check limit price"
        limit_fill = chase_up_strategy.filled_orders[-1]
        actual_cents = round(float(limit_fill.last_px) * 100)
        assert actual_cents == CHASE_UP.expect_limit_fill_price_cents, (
            f"Limit fill price {actual_cents}c != expected "
            f"{CHASE_UP.expect_limit_fill_price_cents}c"
        )

    def test_fill_quantities(self, chase_up_strategy):
        """Each fill must be for exactly 1 contract."""
        for event in chase_up_strategy.filled_orders:
            assert int(event.last_qty) == 1, (
                f"Fill {event.client_order_id} qty {event.last_qty} != 1"
            )
