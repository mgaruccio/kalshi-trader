"""Load signal server backfill data into SignalScore events for backtesting."""
from __future__ import annotations

from datetime import datetime, timezone

import httpx

from kalshi.signal_actor import parse_score_msg
from kalshi.signals import SignalScore


def _iso_to_ns(ts_str: str) -> int:
    """Convert ISO 8601 timestamp string to nanoseconds since epoch.

    Handles both 'Z' and '+00:00' UTC suffixes.
    """
    dt = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
    return int(dt.timestamp() * 1_000_000_000)


def parse_backfill_response(data: list[dict]) -> list[SignalScore]:
    """Parse backfill response into chronologically sorted list of SignalScore events.

    Items that fail validation in parse_score_msg are silently dropped.
    """
    scores: list[SignalScore] = []
    for item in data:
        ts_ns = _iso_to_ns(item["timestamp"])
        score = parse_score_msg(item, ts_ns)
        if score is not None:
            scores.append(score)
    scores.sort(key=lambda s: s.ts_event)
    return scores


async def fetch_backfill(
    http_url: str,
    lead_idx: int = 1,
    start_date: str = "2026-01-01",
) -> list[SignalScore]:
    """Fetch historical scores from the signal server backfill endpoint.

    Args:
        http_url: Base URL of the signal server (e.g. "http://localhost:8000").
        lead_idx: Forecast lead index (1 = day-ahead).
        start_date: ISO date string for the earliest scores to fetch (YYYY-MM-DD).

    Returns:
        Chronologically sorted list of SignalScore events.
    """
    url = f"{http_url}/v1/trading/scores/backfill"
    params = {"lead_idx": lead_idx, "start_date": start_date}
    async with httpx.AsyncClient() as client:
        resp = await client.get(url, params=params, timeout=120.0)
        resp.raise_for_status()
        data = resp.json()
    return parse_backfill_response(data)
