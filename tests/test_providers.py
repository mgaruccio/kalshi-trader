"""Tests for KalshiInstrumentProvider — instrument creation and loading."""
import re
from decimal import Decimal
from unittest.mock import MagicMock

import pytest
from nautilus_trader.model.identifiers import InstrumentId, Symbol, Venue
from nautilus_trader.model.instruments import BinaryOption
from nautilus_trader.model.enums import AssetClass

from kalshi.common.constants import KALSHI_VENUE
from kalshi.providers import KalshiInstrumentProvider, parse_instrument_id


def test_parse_instrument_id_yes():
    ticker, side = parse_instrument_id(
        InstrumentId(Symbol("KXHIGHCHI-26MAR14-T55-YES"), KALSHI_VENUE)
    )
    assert ticker == "KXHIGHCHI-26MAR14-T55"
    assert side == "yes"


def test_parse_instrument_id_no():
    ticker, side = parse_instrument_id(
        InstrumentId(Symbol("KXHIGHCHI-26MAR14-T55-NO"), KALSHI_VENUE)
    )
    assert ticker == "KXHIGHCHI-26MAR14-T55"
    assert side == "no"


def test_parse_instrument_id_invalid():
    with pytest.raises(ValueError, match="Cannot parse side"):
        parse_instrument_id(
            InstrumentId(Symbol("KXHIGHCHI-26MAR14-T55"), KALSHI_VENUE)
        )


def _mock_market(ticker="KXHIGHCHI-26MAR14-T55", status="active"):
    m = MagicMock()
    m.ticker = ticker
    m.status = status
    m.title = "Will Chicago high temp be 55F or above on Mar 14?"
    m.close_time = "2026-03-14T12:00:00Z"
    m.expiration_time = "2026-03-15T00:00:00Z"
    return m


def test_build_instrument_creates_binary_option():
    provider = KalshiInstrumentProvider.__new__(KalshiInstrumentProvider)
    instrument = provider._build_instrument(_mock_market(), "YES")
    assert isinstance(instrument, BinaryOption)
    assert instrument.id == InstrumentId(Symbol("KXHIGHCHI-26MAR14-T55-YES"), KALSHI_VENUE)
    assert instrument.asset_class == AssetClass.ALTERNATIVE
    assert instrument.price_precision == 2
    assert instrument.size_precision == 0
    assert instrument.outcome == "Yes"
    assert instrument.info["kalshi_ticker"] == "KXHIGHCHI-26MAR14-T55"


def test_build_instrument_no_side():
    provider = KalshiInstrumentProvider.__new__(KalshiInstrumentProvider)
    instrument = provider._build_instrument(_mock_market(), "NO")
    assert instrument.id == InstrumentId(Symbol("KXHIGHCHI-26MAR14-T55-NO"), KALSHI_VENUE)
    assert instrument.outcome == "No"


def test_build_instrument_description_is_none_not_empty():
    """Correction #15: BinaryOption.description must be None, not empty string."""
    provider = KalshiInstrumentProvider.__new__(KalshiInstrumentProvider)
    market = _mock_market()
    market.title = None
    instrument = provider._build_instrument(market, "YES")
    assert instrument.description is None


def test_build_instrument_observation_date_from_ticker():
    """Correction #17: Observation date comes from ticker, not close_time."""
    provider = KalshiInstrumentProvider.__new__(KalshiInstrumentProvider)
    instrument = provider._build_instrument(_mock_market(), "YES")
    assert instrument.info.get("observation_date") == "2026-03-14"


# ---------------------------------------------------------------------------
# _parse_observation_date edge cases (Minor #5)
# ---------------------------------------------------------------------------

class TestParseObservationDate:
    def test_valid_ticker(self):
        assert KalshiInstrumentProvider._parse_observation_date("KXHIGHCHI-26MAR14-T55") == "2026-03-14"

    def test_no_date_pattern_returns_none(self):
        assert KalshiInstrumentProvider._parse_observation_date("NOPATTERNTICKER") is None

    def test_unrecognized_month_returns_none(self):
        # "XYZ" is not a valid month abbreviation
        assert KalshiInstrumentProvider._parse_observation_date("KXHIGH-26XYZ14-T55") is None

    def test_different_month(self):
        assert KalshiInstrumentProvider._parse_observation_date("KXHIGHNYC-26DEC31-T40") == "2026-12-31"


# ---------------------------------------------------------------------------
# _load_all_sync (Bug #2)
# ---------------------------------------------------------------------------

class TestLoadAllSync:
    def _make_provider(self):
        provider = KalshiInstrumentProvider.__new__(KalshiInstrumentProvider)
        provider._instruments = {}
        provider._instrument_ids = set()
        return provider

    def _mock_resp(self, markets, cursor=None):
        resp = MagicMock()
        resp.markets = markets
        resp.cursor = cursor
        return resp

    def test_active_open_unopened_markets_loaded(self):
        provider = self._make_provider()
        markets = [
            _mock_market("TICKER-A", "active"),
            _mock_market("TICKER-B", "open"),
            _mock_market("TICKER-C", "unopened"),
        ]
        provider._markets_api = MagicMock()
        provider._markets_api.get_markets.return_value = self._mock_resp(markets)

        provider._load_all_sync({"statuses": ["active"]})
        # 3 markets × 2 sides = 6 instruments
        assert len(provider._instruments) == 6

    def test_other_statuses_skipped(self):
        provider = self._make_provider()
        markets = [
            _mock_market("TICKER-SETTLED", "settled"),
            _mock_market("TICKER-OK", "active"),
        ]
        provider._markets_api = MagicMock()
        provider._markets_api.get_markets.return_value = self._mock_resp(markets)

        provider._load_all_sync({"statuses": ["active"]})
        # Only the active market → 2 instruments (YES + NO)
        assert len(provider._instruments) == 2

    def test_cursor_pagination_terminates_on_none(self):
        provider = self._make_provider()
        page1 = self._mock_resp([_mock_market("TICKER-P1", "active")], cursor="next_page")
        page2 = self._mock_resp([_mock_market("TICKER-P2", "active")], cursor=None)
        provider._markets_api = MagicMock()
        provider._markets_api.get_markets.side_effect = [page1, page2]

        provider._load_all_sync({"statuses": ["active"]})
        assert provider._markets_api.get_markets.call_count == 2
        assert len(provider._instruments) == 4  # 2 markets × 2 sides

    def test_both_yes_and_no_created_per_market(self):
        provider = self._make_provider()
        provider._markets_api = MagicMock()
        provider._markets_api.get_markets.return_value = self._mock_resp(
            [_mock_market("KXHIGHCHI-26MAR14-T55", "active")]
        )

        provider._load_all_sync({"statuses": ["active"]})
        ids = list(provider._instruments.keys())
        symbols = [str(i) for i in ids]
        assert any("YES" in s for s in symbols)
        assert any("NO" in s for s in symbols)
