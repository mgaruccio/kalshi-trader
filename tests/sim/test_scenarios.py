"""Focused scenario tests for dedup, reconnection, and rate limiting edge cases.

Each test targets a specific dangerous WS message ordering. Messages flow through
the production _handle_ws_message → decode_ws_msg → _handle_fill/_handle_user_order
path via the SimpleNamespace + MethodType pattern.
"""
import asyncio
import types
from collections import OrderedDict
from unittest.mock import AsyncMock, MagicMock, patch

import msgspec
import pytest

from kalshi.execution import KalshiExecutionClient, _DEDUP_MAX
from kalshi.websocket.client import KalshiWebSocketClient
from tests.sim.ws_factory import WsMessageFactory


# ---------------------------------------------------------------------------
# Adapter stub builder
# ---------------------------------------------------------------------------

def _make_adapter_stub():
    """Build a stub with _handle_ws_message bound from KalshiExecutionClient.

    Uses SimpleNamespace + MethodType to avoid Cython descriptor issues.
    Binds _handle_ws_message (the full dispatch path), not individual handlers.
    """
    instrument = MagicMock()
    instrument.make_qty = lambda x: x
    instrument.make_price = lambda x: x

    ws_client = MagicMock(spec=KalshiWebSocketClient)
    ws_client._seq_tracker = {}

    # Use the real _check_sequence implementation
    ws_client._check_sequence = types.MethodType(
        KalshiWebSocketClient._check_sequence, ws_client,
    )

    stub = types.SimpleNamespace(
        _seen_trade_ids=OrderedDict(),
        _accepted_orders={},
        _instrument_provider=MagicMock(),
        _clock=MagicMock(timestamp_ns=MagicMock(
            side_effect=iter(range(1_000_000_000, 2_000_000_000, 1_000_000))
        )),
        _ws_client=ws_client,
        _log=MagicMock(),
        _ack_events={},
        generate_order_accepted=MagicMock(),
        generate_order_filled=MagicMock(),
        generate_order_rejected=MagicMock(),
        generate_order_canceled=MagicMock(),
        generate_position_status_reports=AsyncMock(return_value=[]),
        generate_order_status_reports=AsyncMock(return_value=[]),
        generate_fill_reports=AsyncMock(return_value=[]),
    )
    stub._instrument_provider.find.return_value = instrument

    # Bind the FULL dispatch method (not individual handlers)
    stub._handle_ws_message = types.MethodType(
        KalshiExecutionClient._handle_ws_message, stub,
    )
    # Also bind individual handlers (called by _handle_ws_message)
    stub._handle_user_order = types.MethodType(
        KalshiExecutionClient._handle_user_order, stub,
    )
    stub._handle_fill = types.MethodType(
        KalshiExecutionClient._handle_fill, stub,
    )

    return stub


# ---------------------------------------------------------------------------
# Scenario 1: Duplicate fill → dedup prevents double emission
# ---------------------------------------------------------------------------

class TestDuplicateFillDedup:
    def test_duplicate_trade_id_emits_single_fill(self):
        stub = _make_adapter_stub()
        factory = WsMessageFactory()

        msg1 = factory.fill(
            trade_id="T-001", order_id="O-001", ticker="KXHIGH-T55",
            side="yes", action="buy", count=5, price_cents=50,
            client_order_id="C-001",
        )
        # Same trade_id, different seq (simulates WS duplicate delivery)
        msg2 = factory.fill(
            trade_id="T-001", order_id="O-001", ticker="KXHIGH-T55",
            side="yes", action="buy", count=5, price_cents=50,
            client_order_id="C-001",
        )

        with patch("kalshi.execution.asyncio.create_task"):
            stub._handle_ws_message(msg1)
            stub._handle_ws_message(msg2)

        assert stub.generate_order_filled.call_count == 1

    def test_different_trade_ids_emit_separate_fills(self):
        stub = _make_adapter_stub()
        factory = WsMessageFactory()

        for i in range(3):
            msg = factory.fill(
                trade_id=f"T-{i:03d}", order_id="O-001", ticker="KXHIGH-T55",
                side="yes", action="buy", count=1, price_cents=50,
                client_order_id="C-001",
            )
            with patch("kalshi.execution.asyncio.create_task"):
                stub._handle_ws_message(msg)

        assert stub.generate_order_filled.call_count == 3


# ---------------------------------------------------------------------------
# Scenario 2: Dedup cache eviction → replayed trade_id accepted
# ---------------------------------------------------------------------------

class TestDedupCacheEviction:
    def test_evicted_trade_id_replayed_generates_new_fill(self):
        """After 10K fills, oldest trade_id is evicted. Replaying it creates
        a second fill event. This documents the KNOWN LIMITATION of bounded dedup.
        """
        stub = _make_adapter_stub()
        factory = WsMessageFactory()

        # Pre-fill cache to max
        for i in range(_DEDUP_MAX):
            stub._seen_trade_ids[f"T-{i:06d}"] = None

        assert "T-000000" in stub._seen_trade_ids

        # One more fill evicts T-000000
        msg = factory.fill(
            trade_id="T-NEW", order_id="O-NEW", ticker="KXHIGH-T55",
            side="yes", action="buy", count=1, price_cents=50,
        )
        with patch("kalshi.execution.asyncio.create_task"):
            stub._handle_ws_message(msg)

        assert "T-000000" not in stub._seen_trade_ids

        # Replay T-000000 — will be accepted as new (known limitation)
        stub.generate_order_filled.reset_mock()
        replay = factory.fill(
            trade_id="T-000000", order_id="O-OLD", ticker="KXHIGH-T55",
            side="yes", action="buy", count=1, price_cents=50,
        )
        with patch("kalshi.execution.asyncio.create_task"):
            stub._handle_ws_message(replay)

        assert stub.generate_order_filled.call_count == 1  # documents known risk


# ---------------------------------------------------------------------------
# Scenario 3: Settlement fill → position reconciliation triggered
# ---------------------------------------------------------------------------

class TestSettlementFill:
    def test_settlement_fill_triggers_position_recon(self):
        stub = _make_adapter_stub()
        factory = WsMessageFactory()

        msg = factory.settlement_fill(
            trade_id="T-SET", order_id="O-SET",
            side="yes", action="sell", count=5,
        )
        with patch("kalshi.execution.asyncio.create_task") as mock_task:
            stub._handle_ws_message(msg)

        # Settlement fill (no ticker) triggers position reconciliation
        assert mock_task.call_count == 1

    def test_settlement_fill_does_not_emit_order_filled(self):
        stub = _make_adapter_stub()
        factory = WsMessageFactory()

        msg = factory.settlement_fill(
            trade_id="T-SET2", order_id="O-SET2",
            side="yes", action="sell", count=5,
        )
        with patch("kalshi.execution.asyncio.create_task"):
            stub._handle_ws_message(msg)

        stub.generate_order_filled.assert_not_called()


# ---------------------------------------------------------------------------
# Scenario 4: Sequence gap → reconciliation triggered
# ---------------------------------------------------------------------------

class TestSequenceGap:
    def test_sequence_gap_triggers_reconciliation(self):
        stub = _make_adapter_stub()
        factory = WsMessageFactory()

        # First message establishes sequence on SID 2
        msg1 = factory.fill(
            trade_id="T-001", order_id="O-001", ticker="KXHIGH-T55",
            side="yes", action="buy", count=1, price_cents=50,
        )
        with patch("kalshi.execution.asyncio.create_task"):
            stub._handle_ws_message(msg1)

        # Skip 3 sequence numbers → gap
        factory.skip_seq(channel="fill", count=3)

        msg2 = factory.fill(
            trade_id="T-002", order_id="O-002", ticker="KXHIGH-T55",
            side="yes", action="buy", count=1, price_cents=50,
        )
        with patch("kalshi.execution.asyncio.create_task") as mock_task:
            stub._handle_ws_message(msg2)

        # Sequence gap triggers 2 tasks: generate_order_status_reports + generate_fill_reports
        assert mock_task.call_count == 2

    def test_independent_sids_dont_interfere(self):
        """Sequence on SID 1 (user_order) should not affect SID 2 (fill)."""
        stub = _make_adapter_stub()
        factory = WsMessageFactory()

        # user_order on SID 1
        msg1 = factory.user_order(
            order_id="O-001", ticker="KXHIGH-T55",
            side="yes", action="buy", status="resting",
            client_order_id="C-001",
        )
        # fill on SID 2
        msg2 = factory.fill(
            trade_id="T-001", order_id="O-001", ticker="KXHIGH-T55",
            side="yes", action="buy", count=5, price_cents=50,
            client_order_id="C-001",
        )

        with patch("kalshi.execution.asyncio.create_task"):
            stub._handle_ws_message(msg1)  # SID 1, seq 1
            stub._handle_ws_message(msg2)  # SID 2, seq 1 — no gap

        # Fill should have been processed (no gap on SID 2)
        assert stub.generate_order_filled.call_count == 1


# ---------------------------------------------------------------------------
# Scenario 5: Fill-before-ack (reordered WS messages)
# ---------------------------------------------------------------------------

class TestFillBeforeAck:
    def test_fill_before_user_order_emits_accepted_then_filled(self):
        """If fill arrives before user_order ack, adapter should emit
        accepted (from fill handler) then filled."""
        stub = _make_adapter_stub()
        factory = WsMessageFactory()

        # Fill arrives FIRST (before any user_order "resting" message)
        fill_msg = factory.fill(
            trade_id="T-001", order_id="O-001", ticker="KXHIGH-T55",
            side="yes", action="buy", count=5, price_cents=50,
            client_order_id="C-001",
        )
        with patch("kalshi.execution.asyncio.create_task"):
            stub._handle_ws_message(fill_msg)

        # _handle_fill emits accepted first because c_oid was not yet in _accepted_orders
        assert stub.generate_order_accepted.call_count == 1
        assert stub.generate_order_filled.call_count == 1


# ---------------------------------------------------------------------------
# Scenario 6: User order with status="filled" (immediate fill, no "resting")
# ---------------------------------------------------------------------------

class TestImmediateFill:
    def test_filled_status_emits_accepted_for_unknown_order(self):
        """Orders that fill immediately never get 'resting' status.
        The adapter must emit accepted when seeing status='filled'."""
        stub = _make_adapter_stub()
        factory = WsMessageFactory()

        msg = factory.user_order(
            order_id="O-IMM", ticker="KXHIGH-T55",
            side="yes", action="buy", status="filled",
            client_order_id="C-IMM",
        )
        with patch("kalshi.execution.asyncio.create_task"):
            stub._handle_ws_message(msg)

        assert stub.generate_order_accepted.call_count == 1


# ---------------------------------------------------------------------------
# Scenario 7: Fill for unknown instrument → silently dropped
# ---------------------------------------------------------------------------

class TestUnknownInstrumentFill:
    def test_fill_for_unknown_instrument_does_not_crash(self):
        stub = _make_adapter_stub()
        stub._instrument_provider.find.return_value = None  # unknown instrument
        factory = WsMessageFactory()

        msg = factory.fill(
            trade_id="T-UNK", order_id="O-UNK", ticker="UNKNOWN-TICKER",
            side="yes", action="buy", count=1, price_cents=50,
        )
        with patch("kalshi.execution.asyncio.create_task"):
            stub._handle_ws_message(msg)

        stub.generate_order_filled.assert_not_called()


# ---------------------------------------------------------------------------
# Scenario 8: Malformed WS message → no state corruption
# ---------------------------------------------------------------------------

class TestMalformedMessage:
    def test_garbage_bytes_do_not_corrupt_state(self):
        stub = _make_adapter_stub()
        initial_trades = len(stub._seen_trade_ids)
        initial_accepted = len(stub._accepted_orders)

        with patch("kalshi.execution.asyncio.create_task"):
            stub._handle_ws_message(b"not json at all")

        assert len(stub._seen_trade_ids) == initial_trades
        assert len(stub._accepted_orders) == initial_accepted

    def test_unknown_message_type_does_not_crash(self):
        stub = _make_adapter_stub()
        raw = msgspec.json.encode({"type": "unknown_type", "sid": 1, "seq": 1, "msg": {}})
        with patch("kalshi.execution.asyncio.create_task"):
            stub._handle_ws_message(raw)

        # Should not crash — unknown types are silently ignored


# ---------------------------------------------------------------------------
# Scenario 9: Cross-market fills are independent
# ---------------------------------------------------------------------------

class TestCrossMarketIndependence:
    def test_fills_on_different_tickers_tracked_independently(self):
        stub = _make_adapter_stub()
        factory = WsMessageFactory()

        msg_a = factory.fill(
            trade_id="T-A", order_id="O-A", ticker="TICKER-A",
            side="yes", action="buy", count=5, price_cents=50,
            client_order_id="C-A",
        )
        msg_b = factory.fill(
            trade_id="T-B", order_id="O-B", ticker="TICKER-B",
            side="no", action="sell", count=3, price_cents=70,
            client_order_id="C-B",
        )

        with patch("kalshi.execution.asyncio.create_task"):
            stub._handle_ws_message(msg_a)
            stub._handle_ws_message(msg_b)

        assert stub.generate_order_filled.call_count == 2
        assert "T-A" in stub._seen_trade_ids
        assert "T-B" in stub._seen_trade_ids


# ---------------------------------------------------------------------------
# Scenario 10: Dedup cache off-by-one boundary
# ---------------------------------------------------------------------------

class TestDedupCacheBoundary:
    def test_cache_size_at_dedup_max(self):
        """Verify the exact boundary behavior of the >= check."""
        stub = _make_adapter_stub()

        # Fill to exactly _DEDUP_MAX - 1
        for i in range(_DEDUP_MAX - 1):
            stub._seen_trade_ids[f"T-{i:06d}"] = None

        assert len(stub._seen_trade_ids) == _DEDUP_MAX - 1

        # One more entry should trigger eviction (>= check)
        factory = WsMessageFactory()
        msg = factory.fill(
            trade_id="T-BOUNDARY", order_id="O-B", ticker="KXHIGH-T55",
            side="yes", action="buy", count=1, price_cents=50,
        )
        with patch("kalshi.execution.asyncio.create_task"):
            stub._handle_ws_message(msg)

        # After insert + eviction, size should be _DEDUP_MAX - 1
        # (insert makes it _DEDUP_MAX, then >= triggers popitem)
        assert len(stub._seen_trade_ids) == _DEDUP_MAX - 1
        assert "T-BOUNDARY" in stub._seen_trade_ids
        assert "T-000000" not in stub._seen_trade_ids  # oldest evicted
