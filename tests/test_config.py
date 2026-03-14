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


# ---------------------------------------------------------------------------
# Minor #4: exec config ws_url / rest_url properties (demo vs prod)
# ---------------------------------------------------------------------------

def test_exec_config_demo_urls():
    config = KalshiExecClientConfig(
        api_key_id="test", private_key_path="/tmp/key.pem",
    )
    assert "demo-api" in config.ws_url
    assert "demo-api" in config.rest_url


def test_exec_config_prod_urls():
    config = KalshiExecClientConfig(
        api_key_id="test", private_key_path="/tmp/key.pem",
        environment="production",
    )
    assert "api.elections.kalshi.com" in config.ws_url
    assert "api.elections.kalshi.com" in config.rest_url


def test_data_config_rest_url_demo():
    config = KalshiDataClientConfig(
        api_key_id="test", private_key_path="/tmp/key.pem",
    )
    assert "demo-api" in config.rest_url


def test_config_base_url_override():
    """base_url_http and base_url_ws fields override environment-derived URLs."""
    config = KalshiDataClientConfig(
        api_key_id="test", private_key_path="/tmp/key.pem",
        base_url_ws="wss://custom.example.com/ws",
        base_url_http="https://custom.example.com/http",
    )
    assert config.ws_url == "wss://custom.example.com/ws"
    assert config.rest_url == "https://custom.example.com/http"
