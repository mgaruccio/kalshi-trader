import pytest
import json
from unittest.mock import patch, MagicMock, PropertyMock

from nautilus_trader.model.identifiers import InstrumentId, Symbol, Venue, ClientOrderId, VenueOrderId
from nautilus_trader.model.objects import Price, Quantity

from adapter import KalshiDataClient, KalshiExecutionClient


@pytest.fixture(autouse=True)
def mock_kalshi_auth():
    with patch("kalshi_python.api_client.KalshiAuth.__init__", return_value=None):
        yield


def _make_data_client(subscriptions=None):
    """Create a KalshiDataClient with mocked internals for testing."""
    orig_init = KalshiDataClient.__init__
    KalshiDataClient.__init__ = lambda self, loop, provider, config, msgbus, cache, clock: None

    mock_inst = MagicMock()
    mock_inst.make_price.side_effect = lambda x: Price(float(x), precision=2)
    mock_inst.make_qty.side_effect = lambda x: Quantity(float(x), precision=0)

    provider = MagicMock()
    provider.find = lambda i: mock_inst

    client = KalshiDataClient(None, provider, None, None, None, None)
    client._instrument_provider = provider
    client._subscriptions = subscriptions or {InstrumentId(Symbol("TEST-YES"), Venue("KALSHI"))}
    client._orderbooks = {}
    KalshiDataClient.__init__ = orig_init

    ticks_received = []
    client._handle_data = lambda tick: ticks_received.append(tick)
    return client, ticks_received


def test_snapshot_sets_book_and_emits_tick():
    """Orderbook snapshot with yes_dollars_fp/no_dollars_fp should emit correct QuoteTick."""
    client, ticks = _make_data_client()

    msg = {
        "type": "orderbook_snapshot",
        "msg": {
            "market_ticker": "TEST",
            "yes_dollars_fp": [["0.55", "100.00"], ["0.50", "200.00"]],
            "no_dollars_fp": [["0.48", "50.00"], ["0.45", "80.00"]],
        },
    }

    with patch("adapter.KalshiDataClient._clock", new_callable=PropertyMock) as mock_clock, \
         patch("adapter.KalshiDataClient._log", new_callable=PropertyMock), \
         patch("adapter.KalshiDataClient._cache", new_callable=PropertyMock) as mock_cache:
        mock_clock.return_value.timestamp_ns.return_value = 123456789000000
        mock_cache.return_value.instrument.return_value = True
        client._handle_ws_message(json.dumps(msg))

    # Should have YES tick (only YES is subscribed)
    assert len(ticks) >= 1
    tick = ticks[-1]
    # YES best bid = max(yes prices) = 0.55
    assert tick.bid_price.as_double() == 0.55
    # YES best ask = 1.0 - max(no prices) = 1.0 - 0.48 = 0.52
    assert tick.ask_price.as_double() == 0.52
    assert tick.bid_size.as_double() == 100
    assert tick.ask_size.as_double() == 50


def test_snapshot_both_sides():
    """Snapshot should emit both YES and NO ticks when both are subscribed."""
    subs = {
        InstrumentId(Symbol("TEST-YES"), Venue("KALSHI")),
        InstrumentId(Symbol("TEST-NO"), Venue("KALSHI")),
    }
    client, ticks = _make_data_client(subscriptions=subs)

    msg = {
        "type": "orderbook_snapshot",
        "msg": {
            "market_ticker": "TEST",
            "yes_dollars_fp": [["0.60", "100.00"]],
            "no_dollars_fp": [["0.42", "50.00"]],
        },
    }

    with patch("adapter.KalshiDataClient._clock", new_callable=PropertyMock) as mock_clock, \
         patch("adapter.KalshiDataClient._log", new_callable=PropertyMock), \
         patch("adapter.KalshiDataClient._cache", new_callable=PropertyMock) as mock_cache:
        mock_clock.return_value.timestamp_ns.return_value = 100000
        mock_cache.return_value.instrument.return_value = True
        client._handle_ws_message(json.dumps(msg))

    # Filter out instrument emissions (MagicMock), keep only QuoteTick-like objects
    quote_ticks = [t for t in ticks if hasattr(t, "bid_price")]
    assert len(quote_ticks) == 2

    yes_tick = [t for t in quote_ticks if "YES" in str(t.instrument_id)][0]
    no_tick = [t for t in quote_ticks if "NO" in str(t.instrument_id)][0]

    # YES: bid=0.60, ask=1.0-0.42=0.58
    assert yes_tick.bid_price.as_double() == 0.60
    assert yes_tick.ask_price.as_double() == 0.58

    # NO: bid=0.42, ask=1.0-0.60=0.40
    assert no_tick.bid_price.as_double() == 0.42
    assert no_tick.ask_price.as_double() == 0.40


def test_delta_updates_book():
    """Orderbook delta should update the internal book and emit ticks."""
    client, ticks = _make_data_client()

    # First, set up initial book via snapshot
    snapshot = {
        "type": "orderbook_snapshot",
        "msg": {
            "market_ticker": "TEST",
            "yes_dollars_fp": [["0.55", "100.00"]],
            "no_dollars_fp": [["0.48", "50.00"]],
        },
    }

    with patch("adapter.KalshiDataClient._clock", new_callable=PropertyMock) as mock_clock, \
         patch("adapter.KalshiDataClient._log", new_callable=PropertyMock), \
         patch("adapter.KalshiDataClient._cache", new_callable=PropertyMock) as mock_cache:
        mock_clock.return_value.timestamp_ns.return_value = 100000
        mock_cache.return_value.instrument.return_value = True

        client._handle_ws_message(json.dumps(snapshot))
        initial_count = len(ticks)

        # Now send a delta adding a better yes bid
        delta = {
            "type": "orderbook_delta",
            "msg": {
                "market_ticker": "TEST",
                "side": "yes",
                "price_dollars": "0.60",
                "delta_fp": "25.00",
            },
        }
        client._handle_ws_message(json.dumps(delta))

    assert len(ticks) > initial_count
    tick = ticks[-1]
    # New best bid should be 0.60 (the delta added a higher yes level)
    assert tick.bid_price.as_double() == 0.60


def test_delta_removes_level_on_zero():
    """Delta with delta_fp <= 0 should remove the price level."""
    client, ticks = _make_data_client()

    # Set up book with two yes levels
    snapshot = {
        "type": "orderbook_snapshot",
        "msg": {
            "market_ticker": "TEST",
            "yes_dollars_fp": [["0.55", "100.00"], ["0.50", "200.00"]],
            "no_dollars_fp": [["0.48", "50.00"]],
        },
    }

    with patch("adapter.KalshiDataClient._clock", new_callable=PropertyMock) as mock_clock, \
         patch("adapter.KalshiDataClient._log", new_callable=PropertyMock), \
         patch("adapter.KalshiDataClient._cache", new_callable=PropertyMock) as mock_cache:
        mock_clock.return_value.timestamp_ns.return_value = 100000
        mock_cache.return_value.instrument.return_value = True

        client._handle_ws_message(json.dumps(snapshot))

        # Remove the 0.55 level
        delta = {
            "type": "orderbook_delta",
            "msg": {
                "market_ticker": "TEST",
                "side": "yes",
                "price_dollars": "0.55",
                "delta_fp": "0",
            },
        }
        client._handle_ws_message(json.dumps(delta))

    tick = ticks[-1]
    # After removing 0.55, best yes bid should be 0.50
    assert tick.bid_price.as_double() == 0.50


def test_one_sided_book_emits_zero_bid():
    """When one side of the book is empty, emit 0 bid so quotes stay fresh.

    This prevents stale quotes from cancelled orders being used as "the market".
    """
    client, ticks = _make_data_client()

    # Delta with only yes side — no side is empty
    delta = {
        "type": "orderbook_delta",
        "msg": {
            "market_ticker": "TEST",
            "side": "yes",
            "price_dollars": "0.55",
            "delta_fp": "100.00",
        },
    }

    with patch("adapter.KalshiDataClient._clock", new_callable=PropertyMock) as mock_clock, \
         patch("adapter.KalshiDataClient._log", new_callable=PropertyMock), \
         patch("adapter.KalshiDataClient._cache", new_callable=PropertyMock) as mock_cache:
        mock_clock.return_value.timestamp_ns.return_value = 100000
        mock_cache.return_value.instrument.return_value = True
        client._handle_ws_message(json.dumps(delta))

    # YES tick: bid=0.55 (from yes_levels), ask=1.00 (no_levels empty → fallback)
    yes_ticks = [t for t in ticks if hasattr(t, "bid_price") and "YES" in str(t.instrument_id)]
    assert len(yes_ticks) == 1
    assert yes_ticks[0].bid_price.as_double() == 0.55
    assert yes_ticks[0].ask_price.as_double() == 1.00  # no NO side → ask defaults high

    # NO side not subscribed in default _make_data_client — verify separately
    client2, ticks2 = _make_data_client(subscriptions={
        InstrumentId(Symbol("TEST-NO"), Venue("KALSHI")),
    })
    with patch("adapter.KalshiDataClient._clock", new_callable=PropertyMock) as mock_clock, \
         patch("adapter.KalshiDataClient._log", new_callable=PropertyMock), \
         patch("adapter.KalshiDataClient._cache", new_callable=PropertyMock) as mock_cache:
        mock_clock.return_value.timestamp_ns.return_value = 100000
        mock_cache.return_value.instrument.return_value = True
        client2._handle_ws_message(json.dumps(delta))

    no_ticks = [t for t in ticks2 if hasattr(t, "bid_price")]
    assert len(no_ticks) == 1
    assert no_ticks[0].bid_price.as_double() == 0.00
    assert no_ticks[0].ask_price.as_double() == 0.45


def test_no_hardcoded_fallback_prices():
    """Verify no 0.01/0.99 dummy prices exist in any emitted tick."""
    client, ticks = _make_data_client()

    snapshot = {
        "type": "orderbook_snapshot",
        "msg": {
            "market_ticker": "TEST",
            "yes_dollars_fp": [["0.55", "100.00"]],
            "no_dollars_fp": [["0.48", "50.00"]],
        },
    }

    with patch("adapter.KalshiDataClient._clock", new_callable=PropertyMock) as mock_clock, \
         patch("adapter.KalshiDataClient._log", new_callable=PropertyMock), \
         patch("adapter.KalshiDataClient._cache", new_callable=PropertyMock) as mock_cache:
        mock_clock.return_value.timestamp_ns.return_value = 100000
        mock_cache.return_value.instrument.return_value = True
        client._handle_ws_message(json.dumps(snapshot))

    for tick in ticks:
        if hasattr(tick, "bid_price"):
            assert tick.bid_price.as_double() != 0.01, "Hardcoded 0.01 bid detected!"
            assert tick.ask_price.as_double() != 0.99, "Hardcoded 0.99 ask detected!"


def _make_exec_client():
    """Create a KalshiExecutionClient with mocked internals for batch testing."""
    orig_init = KalshiExecutionClient.__init__
    KalshiExecutionClient.__init__ = lambda self, *a, **kw: None

    client = KalshiExecutionClient(None, None, None, None, None, None)
    KalshiExecutionClient.__init__ = orig_init

    # Batch state
    client._pending_submits = []
    client._pending_cancels = []
    client._submit_flush_handle = None
    client._cancel_flush_handle = None
    client._BATCH_MAX = 20
    client._BATCH_WINDOW_S = 0.05
    client._accepted_orders = set()
    client._executor = None

    # Mock portfolio API
    client.k_portfolio = MagicMock()

    # Event collectors
    client._accepted = []
    client._rejected = []
    client._cancel_rejected = []

    def mock_accepted(**kw):
        client._accepted.append(kw)
    def mock_rejected(**kw):
        client._rejected.append(kw)
    def mock_cancel_rejected(**kw):
        client._cancel_rejected.append(kw)

    client.generate_order_accepted = mock_accepted
    client.generate_order_rejected = mock_rejected
    client.generate_order_cancel_rejected = mock_cancel_rejected

    return client


def _make_submit_cmd(ticker="TEST", side_suffix="NO", price_cents=92, n=1, idx=0):
    """Create a mock SubmitOrder command."""
    from nautilus_trader.model.enums import OrderSide, OrderType
    from decimal import Decimal

    order = MagicMock()
    order.side = OrderSide.BUY
    order.order_type = OrderType.LIMIT
    order.quantity.as_double.return_value = float(n)
    order.price.as_decimal.return_value = Decimal(price_cents) / 100
    order.client_order_id = ClientOrderId(f"client-{idx}")

    cmd = MagicMock()
    cmd.order = order
    cmd.instrument_id = InstrumentId(Symbol(f"{ticker}-{side_suffix}"), Venue("KALSHI"))
    cmd.strategy_id = None
    return cmd


def _make_cancel_cmd(order_id="venue-0", idx=0):
    """Create a mock CancelOrder command."""
    cmd = MagicMock()
    cmd.venue_order_id = VenueOrderId(order_id)
    cmd.client_order_id = ClientOrderId(f"client-{idx}")
    cmd.instrument_id = InstrumentId(Symbol("TEST-NO"), Venue("KALSHI"))
    cmd.strategy_id = None
    return cmd


def _make_batch_response(n, errors_at=None):
    """Create a mock BatchCreateOrdersResponse with n items.

    errors_at: set of indices that should be errors instead of successes.
    """
    errors_at = errors_at or set()
    resp = MagicMock()
    items = []
    for i in range(n):
        item = MagicMock()
        if i in errors_at:
            item.order = None
            item.error = MagicMock()
            item.error.__str__ = lambda self: "Insufficient balance"
        else:
            item.order = MagicMock()
            item.order.order_id = f"venue-{i}"
            item.error = None
        items.append(item)
    resp.responses = items
    return resp


@pytest.mark.asyncio
async def test_submit_batch_accumulates():
    """Multiple submit_order calls should accumulate and flush as one batch API call."""
    client = _make_exec_client()
    client.k_portfolio.batch_create_orders.return_value = _make_batch_response(5)

    with patch("adapter.KalshiExecutionClient._clock", new_callable=PropertyMock) as mock_clock, \
         patch("adapter.KalshiExecutionClient._log", new_callable=PropertyMock):
        mock_clock.return_value.timestamp_ns.return_value = 1000

        for i in range(5):
            await client._submit_order(_make_submit_cmd(idx=i))

        assert len(client._pending_submits) == 5
        await client._flush_submits()

    client.k_portfolio.batch_create_orders.assert_called_once()
    assert len(client._accepted) == 5
    assert len(client._rejected) == 0


@pytest.mark.asyncio
async def test_submit_batch_chunks_at_20():
    """25 orders should produce 2 batch API calls (20 + 5)."""
    client = _make_exec_client()
    client.k_portfolio.batch_create_orders.side_effect = [
        _make_batch_response(20),
        _make_batch_response(5),
    ]

    with patch("adapter.KalshiExecutionClient._clock", new_callable=PropertyMock) as mock_clock, \
         patch("adapter.KalshiExecutionClient._log", new_callable=PropertyMock):
        mock_clock.return_value.timestamp_ns.return_value = 1000

        # First 20 trigger immediate flush
        for i in range(20):
            await client._submit_order(_make_submit_cmd(idx=i))

        # Remaining 5 need manual flush
        for i in range(20, 25):
            await client._submit_order(_make_submit_cmd(idx=i))
        await client._flush_submits()

    assert client.k_portfolio.batch_create_orders.call_count == 2
    assert len(client._accepted) == 25


@pytest.mark.asyncio
async def test_submit_batch_429_retry():
    """First call returns 429, retry succeeds — all orders accepted."""
    from kalshi_python.exceptions import ApiException

    client = _make_exec_client()
    client.k_portfolio.batch_create_orders.side_effect = [
        ApiException(status=429, reason="Rate limited"),
        _make_batch_response(3),
    ]

    with patch("adapter.KalshiExecutionClient._clock", new_callable=PropertyMock) as mock_clock, \
         patch("adapter.KalshiExecutionClient._log", new_callable=PropertyMock):
        mock_clock.return_value.timestamp_ns.return_value = 1000

        for i in range(3):
            await client._submit_order(_make_submit_cmd(idx=i))
        await client._flush_submits()

    assert client.k_portfolio.batch_create_orders.call_count == 2
    assert len(client._accepted) == 3
    assert len(client._rejected) == 0


@pytest.mark.asyncio
async def test_submit_batch_429_double_fail():
    """Both calls 429 — all orders rejected."""
    from kalshi_python.exceptions import ApiException

    client = _make_exec_client()
    client.k_portfolio.batch_create_orders.side_effect = [
        ApiException(status=429, reason="Rate limited"),
        ApiException(status=429, reason="Still rate limited"),
    ]

    with patch("adapter.KalshiExecutionClient._clock", new_callable=PropertyMock) as mock_clock, \
         patch("adapter.KalshiExecutionClient._log", new_callable=PropertyMock):
        mock_clock.return_value.timestamp_ns.return_value = 1000

        for i in range(3):
            await client._submit_order(_make_submit_cmd(idx=i))
        await client._flush_submits()

    assert len(client._accepted) == 0
    assert len(client._rejected) == 3


@pytest.mark.asyncio
async def test_submit_batch_partial_error():
    """Batch response with mix of success/error — per-order events."""
    client = _make_exec_client()
    client.k_portfolio.batch_create_orders.return_value = _make_batch_response(3, errors_at={1})

    with patch("adapter.KalshiExecutionClient._clock", new_callable=PropertyMock) as mock_clock, \
         patch("adapter.KalshiExecutionClient._log", new_callable=PropertyMock):
        mock_clock.return_value.timestamp_ns.return_value = 1000

        for i in range(3):
            await client._submit_order(_make_submit_cmd(idx=i))
        await client._flush_submits()

    assert len(client._accepted) == 2
    assert len(client._rejected) == 1


@pytest.mark.asyncio
async def test_cancel_batch_accumulates():
    """Multiple cancel_order calls should accumulate and flush as one batch API call."""
    client = _make_exec_client()

    with patch("adapter.KalshiExecutionClient._clock", new_callable=PropertyMock) as mock_clock, \
         patch("adapter.KalshiExecutionClient._log", new_callable=PropertyMock):
        mock_clock.return_value.timestamp_ns.return_value = 1000

        for i in range(5):
            await client._cancel_order(_make_cancel_cmd(order_id=f"venue-{i}", idx=i))

        assert len(client._pending_cancels) == 5
        await client._flush_cancels()

    client.k_portfolio.batch_cancel_orders.assert_called_once()
    assert len(client._cancel_rejected) == 0


@pytest.mark.asyncio
async def test_cancel_batch_429_rejects_all():
    """Cancel batch 429 double-fail rejects all cancels."""
    from kalshi_python.exceptions import ApiException

    client = _make_exec_client()
    client.k_portfolio.batch_cancel_orders.side_effect = [
        ApiException(status=429, reason="Rate limited"),
        ApiException(status=429, reason="Still rate limited"),
    ]

    with patch("adapter.KalshiExecutionClient._clock", new_callable=PropertyMock) as mock_clock, \
         patch("adapter.KalshiExecutionClient._log", new_callable=PropertyMock):
        mock_clock.return_value.timestamp_ns.return_value = 1000

        for i in range(3):
            await client._cancel_order(_make_cancel_cmd(order_id=f"venue-{i}", idx=i))
        await client._flush_cancels()

    assert len(client._cancel_rejected) == 3


@pytest.mark.asyncio
async def test_single_order_batched():
    """A single order still goes through batch path correctly."""
    client = _make_exec_client()
    client.k_portfolio.batch_create_orders.return_value = _make_batch_response(1)

    with patch("adapter.KalshiExecutionClient._clock", new_callable=PropertyMock) as mock_clock, \
         patch("adapter.KalshiExecutionClient._log", new_callable=PropertyMock):
        mock_clock.return_value.timestamp_ns.return_value = 1000

        await client._submit_order(_make_submit_cmd(idx=0))
        await client._flush_submits()

    client.k_portfolio.batch_create_orders.assert_called_once()
    assert len(client._accepted) == 1


@pytest.mark.asyncio
async def test_command_to_create_request():
    """Verify CreateOrderRequest is built correctly for YES and NO sides."""
    client = _make_exec_client()

    # NO side, limit order
    cmd_no = _make_submit_cmd(ticker="KXHIGHNY-26MAR13-T52", side_suffix="NO", price_cents=92, idx=0)
    req_no = client._command_to_create_request(cmd_no)
    assert req_no.ticker == "KXHIGHNY-26MAR13-T52"
    assert req_no.side == "no"
    assert req_no.action == "buy"
    assert req_no.no_price == 92
    assert req_no.yes_price is None

    # YES side, limit order
    cmd_yes = _make_submit_cmd(ticker="KXHIGHNY-26MAR13-T52", side_suffix="YES", price_cents=8, idx=1)
    req_yes = client._command_to_create_request(cmd_yes)
    assert req_yes.side == "yes"
    assert req_yes.yes_price == 8
    assert req_yes.no_price is None

    # Market order omits price
    cmd_mkt = _make_submit_cmd(idx=2)
    from nautilus_trader.model.enums import OrderType
    cmd_mkt.order.order_type = OrderType.MARKET
    cmd_mkt.order.price = None
    req_mkt = client._command_to_create_request(cmd_mkt)
    assert req_mkt.type == "market"
    assert req_mkt.yes_price is None
    assert req_mkt.no_price is None
