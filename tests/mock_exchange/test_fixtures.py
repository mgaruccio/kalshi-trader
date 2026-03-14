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
