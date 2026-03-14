"""Kalshi API error types and retry logic."""
from kalshi_python.exceptions import ApiException


class KalshiApiError(Exception):
    """Kalshi API error with status code and raw response."""

    def __init__(self, status: int, message: str, raw: str = ""):
        self.status = status
        self.message = message
        self.raw = raw
        super().__init__(f"Kalshi API {status}: {message}")


def should_retry(exc: BaseException) -> bool:
    """Return True if the exception is retryable (429 or 5xx)."""
    if isinstance(exc, ApiException):
        return exc.status == 429 or (500 <= exc.status < 600)
    if isinstance(exc, KalshiApiError):
        return exc.status == 429 or (500 <= exc.status < 600)
    return False
