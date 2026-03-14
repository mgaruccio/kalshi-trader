"""Kalshi adapter configuration."""
from nautilus_trader.config import LiveDataClientConfig, LiveExecClientConfig, PositiveFloat, PositiveInt
from nautilus_trader.model.identifiers import Venue

from kalshi.common.constants import (
    KALSHI_VENUE,
    DEMO_REST_URL, DEMO_WS_URL,
    PROD_REST_URL, PROD_WS_URL,
)


class KalshiDataClientConfig(LiveDataClientConfig, frozen=True):
    venue: Venue = KALSHI_VENUE
    api_key_id: str = ""
    private_key_path: str = ""
    environment: str = "demo"  # "demo" or "production"
    base_url_http: str | None = None  # override auto-detected URL
    base_url_ws: str | None = None
    series_ticker: str | None = None  # filter instruments on load
    update_instruments_interval_mins: PositiveInt | None = None

    @property
    def rest_url(self) -> str:
        if self.base_url_http:
            return self.base_url_http
        return PROD_REST_URL if self.environment == "production" else DEMO_REST_URL

    @property
    def ws_url(self) -> str:
        if self.base_url_ws:
            return self.base_url_ws
        return PROD_WS_URL if self.environment == "production" else DEMO_WS_URL


class KalshiExecClientConfig(LiveExecClientConfig, frozen=True):
    venue: Venue = KALSHI_VENUE
    api_key_id: str = ""
    private_key_path: str = ""
    environment: str = "demo"
    base_url_http: str | None = None
    base_url_ws: str | None = None
    max_retries: PositiveInt | None = 3
    retry_delay_initial_ms: PositiveInt | None = 1_000
    retry_delay_max_ms: PositiveInt | None = 10_000
    ack_timeout_secs: PositiveFloat = 5.0

    @property
    def rest_url(self) -> str:
        if self.base_url_http:
            return self.base_url_http
        return PROD_REST_URL if self.environment == "production" else DEMO_REST_URL

    @property
    def ws_url(self) -> str:
        if self.base_url_ws:
            return self.base_url_ws
        return PROD_WS_URL if self.environment == "production" else DEMO_WS_URL
