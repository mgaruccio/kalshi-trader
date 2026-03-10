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


def test_no_tick_emitted_when_book_incomplete():
    """Should NOT emit garbage when one side of the book is empty."""
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

    # No ticks should be emitted — book is incomplete (no NO side)
    quote_ticks = [t for t in ticks if hasattr(t, "bid_price")]
    assert len(quote_ticks) == 0


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


@pytest.mark.asyncio
async def test_cancel_order_rejected():
    orig_init = KalshiExecutionClient.__init__
    KalshiExecutionClient.__init__ = lambda self, loop, provider, config, msgbus, cache, clock: None

    client = KalshiExecutionClient(None, None, None, None, None, None)
    KalshiExecutionClient.__init__ = orig_init

    def mock_do_cancel(*args, **kwargs):
        raise ValueError("Simulated network error")

    client._do_cancel_order = mock_do_cancel

    cancel_rejected_events = []
    def mock_generate_order_cancel_rejected(**kwargs):
        cancel_rejected_events.append(kwargs)
    client.generate_order_cancel_rejected = mock_generate_order_cancel_rejected

    class MockLoop:
        async def run_in_executor(self, executor, func, *args):
            return func(*args)
    client.loop = MockLoop()
    client._executor = None

    cmd = type("MockCancelOrder", (), {
        "client_order_id": ClientOrderId("client123"),
        "venue_order_id": VenueOrderId("venue123"),
        "instrument_id": InstrumentId(Symbol("TEST-YES"), Venue("KALSHI")),
        "strategy_id": None
    })()

    with patch("adapter.KalshiExecutionClient._clock", new_callable=PropertyMock) as mock_clock, \
         patch("adapter.KalshiExecutionClient._log", new_callable=PropertyMock):
        mock_clock.return_value.timestamp_ns.return_value = 1000
        await client._cancel_order(cmd)

    assert len(cancel_rejected_events) == 1
    assert cancel_rejected_events[0]["reason"] == "Simulated network error"
    assert cancel_rejected_events[0]["client_order_id"] == ClientOrderId("client123")
