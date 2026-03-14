import os
from tests.mock_exchange.fixtures import create_dummy_rsa_key


def test_dummy_rsa_key_creates_valid_pem():
    path = create_dummy_rsa_key()
    try:
        assert os.path.exists(path)
        with open(path, "rb") as f:
            data = f.read()
        assert b"BEGIN PRIVATE KEY" in data
    finally:
        os.unlink(path)


def test_dummy_rsa_key_loadable_by_kalshi_auth():
    path = create_dummy_rsa_key()
    try:
        from kalshi_python.api_client import KalshiAuth
        auth = KalshiAuth(key_id="test-key-id", private_key_path=path)
        assert auth is not None
    finally:
        os.unlink(path)
