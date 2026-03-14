"""Kalshi adapter constants."""
from nautilus_trader.model.identifiers import Venue

KALSHI_VENUE = Venue("KALSHI")

# REST API base URLs (without trailing slash)
PROD_REST_URL = "https://api.elections.kalshi.com/trade-api/v2"
DEMO_REST_URL = "https://demo-api.kalshi.co/trade-api/v2"

# WebSocket base URLs
PROD_WS_URL = "wss://api.elections.kalshi.com/trade-api/ws/v2"
DEMO_WS_URL = "wss://demo-api.kalshi.co/trade-api/ws/v2"

# Rate limit costs per write operation (Basic tier: 10 writes/sec)
WRITE_COST_CREATE = 1.0
WRITE_COST_CANCEL = 0.2
WRITE_COST_AMEND = 1.0

# Max batch sizes (rate-limit-safe: 9 orders x 1 write each = 9 writes/sec < 10 limit)
MAX_BATCH_CREATE = 9
MAX_BATCH_CANCEL = 20
