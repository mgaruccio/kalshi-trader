"""Kalshi WebSocket client wrapping nautilus_pyo3.WebSocketClient."""

import asyncio
import logging

import msgspec

from nautilus_trader.core.nautilus_pyo3 import WebSocketClient, WebSocketConfig

log = logging.getLogger(__name__)


class KalshiWebSocketClient:
    def __init__(
        self, clock, base_url, channels, handler, api_key_id, private_key_path, loop
    ):
        self._clock = clock
        self._base_url = base_url
        self._channels = channels
        self._handler = handler
        self._loop = loop
        self._api_key_id = api_key_id
        self._private_key_path = private_key_path

        # Lazy: KalshiAuth loads PEM on init — defer until connect()
        self._auth = None

        self._ws_client: WebSocketClient | None = None
        self._subscribed_tickers: set[str] = set()
        self._seq_tracker: dict[int, int] = {}
        self._next_cmd_id = 1
        self._sub_lock = asyncio.Lock()  # Correction #25

    def _get_auth(self):
        """Lazily construct KalshiAuth to avoid PEM load at init time."""
        if self._auth is None:
            from kalshi_python.api_client import KalshiAuth

            self._auth = KalshiAuth(self._api_key_id, self._private_key_path)
        return self._auth

    def add_ticker(self, ticker: str) -> None:
        self._subscribed_tickers.add(ticker)

    def remove_ticker(self, ticker: str) -> None:
        self._subscribed_tickers.discard(ticker)

    def _check_sequence(self, sid: int, seq: int) -> bool:
        """Return False on sequence gap; caller must clear book and resubscribe (Correction #7)."""
        last = self._seq_tracker.get(sid)
        self._seq_tracker[sid] = seq
        if last is None:
            return True
        if seq != last + 1:
            log.warning(f"Sequence gap on sid={sid}: expected {last + 1}, got {seq}")
            return False
        return True

    async def connect(self) -> None:
        await self._build_and_connect()
        await self._subscribe_all()

    async def _build_and_connect(self) -> None:
        # Correction #2: Fresh RSA-PSS auth headers each time (timestamps expire)
        auth = self._get_auth()
        headers = auth.create_auth_headers("GET", "/trade-api/ws/v2")
        header_list = list(headers.items())
        config = WebSocketConfig(
            url=self._base_url,
            headers=header_list,
            heartbeat=None,
            reconnect_timeout_ms=10_000,
            reconnect_delay_initial_ms=2_000,
            reconnect_delay_max_ms=30_000,
            reconnect_backoff_factor=1.5,
        )
        self._ws_client = await WebSocketClient.connect(
            loop_=self._loop,
            config=config,
            handler=self._handler,
            post_reconnection=self._on_reconnect,
        )

    async def _subscribe_all(self) -> None:
        # Correction #34: user_orders / fill channels don't need market_tickers.
        # Subscribe whenever there are channels to subscribe to, even with no
        # tickers (exec client case). Data channels without tickers are no-ops
        # on the server but harmless.
        async with self._sub_lock:
            if not self._ws_client or (
                not self._channels and not self._subscribed_tickers
            ):
                return
            cmd = {
                "id": self._next_cmd_id,
                "cmd": "subscribe",
                "params": {
                    "channels": self._channels,
                    "market_tickers": list(self._subscribed_tickers),
                },
            }
            self._next_cmd_id += 1
            await self._ws_client.send_text(msgspec.json.encode(cmd))

    async def subscribe_ticker(self, ticker: str) -> None:
        self.add_ticker(ticker)
        async with self._sub_lock:
            if self._ws_client:
                cmd = {
                    "id": self._next_cmd_id,
                    "cmd": "subscribe",
                    "params": {"channels": self._channels, "market_tickers": [ticker]},
                }
                self._next_cmd_id += 1
                await self._ws_client.send_text(msgspec.json.encode(cmd))

    def _on_reconnect(self) -> None:
        # Correction #2 + #11: tracked task with done_callback; fresh auth headers
        self._seq_tracker.clear()
        log.info("WebSocket reconnected, rebuilding with fresh auth...")
        task = asyncio.ensure_future(self._rebuild_and_resubscribe())
        task.add_done_callback(self._reconnect_done)

    async def _rebuild_and_resubscribe(self) -> None:
        if self._ws_client:
            await self._ws_client.disconnect()
        await self._build_and_connect()
        await self._subscribe_all()

    @staticmethod
    def _reconnect_done(task: asyncio.Task) -> None:
        if task.exception():
            log.error(f"Reconnect failed: {task.exception()}")

    async def disconnect(self) -> None:
        if self._ws_client:
            await self._ws_client.disconnect()
            self._ws_client = None

    @property
    def is_connected(self) -> bool:
        return self._ws_client is not None and self._ws_client.is_active()
