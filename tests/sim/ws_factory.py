"""WS message factory — produces canned Kalshi WebSocket messages as JSON bytes.

All messages are serialized to bytes and decoded through the production
`decode_ws_msg` path, ensuring field name and type conformance with the
actual Kalshi WS protocol.
"""
from __future__ import annotations

import msgspec


# SID assignments per channel (matches Kalshi WS behavior)
_CHANNEL_SIDS = {
    "user_order": 1,
    "fill": 2,
}


class WsMessageFactory:
    """Produces WS messages as raw JSON bytes for feeding into adapter handlers.

    Messages go through `decode_ws_msg()` in the test, exercising the
    production deserialization path. Sequence numbers auto-increment per SID.
    """

    def __init__(self) -> None:
        self._seq: dict[int, int] = {}  # sid → next seq

    def _next_seq(self, sid: int) -> int:
        seq = self._seq.get(sid, 0) + 1
        self._seq[sid] = seq
        return seq

    def skip_seq(self, channel: str, count: int = 1) -> None:
        """Advance sequence counter without producing a message (creates a gap)."""
        sid = _CHANNEL_SIDS[channel]
        current = self._seq.get(sid, 0)
        self._seq[sid] = current + count

    def fill(
        self,
        trade_id: str,
        order_id: str,
        ticker: str,
        side: str,
        action: str,
        count: int,
        price_cents: int,
        client_order_id: str | None = None,
        is_taker: bool = True,
        fee_cents: float = 0.0,
    ) -> bytes:
        """Produce a fill WS message as JSON bytes."""
        sid = _CHANNEL_SIDS["fill"]
        seq = self._next_seq(sid)
        price_dollars = f"{price_cents / 100:.2f}"

        msg: dict = {
            "trade_id": trade_id,
            "order_id": order_id,
            "side": side,
            "action": action,
            "count_fp": f"{count:.2f}",
            "is_taker": is_taker,
            "ts": 1_000_000_000,
        }
        if client_order_id is not None:
            msg["client_order_id"] = client_order_id
        if ticker is not None:
            msg["market_ticker"] = ticker
        # Both price fields present (they sum to $1)
        if side == "yes":
            msg["yes_price_dollars"] = price_dollars
            msg["no_price_dollars"] = f"{(100 - price_cents) / 100:.2f}"
        else:
            msg["no_price_dollars"] = price_dollars
            msg["yes_price_dollars"] = f"{(100 - price_cents) / 100:.2f}"
        if fee_cents > 0:
            msg["fee_cost"] = f"{fee_cents / 100:.4f}"

        envelope = {"type": "fill", "sid": sid, "seq": seq, "msg": msg}
        return msgspec.json.encode(envelope)

    def settlement_fill(
        self,
        trade_id: str,
        order_id: str,
        side: str,
        action: str,
        count: int,
    ) -> bytes:
        """Produce a settlement fill (market_ticker=None)."""
        sid = _CHANNEL_SIDS["fill"]
        seq = self._next_seq(sid)
        msg = {
            "trade_id": trade_id,
            "order_id": order_id,
            "market_ticker": None,
            "side": side,
            "action": action,
            "count_fp": f"{count:.2f}",
            "is_taker": False,
            "ts": 1_000_000_000,
        }
        envelope = {"type": "fill", "sid": sid, "seq": seq, "msg": msg}
        return msgspec.json.encode(envelope)

    def user_order(
        self,
        order_id: str,
        ticker: str,
        side: str,
        action: str,
        status: str,
        client_order_id: str | None = None,
    ) -> bytes:
        """Produce a user_order WS message."""
        sid = _CHANNEL_SIDS["user_order"]
        seq = self._next_seq(sid)
        msg: dict = {
            "order_id": order_id,
            "ticker": ticker,
            "status": status,
            "side": side,
            "action": action,
        }
        if client_order_id is not None:
            msg["client_order_id"] = client_order_id
        envelope = {"type": "user_order", "sid": sid, "seq": seq, "msg": msg}
        return msgspec.json.encode(envelope)
