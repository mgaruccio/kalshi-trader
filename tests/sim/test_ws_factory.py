"""Tests for WsMessageFactory — canned WS message generation."""
import msgspec
import pytest

from kalshi.websocket.types import FillMsg, UserOrderMsg, decode_ws_msg
from tests.sim.ws_factory import WsMessageFactory


class TestWsMessageFactory:
    def test_fill_message_decodes_through_production_path(self):
        """Messages must round-trip through decode_ws_msg."""
        factory = WsMessageFactory()
        raw = factory.fill(
            trade_id="T-001", order_id="O-001", ticker="KXHIGH-T55",
            side="yes", action="buy", count=5, price_cents=50,
        )
        msg_type, sid, seq, msg = decode_ws_msg(raw)
        assert msg_type == "fill"
        assert isinstance(msg, FillMsg)
        assert msg.trade_id == "T-001"
        assert msg.yes_price_dollars == "0.50"
        assert msg.no_price_dollars == "0.50"
        assert msg.count_fp == "5.00"
        assert msg.is_taker is True

    def test_user_order_message_decodes(self):
        factory = WsMessageFactory()
        raw = factory.user_order(
            order_id="O-001", ticker="KXHIGH-T55",
            side="yes", action="buy", status="resting",
            client_order_id="C-001",
        )
        msg_type, sid, seq, msg = decode_ws_msg(raw)
        assert msg_type == "user_order"
        assert isinstance(msg, UserOrderMsg)
        assert msg.status == "resting"
        assert msg.client_order_id == "C-001"

    def test_settlement_fill_has_no_ticker(self):
        factory = WsMessageFactory()
        raw = factory.settlement_fill(
            trade_id="T-SET", order_id="O-SET",
            side="yes", action="sell", count=5,
        )
        msg_type, _, _, msg = decode_ws_msg(raw)
        assert msg_type == "fill"
        assert msg.market_ticker is None

    def test_sequence_numbers_increment(self):
        factory = WsMessageFactory()
        msgs = [
            factory.fill(trade_id=f"T-{i}", order_id="O-1", ticker="T",
                        side="yes", action="buy", count=1, price_cents=50)
            for i in range(5)
        ]
        seqs = [decode_ws_msg(m)[2] for m in msgs]
        assert seqs == [1, 2, 3, 4, 5]

    def test_fill_channel_uses_sid_2(self):
        factory = WsMessageFactory()
        raw = factory.fill(
            trade_id="T-1", order_id="O-1", ticker="T",
            side="yes", action="buy", count=1, price_cents=50,
        )
        _, sid, _, _ = decode_ws_msg(raw)
        assert sid == 2  # fill channel

    def test_user_order_channel_uses_sid_1(self):
        factory = WsMessageFactory()
        raw = factory.user_order(
            order_id="O-1", ticker="T", side="yes",
            action="buy", status="resting",
        )
        _, sid, _, _ = decode_ws_msg(raw)
        assert sid == 1  # user_order channel

    def test_duplicate_fill_preserves_trade_id(self):
        factory = WsMessageFactory()
        raw1 = factory.fill(
            trade_id="T-DUP", order_id="O-1", ticker="T",
            side="yes", action="buy", count=1, price_cents=50,
        )
        # Reset seq to same value for dedup testing
        raw2 = factory.fill(
            trade_id="T-DUP", order_id="O-1", ticker="T",
            side="yes", action="buy", count=1, price_cents=50,
        )
        _, _, _, msg1 = decode_ws_msg(raw1)
        _, _, _, msg2 = decode_ws_msg(raw2)
        assert msg1.trade_id == msg2.trade_id

    def test_gap_in_sequence(self):
        """skip_seq produces a gap that _check_sequence should detect."""
        factory = WsMessageFactory()
        raw1 = factory.fill(
            trade_id="T-1", order_id="O-1", ticker="T",
            side="yes", action="buy", count=1, price_cents=50,
        )
        factory.skip_seq(channel="fill", count=3)  # skip 3 seq numbers
        raw2 = factory.fill(
            trade_id="T-2", order_id="O-1", ticker="T",
            side="yes", action="buy", count=1, price_cents=50,
        )
        _, _, seq1, _ = decode_ws_msg(raw1)
        _, _, seq2, _ = decode_ws_msg(raw2)
        assert seq2 == seq1 + 4  # gap of 3
