"""SignalActor — consumes signal server WebSocket, publishes NT data events."""
from __future__ import annotations

import asyncio
import datetime
import json
import logging

import httpx
import websockets.asyncio.client

from nautilus_trader.common.actor import Actor
from nautilus_trader.config import ActorConfig
from nautilus_trader.model.data import DataType

from kalshi.signals import ForecastDrift, SignalScore

log = logging.getLogger(__name__)


class SignalActorConfig(ActorConfig, frozen=True):
    """Configuration for the SignalActor."""

    signal_ws_url: str = "ws://localhost:8000/v1/stream"
    signal_http_url: str = "http://localhost:8000"
    component_id: str = "SignalActor-001"


def parse_score_msg(msg: dict, ts_ns: int) -> SignalScore | None:
    """Parse a signal server score dict into a SignalScore data event.

    Returns None if the message fails validation (Enhancement #13).
    Logs a WARNING for invalid messages.
    """
    no_p_win = float(msg.get("no_p_win", 0.0))
    yes_p_win = float(msg.get("yes_p_win", 0.0))
    n_models = int(msg.get("n_models", 0))
    yes_bid = int(msg.get("yes_bid", 0))

    if not (0.0 <= no_p_win <= 1.0):
        log.warning(f"parse_score_msg: no_p_win={no_p_win} out of [0, 1] — dropping")
        return None
    if not (0.0 <= yes_p_win <= 1.0):
        log.warning(f"parse_score_msg: yes_p_win={yes_p_win} out of [0, 1] — dropping")
        return None
    if not (1 <= n_models <= 3):
        log.warning(f"parse_score_msg: n_models={n_models} out of [1, 3] — dropping")
        return None
    if not (0 <= yes_bid <= 99):
        log.warning(f"parse_score_msg: yes_bid={yes_bid} out of [0, 99] — dropping")
        return None

    return SignalScore(
        ticker=str(msg["ticker"]),
        city=str(msg["city"]),
        threshold=float(msg["threshold"]),
        direction=str(msg["direction"]),
        no_p_win=no_p_win,
        yes_p_win=yes_p_win,
        no_margin=float(msg["no_margin"]),
        n_models=n_models,
        emos_no=float(msg.get("emos_no", 0.0)),
        ngboost_no=float(msg.get("ngboost_no", 0.0)),
        drn_no=float(msg.get("drn_no", 0.0)),
        yes_bid=yes_bid,
        yes_ask=int(msg.get("yes_ask", 0)),
        status=str(msg.get("status", "")),
        ts_event=ts_ns,
        ts_init=ts_ns,
    )


def parse_alert_msg(msg: dict, ts_ns: int) -> ForecastDrift | None:
    """Parse a signal server alert into a ForecastDrift, or None if not a drift alert."""
    if msg.get("alert_type") != "forecast_drift":
        return None
    return ForecastDrift(
        city=str(msg["city"]),
        date=str(msg["date"]),
        message=str(msg["message"]),
        ts_event=ts_ns,
        ts_init=ts_ns,
    )


class SignalActor(Actor):
    """Consumes signal server WebSocket and publishes SignalScore/ForecastDrift on the MessageBus."""

    def __init__(self, config: SignalActorConfig) -> None:
        super().__init__(config)
        self._config = config
        self._ws_task: asyncio.Task | None = None
        self._ws: websockets.asyncio.client.ClientConnection | None = None
        self._ws_dead = False  # set by done_callback if task exits unexpectedly
        self._score_data_type = DataType(SignalScore)
        self._drift_data_type = DataType(ForecastDrift)
        self._pong_missed = 0

    def on_start(self) -> None:
        # Capture event loop — timer callbacks can't call get_running_loop() (collector.py:39 pattern)
        self._loop = asyncio.get_running_loop()
        self._ws_task = self._loop.create_task(self._run_ws())
        # Enhancement #14: done_callback surfaces silent WS task death
        self._ws_task.add_done_callback(self._on_ws_task_done)
        # Enhancement #12: heartbeat ping every 30s
        self.clock.set_timer(
            "signal_ping",
            interval=datetime.timedelta(seconds=30),
            callback=self._send_ping,
        )

    def on_stop(self) -> None:
        if self._ws_task and not self._ws_task.done():
            self._ws_task.cancel()

    def _on_ws_task_done(self, task: asyncio.Task) -> None:
        """Log CRITICAL if WS task exits unexpectedly (Enhancement #14)."""
        if task.cancelled():
            return  # normal shutdown via on_stop
        exc = task.exception()
        if exc is not None:
            self._ws_dead = True
            self.log.critical(f"Signal WS task died with exception: {exc}")

    def _send_ping(self, event=None) -> None:
        """Send ping to signal server; track missed pongs (Enhancement #12)."""
        if self._ws_task is None or self._ws_task.done():
            return
        # Actually send the ping — server responds with pong which decrements _pong_missed
        if self._ws is not None:
            asyncio.ensure_future(
                self._ws.send(json.dumps({"type": "ping"})),
                loop=self._loop,
            )
        self._pong_missed += 1
        if self._pong_missed >= 2:
            self.log.warning(
                f"Signal server missed {self._pong_missed} pongs — reconnecting"
            )
            if self._ws_task and not self._ws_task.done():
                self._ws_task.cancel()
            self._ws_task = self._loop.create_task(self._run_ws())
            self._ws_task.add_done_callback(self._on_ws_task_done)
            self._pong_missed = 0

    async def _bootstrap(self) -> None:
        """Fetch initial scores from REST endpoint and publish.

        Enhancement #15: called inside reconnect loop after subscribing.
        """
        url = f"{self._config.signal_http_url}/v1/trading/scores"
        async with httpx.AsyncClient() as client:
            resp = await client.get(url, timeout=30.0)
            resp.raise_for_status()
            scores = resp.json()

        ts = self.clock.timestamp_ns()
        published = 0
        for item in scores:
            score = parse_score_msg(item, ts)
            if score is not None:
                self.publish_data(self._score_data_type, score)
                published += 1

        self.log.info(f"Bootstrapped {published}/{len(scores)} scores from REST")

    async def _run_ws(self) -> None:
        """Connect to signal server WebSocket and process messages (Enhancement #15: re-bootstrap after reconnect)."""
        async for ws in websockets.asyncio.client.connect(self._config.signal_ws_url):
            self._ws = ws
            try:
                subscribe_msg = json.dumps({
                    "type": "subscribe",
                    "channels": ["scores", "alerts"],
                    "cities": [],
                })
                await ws.send(subscribe_msg)
                self.log.info("Subscribed to signal server [scores, alerts]")

                # Enhancement #15: bootstrap inside reconnect loop
                await self._bootstrap()
                self._pong_missed = 0

                async for raw in ws:
                    self._handle_ws_message(raw)

            except Exception as e:
                self.log.warning(f"Signal server WS error: {e}, reconnecting...")
                continue
            finally:
                self._ws = None

    def _handle_ws_message(self, raw: str | bytes) -> None:
        """Route incoming WS message to appropriate handler.

        Enhancement #16: try/except around parsing — malformed JSON logs + continues.
        """
        try:
            msg = json.loads(raw)
        except (json.JSONDecodeError, ValueError) as e:
            self.log.warning(f"Malformed signal server message, skipping: {e}")
            return

        try:
            msg_type = msg.get("type", "")
            ts = self.clock.timestamp_ns()

            if msg_type == "scores_update":
                contracts = msg.get("contracts", msg.get("scores", []))
                for item in contracts:
                    score = parse_score_msg(item, ts)
                    if score is not None:
                        self.publish_data(self._score_data_type, score)

            elif msg_type == "alert":
                # Enhancement #18: drift persists until session end — no auto-clear
                drift = parse_alert_msg(msg, ts)
                if drift is not None:
                    self.publish_data(self._drift_data_type, drift)

            elif msg_type == "pong":
                # Enhancement #12: track pong responses
                self._pong_missed = max(0, self._pong_missed - 1)

            else:
                self.log.debug(f"Unhandled signal server message type: {msg_type!r}")

        except Exception as e:
            self.log.warning(f"Error processing signal server message: {e}")
