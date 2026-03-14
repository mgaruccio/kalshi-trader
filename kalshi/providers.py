"""Kalshi instrument provider — loads markets and creates BinaryOption instruments."""
import asyncio
import logging
import re
from datetime import datetime, timezone
from decimal import Decimal

import kalshi_python
from kalshi_python.api.markets_api import MarketsApi

from nautilus_trader.common.providers import InstrumentProvider
from nautilus_trader.model.enums import AssetClass
from nautilus_trader.model.identifiers import InstrumentId, Symbol
from nautilus_trader.model.instruments import BinaryOption
from nautilus_trader.model.objects import Currency, Price, Quantity

from kalshi.common.constants import KALSHI_VENUE

log = logging.getLogger(__name__)


def parse_instrument_id(instrument_id: InstrumentId) -> tuple[str, str]:
    """Extract (ticker, side) from an InstrumentId like 'TICKER-YES.KALSHI'."""
    val = instrument_id.symbol.value
    if val.endswith("-YES"):
        return val[:-4], "yes"
    elif val.endswith("-NO"):
        return val[:-3], "no"
    else:
        raise ValueError(
            f"Cannot parse side from instrument {val!r}. "
            f"Expected suffix '-YES' or '-NO'."
        )


class KalshiInstrumentProvider(InstrumentProvider):
    """Loads Kalshi markets and provides BinaryOption instruments."""

    def __init__(
        self,
        api_key_id: str,
        private_key_path: str,
        rest_host: str,
    ):
        super().__init__()
        self._api_config = kalshi_python.Configuration()
        self._api_config.host = rest_host
        self._api_config.request_timeout = 10  # Correction #19
        self._client = kalshi_python.KalshiClient(self._api_config)
        self._client.set_kalshi_auth(
            key_id=api_key_id, private_key_path=private_key_path,
        )
        self._markets_api = MarketsApi(self._client)

    async def load_all_async(
        self,
        filters: dict | None = None,
    ) -> None:
        """Load instruments from Kalshi API. Filters: series_ticker, status, event_ticker.

        Correction #21: Loading failures are fatal — do not catch. Let propagate.
        """
        await asyncio.to_thread(self._load_all_sync, filters)

    def _load_all_sync(self, filters: dict | None = None) -> None:
        """Synchronous market loading — called via asyncio.to_thread."""
        filters = filters or {}
        series_ticker = filters.get("series_ticker")
        statuses = filters.get("statuses", ["active", "open", "unopened"])

        for status in statuses:
            cursor = None
            while True:
                resp = self._markets_api.get_markets(
                    limit=200,
                    cursor=cursor,
                    status=status,
                    series_ticker=series_ticker,
                )
                for m in getattr(resp, "markets", None) or []:
                    if m.status not in ("active", "open", "unopened"):
                        continue
                    self.add(self._build_instrument(m, "YES"))
                    self.add(self._build_instrument(m, "NO"))

                cursor = getattr(resp, "cursor", None)
                if not cursor:
                    break

        log.info(f"Loaded {self.count} instruments from Kalshi")

    def _build_instrument(self, market, side: str) -> BinaryOption:
        """Create a BinaryOption instrument from a Kalshi market object."""
        ticker = market.ticker
        instrument_id = InstrumentId(
            Symbol(f"{ticker}-{side}"), KALSHI_VENUE,
        )

        # Correction #17: Parse observation date from ticker, not close_time
        observation_date = self._parse_observation_date(ticker)

        # Parse expiration from market close_time
        expiration_ns = 0
        close_time = getattr(market, "close_time", None) or getattr(market, "expiration_time", None)
        if close_time:
            try:
                dt = datetime.fromisoformat(close_time.replace("Z", "+00:00"))
                expiration_ns = int(dt.timestamp() * 1_000_000_000)
            except (ValueError, AttributeError):
                pass

        ts_now = int(datetime.now(timezone.utc).timestamp() * 1_000_000_000)

        # Correction #15: description must be None, not empty string
        title = getattr(market, "title", None)
        description = title if title else None

        return BinaryOption(
            instrument_id=instrument_id,
            raw_symbol=Symbol(f"{ticker}-{side}"),
            asset_class=AssetClass.ALTERNATIVE,
            currency=Currency.from_str("USD"),
            price_precision=2,
            size_precision=0,
            price_increment=Price.from_str("0.01"),
            size_increment=Quantity.from_int(1),
            activation_ns=0,
            expiration_ns=expiration_ns,
            ts_event=ts_now,
            ts_init=ts_now,
            maker_fee=Decimal("0.0175"),
            taker_fee=Decimal("0.07"),
            outcome="Yes" if side == "YES" else "No",
            description=description,
            info={
                "kalshi_ticker": ticker,
                "side": side.lower(),
                "status": getattr(market, "status", ""),
                "observation_date": observation_date,
            },
        )

    @staticmethod
    def _parse_observation_date(ticker: str) -> str | None:
        """Extract observation date from ticker like KXHIGHCHI-26MAR14-T55."""
        match = re.search(r"-(\d{2})([A-Z]{3})(\d{2})-", ticker)
        if not match:
            return None
        year, month_str, day = match.groups()  # ticker format: YYMONDD
        months = {
            "JAN": "01", "FEB": "02", "MAR": "03", "APR": "04",
            "MAY": "05", "JUN": "06", "JUL": "07", "AUG": "08",
            "SEP": "09", "OCT": "10", "NOV": "11", "DEC": "12",
        }
        month = months.get(month_str)
        if not month:
            return None
        return f"20{year}-{month}-{day}"
