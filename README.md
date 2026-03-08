# Kalshi Trader

Nautilus Trader adapter and sample strategy for the Kalshi prediction market.

## Setup

Requires Python >= 3.12 and `uv`.

```bash
uv sync
```

## Known Limitations & Production Readiness

This adapter is a structural foundation. The following considerations remain for production deployment:
- **WebSocket Reconnections:** Basic reconnection loops exist, but sophisticated backoff and state recovery mechanisms (e.g. tracking `cursor` in WebSocket streams) are necessary for 24/7 reliability.
- **Fees & Commissions:** Kalshi fees are complex (tiered by fill price) and currently hardcoded to $0.00 in the adapter for simplicity. Production trading requires accurate fee calculation for P&L tracking.
