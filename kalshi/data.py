"""Kalshi data client — orderbook subscription and QuoteTick emission."""
import asyncio
import logging

from nautilus_trader.live.data_client import LiveMarketDataClient
from nautilus_trader.model.data import QuoteTick
from nautilus_trader.model.identifiers import ClientId, InstrumentId, Symbol
from nautilus_trader.model.enums import PriceType

from kalshi.common.constants import KALSHI_VENUE
from kalshi.providers import parse_instrument_id
from kalshi.websocket.client import KalshiWebSocketClient
from kalshi.websocket.types import (
    OrderbookSnapshotMsg,
    OrderbookDeltaMsg,
    decode_ws_msg,
)

log = logging.getLogger(__name__)


def _derive_quotes(
    ticker: str,
    yes_book: dict[float, float],
    no_book: dict[float, float],
) -> dict | None:
    """Derive YES and NO quote prices from bids-only orderbooks.

    Kalshi sends only bid prices. Ask prices are implied:
    - YES ask = 1.0 - max(no_bids)   (best no bid implies best yes ask)
    - NO ask  = 1.0 - max(yes_bids)  (best yes bid implies best no ask)

    Returns None if both books are empty (no valid quotes to emit).
    """
    if not yes_book and not no_book:
        return None

    yes_bid = max(yes_book.keys()) if yes_book else 0.0
    yes_bid_size = yes_book[yes_bid] if yes_book else 0.0
    no_bid = max(no_book.keys()) if no_book else 0.0
    no_bid_size = no_book[no_bid] if no_book else 0.0

    yes_ask = round(1.0 - no_bid, 10) if no_book else 1.0
    no_ask = round(1.0 - yes_bid, 10) if yes_book else 1.0
    yes_ask_size = no_bid_size
    no_ask_size = yes_bid_size

    return {
        "YES": {
            "bid": yes_bid, "ask": yes_ask,
            "bid_size": yes_bid_size, "ask_size": yes_ask_size,
        },
        "NO": {
            "bid": no_bid, "ask": no_ask,
            "bid_size": no_bid_size, "ask_size": no_ask_size,
        },
    }


class KalshiDataClient(LiveMarketDataClient):
    """Kalshi live market data client.

    Connects to the Kalshi WebSocket API and emits QuoteTick events for
    subscribed instruments derived from the bids-only orderbook.
    """

    def __init__(
        self,
        loop: asyncio.AbstractEventLoop,
        name: str | None,
        config,
        instrument_provider,
        msgbus,
        cache,
        clock,
    ):
        super().__init__(
            loop=loop,
            client_id=ClientId(name or "KALSHI"),  # Correction #26
            venue=KALSHI_VENUE,
            instrument_provider=instrument_provider,
            msgbus=msgbus,
            cache=cache,
            clock=clock,
            config=config,
        )
        self._config = config

        # Per-ticker orderbook state: {ticker: {"yes": {price: size}, "no": {price: size}}}
        self._books: dict[str, dict[str, dict[float, float]]] = {}

        self._ws_client = KalshiWebSocketClient(
            clock=clock,
            base_url=config.ws_url,
            channels=["orderbook_delta"],
            handler=self._handle_ws_message,
            api_key_id=config.api_key_id,
            private_key_path=config.private_key_path,
            loop=loop,
        )

    async def _connect(self) -> None:
        await self._instrument_provider.initialize()

        # Publish instruments to the data engine
        for instrument in self._instrument_provider.get_all().values():
            self._handle_data(instrument)

        await self._ws_client.connect()
        self._set_connected()

    async def _disconnect(self) -> None:
        await self._ws_client.disconnect()
        self._set_disconnected()

    async def _subscribe_quote_ticks(self, command) -> None:
        instrument_id = command.instrument_id
        ticker, _ = parse_instrument_id(instrument_id)
        if ticker not in self._books:
            self._books[ticker] = {"yes": {}, "no": {}}
        await self._ws_client.subscribe_ticker(ticker)

    async def _unsubscribe_quote_ticks(self, command) -> None:
        instrument_id = command.instrument_id
        ticker, _ = parse_instrument_id(instrument_id)
        self._ws_client.remove_ticker(ticker)

    def _handle_ws_message(self, raw: bytes) -> None:
        try:
            msg_type, sid, seq, msg = decode_ws_msg(raw)

            if not self._ws_client._check_sequence(sid=sid, seq=seq):
                # Sequence gap — clear books and resubscribe
                log.warning(f"Sequence gap detected for sid={sid}, clearing books")
                self._books.clear()
                asyncio.create_task(self._resubscribe_all())
                return

            if msg_type == "orderbook_snapshot":
                self._handle_orderbook_snapshot(msg)
            elif msg_type == "orderbook_delta":
                self._handle_orderbook_delta(msg)
        except Exception as e:
            log.error(f"Error handling WS message: {e}")

    def _handle_orderbook_snapshot(self, msg: OrderbookSnapshotMsg) -> None:
        ticker = msg.market_ticker
        yes_book: dict[float, float] = {}
        no_book: dict[float, float] = {}

        for level in msg.yes_dollars_fp:
            price, size = float(level[0]), float(level[1])
            if size > 0:
                yes_book[price] = size

        for level in msg.no_dollars_fp:
            price, size = float(level[0]), float(level[1])
            if size > 0:
                no_book[price] = size

        self._books[ticker] = {"yes": yes_book, "no": no_book}
        self._emit_quotes(ticker)

    def _handle_orderbook_delta(self, msg: OrderbookDeltaMsg) -> None:
        ticker = msg.market_ticker
        if ticker not in self._books:
            self._books[ticker] = {"yes": {}, "no": {}}

        book = self._books[ticker]
        side_key = "yes" if msg.side == "yes" else "no"
        price = float(msg.price_dollars)
        delta = float(msg.delta_fp)

        if delta == 0.0:
            # Remove the level
            book[side_key].pop(price, None)
        else:
            book[side_key][price] = delta

        self._emit_quotes(ticker)

    def _emit_quotes(self, ticker: str) -> None:
        book = self._books.get(ticker)
        if not book:
            return

        quotes = _derive_quotes(ticker, book["yes"], book["no"])
        if quotes is None:
            return

        ts = self._clock.timestamp_ns()

        for side in ("YES", "NO"):
            q = quotes[side]
            instrument_id = InstrumentId(Symbol(f"{ticker}-{side}"), KALSHI_VENUE)
            instrument = self._instrument_provider.find(instrument_id)
            if not instrument:
                continue

            tick = QuoteTick(
                instrument_id=instrument_id,
                bid_price=instrument.make_price(q["bid"]),
                ask_price=instrument.make_price(q["ask"]),
                bid_size=instrument.make_qty(q["bid_size"]),
                ask_size=instrument.make_qty(q["ask_size"]),
                ts_event=ts,
                ts_init=ts,
            )
            self._handle_data(tick)

    async def _resubscribe_all(self) -> None:
        """Re-subscribe all tickers after a sequence gap."""
        for ticker in list(self._ws_client._subscribed_tickers):
            await self._ws_client.subscribe_ticker(ticker)
