"""Tests for KalshiWebSocketClient — subscription management and auth."""
import asyncio
import json
from unittest.mock import AsyncMock, MagicMock

from kalshi.websocket.client import KalshiWebSocketClient


def _make_client():
    clock = MagicMock()
    clock.timestamp_ns.return_value = 1_000_000_000
    return KalshiWebSocketClient(
        clock=clock,
        base_url="wss://demo-api.kalshi.co/trade-api/ws/v2",
        channels=["orderbook_delta"],
        handler=lambda raw: None,
        api_key_id="test-key",
        private_key_path="/dev/null",
        loop=asyncio.new_event_loop(),
    )


def test_init_sets_state():
    client = _make_client()
    assert client._channels == ["orderbook_delta"]
    assert client._subscribed_tickers == set()
    assert client._ws_client is None


def test_add_subscription_tracks_tickers():
    client = _make_client()
    client.add_ticker("KXHIGHCHI-26MAR14-T55")
    assert "KXHIGHCHI-26MAR14-T55" in client._subscribed_tickers


def test_remove_ticker():
    client = _make_client()
    client.add_ticker("KXHIGHCHI-26MAR14-T55")
    client.remove_ticker("KXHIGHCHI-26MAR14-T55")
    assert "KXHIGHCHI-26MAR14-T55" not in client._subscribed_tickers


def test_remove_nonexistent_ticker_no_error():
    client = _make_client()
    client.remove_ticker("DOESNOTEXIST")  # should not raise


def test_sequence_gap_detected():
    client = _make_client()
    assert client._check_sequence(sid=1, seq=1) is True
    assert client._check_sequence(sid=1, seq=2) is True
    assert client._check_sequence(sid=1, seq=5) is False


def test_sequence_first_message_always_ok():
    client = _make_client()
    assert client._check_sequence(sid=99, seq=42) is True


def test_sequence_different_sids_independent():
    client = _make_client()
    assert client._check_sequence(sid=1, seq=1) is True
    assert client._check_sequence(sid=2, seq=1) is True
    assert client._check_sequence(sid=1, seq=2) is True
    assert client._check_sequence(sid=2, seq=2) is True


def test_subscribe_ticker_sends_correct_json_when_connected():
    """subscribe_ticker sends a subscribe cmd when _ws_client is active."""
    async def _run():
        client = _make_client()
        mock_ws = MagicMock()
        mock_ws.send_text = AsyncMock()
        client._ws_client = mock_ws

        await client.subscribe_ticker("KXHIGHCHI-26MAR14-T55")

        assert "KXHIGHCHI-26MAR14-T55" in client._subscribed_tickers
        mock_ws.send_text.assert_called_once()
        raw = mock_ws.send_text.call_args[0][0]
        payload = json.loads(raw)
        assert payload["cmd"] == "subscribe"
        assert "KXHIGHCHI-26MAR14-T55" in payload["params"]["market_tickers"]
        assert payload["params"]["channels"] == ["orderbook_delta"]

    asyncio.run(_run())
