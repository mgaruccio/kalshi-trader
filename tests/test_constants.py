"""Tests for kalshi.common.constants."""
from nautilus_trader.model.identifiers import Venue


def test_kalshi_venue_is_venue():
    from kalshi.common.constants import KALSHI_VENUE
    assert isinstance(KALSHI_VENUE, Venue)
    assert KALSHI_VENUE.value == "KALSHI"


def test_base_urls_are_strings():
    from kalshi.common.constants import (
        DEMO_REST_URL, DEMO_WS_URL, PROD_REST_URL, PROD_WS_URL,
    )
    assert "demo-api.kalshi.co" in DEMO_REST_URL
    assert "demo-api.kalshi.co" in DEMO_WS_URL
    assert "api.elections.kalshi.com" in PROD_REST_URL
    assert "api.elections.kalshi.com" in PROD_WS_URL


def test_max_batch_create_is_nine():
    """Correction #5: MAX_BATCH_CREATE must be 9, not 20."""
    from kalshi.common.constants import MAX_BATCH_CREATE
    assert MAX_BATCH_CREATE == 9
