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
