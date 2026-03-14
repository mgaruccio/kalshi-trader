"""msgspec schema classes for Kalshi WebSocket messages.

Uses msgspec.Raw two-pass decode: first pass extracts the envelope (type, sid, seq),
second pass decodes the inner msg based on the type field. This avoids the union-of-Structs
crash documented in Correction #1.
"""
import msgspec


class OrderbookSnapshotMsg(msgspec.Struct):
    market_ticker: str
    yes_dollars_fp: list[list[str]]
    no_dollars_fp: list[list[str]]
    market_id: str | None = None


class OrderbookDeltaMsg(msgspec.Struct):
    market_ticker: str
    price_dollars: str
    delta_fp: str
    side: str


class UserOrderMsg(msgspec.Struct):
    order_id: str
    ticker: str
    status: str
    side: str
    action: str
    client_order_id: str | None = None
    remaining_count_fp: str | None = None
    fill_count_fp: str | None = None
    initial_count_fp: str | None = None
    yes_price_dollars: str | None = None
    no_price_dollars: str | None = None
    created_time: str | None = None
    last_update_time: str | None = None


class FillMsg(msgspec.Struct):
    """NOTE: no_price_dollars appears exactly ONCE (Correction #23)."""
    trade_id: str
    order_id: str
    side: str
    action: str
    count_fp: str
    is_taker: bool
    ts: int
    client_order_id: str | None = None
    market_ticker: str | None = None
    yes_price_dollars: str | None = None
    no_price_dollars: str | None = None
    fee_cost: str | None = None


class WsEnvelope(msgspec.Struct):
    type: str
    sid: int = 0
    seq: int = 0
    msg: msgspec.Raw = msgspec.Raw(b"{}")


_DECODERS: dict[str, msgspec.json.Decoder] = {
    "orderbook_snapshot": msgspec.json.Decoder(OrderbookSnapshotMsg),
    "orderbook_delta": msgspec.json.Decoder(OrderbookDeltaMsg),
    "user_order": msgspec.json.Decoder(UserOrderMsg),
    "fill": msgspec.json.Decoder(FillMsg),
}

_ENVELOPE_DECODER = msgspec.json.Decoder(WsEnvelope)


def decode_ws_msg(raw: bytes) -> tuple[str, int, int, object]:
    """Two-pass decode. Returns (type, sid, seq, typed_msg)."""
    envelope = _ENVELOPE_DECODER.decode(raw)
    decoder = _DECODERS.get(envelope.type)
    if decoder is not None:
        typed_msg = decoder.decode(envelope.msg)
    else:
        typed_msg = msgspec.json.decode(envelope.msg)
    return envelope.type, envelope.sid, envelope.seq, typed_msg
