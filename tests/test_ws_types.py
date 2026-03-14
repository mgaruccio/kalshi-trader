"""Tests for kalshi.websocket.types — msgspec WS message schemas."""
import msgspec

from kalshi.websocket.types import (
    OrderbookSnapshotMsg,
    OrderbookDeltaMsg,
    UserOrderMsg,
    FillMsg,
    decode_ws_msg,
)


# -- Orderbook Snapshot --
SNAPSHOT_JSON = b'''{
  "type": "orderbook_snapshot",
  "sid": 2,
  "seq": 1,
  "msg": {
    "market_ticker": "KXHIGHCHI-26MAR14-T55",
    "market_id": "abc-123",
    "yes_dollars_fp": [["0.3200", "5.00"], ["0.3100", "10.00"]],
    "no_dollars_fp": [["0.6800", "3.00"]]
  }
}'''

def test_decode_orderbook_snapshot():
    msg_type, sid, seq, msg = decode_ws_msg(SNAPSHOT_JSON)
    assert msg_type == "orderbook_snapshot"
    assert sid == 2
    assert seq == 1
    assert isinstance(msg, OrderbookSnapshotMsg)
    assert msg.market_ticker == "KXHIGHCHI-26MAR14-T55"
    assert len(msg.yes_dollars_fp) == 2
    assert msg.yes_dollars_fp[0] == ["0.3200", "5.00"]
    assert len(msg.no_dollars_fp) == 1

# -- Orderbook Delta --
DELTA_JSON = b'''{
  "type": "orderbook_delta",
  "sid": 2,
  "seq": 3,
  "msg": {
    "market_ticker": "KXHIGHCHI-26MAR14-T55",
    "price_dollars": "0.3200",
    "delta_fp": "-5.00",
    "side": "yes"
  }
}'''

def test_decode_orderbook_delta():
    msg_type, sid, seq, msg = decode_ws_msg(DELTA_JSON)
    assert msg_type == "orderbook_delta"
    assert isinstance(msg, OrderbookDeltaMsg)
    assert msg.side == "yes"
    assert msg.price_dollars == "0.3200"
    assert msg.delta_fp == "-5.00"

# -- User Order --
USER_ORDER_JSON = b'''{
  "type": "user_order",
  "sid": 5,
  "seq": 1,
  "msg": {
    "order_id": "uuid-123",
    "client_order_id": "my-order-1",
    "ticker": "KXHIGHCHI-26MAR14-T55",
    "status": "resting",
    "side": "yes",
    "action": "buy",
    "remaining_count_fp": "10.00",
    "fill_count_fp": "0.00"
  }
}'''

def test_decode_user_order():
    msg_type, sid, seq, msg = decode_ws_msg(USER_ORDER_JSON)
    assert msg_type == "user_order"
    assert isinstance(msg, UserOrderMsg)
    assert msg.order_id == "uuid-123"
    assert msg.status == "resting"
    assert msg.side == "yes"
    assert msg.remaining_count_fp == "10.00"

# -- Fill --
FILL_JSON = b'''{
  "type": "fill",
  "sid": 6,
  "seq": 1,
  "msg": {
    "trade_id": "trade-uuid",
    "order_id": "order-uuid",
    "client_order_id": "my-order-1",
    "market_ticker": "KXHIGHCHI-26MAR14-T55",
    "side": "yes",
    "action": "buy",
    "count_fp": "5.00",
    "yes_price_dollars": "0.3200",
    "no_price_dollars": "0.6800",
    "fee_cost": "0.0056",
    "is_taker": false,
    "ts": 1703123456
  }
}'''

def test_decode_fill():
    msg_type, sid, seq, msg = decode_ws_msg(FILL_JSON)
    assert msg_type == "fill"
    assert isinstance(msg, FillMsg)
    assert msg.trade_id == "trade-uuid"
    assert msg.market_ticker == "KXHIGHCHI-26MAR14-T55"
    assert msg.count_fp == "5.00"
    assert msg.fee_cost == "0.0056"
    assert msg.is_taker is False
    assert msg.ts == 1703123456

# -- Settlement fill (no ticker) --
SETTLEMENT_FILL_JSON = b'''{
  "type": "fill",
  "sid": 6,
  "seq": 2,
  "msg": {
    "trade_id": "settle-uuid",
    "order_id": "settle-order",
    "side": "yes",
    "action": "buy",
    "count_fp": "5.00",
    "yes_price_dollars": "1.0000",
    "fee_cost": "0.0000",
    "is_taker": false,
    "ts": 1703200000
  }
}'''

def test_settlement_fill_has_no_ticker():
    msg_type, sid, seq, msg = decode_ws_msg(SETTLEMENT_FILL_JSON)
    assert msg_type == "fill"
    assert isinstance(msg, FillMsg)
    assert msg.market_ticker is None  # settlement fills omit ticker

# -- Edge: delta_fp of "0.00" means remove level --
DELTA_REMOVE_JSON = b'''{
  "type": "orderbook_delta",
  "sid": 2,
  "seq": 4,
  "msg": {
    "market_ticker": "KXHIGHCHI-26MAR14-T55",
    "price_dollars": "0.3200",
    "delta_fp": "0.00",
    "side": "yes"
  }
}'''

def test_delta_zero_means_remove():
    msg_type, sid, seq, msg = decode_ws_msg(DELTA_REMOVE_JSON)
    assert isinstance(msg, OrderbookDeltaMsg)
    assert float(msg.delta_fp) == 0.0

# -- Unknown message type falls back gracefully --
UNKNOWN_JSON = b'{"type": "heartbeat", "sid": 0, "seq": 0, "msg": {}}'

def test_unknown_type_returns_dict():
    msg_type, sid, seq, msg = decode_ws_msg(UNKNOWN_JSON)
    assert msg_type == "heartbeat"
    assert isinstance(msg, dict)
