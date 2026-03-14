"""Test fixtures for mock exchange — dummy RSA key and shared constants."""
import tempfile
import os

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa


# Test market constants
TEST_TICKER = "KXHIGHCHI-26MAR15-T42"
TEST_SERIES = "KXHIGH"
TEST_TITLE = "Will the high temperature in Chicago be 42°F or above on March 15?"
TEST_CLOSE_TIME = "2026-03-16T00:00:00Z"


def create_dummy_rsa_key() -> str:
    """Create a temporary RSA private key PEM file. Returns file path."""
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    fd, path = tempfile.mkstemp(suffix=".pem")
    os.write(fd, pem)
    os.close(fd)
    return path
