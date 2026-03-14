"""Tests for Kalshi config and factory classes."""
from kalshi.config import KalshiDataClientConfig, KalshiExecClientConfig
from kalshi.common.constants import KALSHI_VENUE


def test_data_config_defaults():
    config = KalshiDataClientConfig(
        api_key_id="test", private_key_path="/tmp/key.pem",
    )
    assert config.venue == KALSHI_VENUE
    assert config.environment == "demo"
    # Correction #24: use config.ws_url (property), not config.base_url_ws
    assert "demo-api" in config.ws_url


def test_exec_config_defaults():
    config = KalshiExecClientConfig(
        api_key_id="test", private_key_path="/tmp/key.pem",
    )
    assert config.venue == KALSHI_VENUE
    assert config.ack_timeout_secs == 5.0


def test_prod_config_uses_prod_urls():
    config = KalshiDataClientConfig(
        api_key_id="test", private_key_path="/tmp/key.pem",
        environment="production",
    )
    assert "api.elections.kalshi.com" in config.ws_url
