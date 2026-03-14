"""Tests for kalshi.common.errors."""
from kalshi_python.exceptions import ApiException
from kalshi.common.errors import KalshiApiError, should_retry


def test_should_retry_on_429():
    exc = ApiException(status=429, reason="Too Many Requests")
    assert should_retry(exc) is True


def test_should_retry_on_500():
    exc = ApiException(status=500, reason="Internal Server Error")
    assert should_retry(exc) is True


def test_should_retry_on_502():
    exc = ApiException(status=502, reason="Bad Gateway")
    assert should_retry(exc) is True


def test_should_not_retry_on_400():
    exc = ApiException(status=400, reason="Bad Request")
    assert should_retry(exc) is False


def test_should_not_retry_on_401():
    exc = ApiException(status=401, reason="Unauthorized")
    assert should_retry(exc) is False


def test_should_not_retry_on_non_api_exception():
    assert should_retry(ValueError("bad")) is False


def test_kalshi_api_error():
    err = KalshiApiError(status=429, message="rate limited", raw="...")
    assert err.status == 429
    assert "rate limited" in str(err)


def test_kalshi_api_error_is_retryable():
    err = KalshiApiError(status=503, message="unavailable")
    assert should_retry(err) is True


def test_kalshi_api_error_400_not_retryable():
    err = KalshiApiError(status=400, message="bad request")
    assert should_retry(err) is False
