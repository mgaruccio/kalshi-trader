# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

```bash
# Install dependencies
uv sync

# Run tests (use uv run python -m pytest, NOT uv run pytest — system Python lacks project deps)
uv run python -m pytest tests/mock_exchange/ -v          # mock exchange unit tests
uv run python -m pytest tests/test_live_confidence.py -v  # adapter confidence tests (run in isolation)
uv run python -m pytest tests/test_execution.py tests/sim/ -v  # adapter unit tests

# Run all tests (excluding confidence tests which need isolation due to event loop)
uv run python -m pytest --ignore=tests/test_live_confidence.py -v

# Run data collector
uv run collector.py
```

## Architecture

This repo contains the **Kalshi NautilusTrader adapter** and a **data collector** for KXHIGH (daily high temperature) prediction markets. The evaluator, trading node, and supporting modules are not yet on master.

### Collector (`collector.py`)
WebSocket data ingestion into ParquetDataCatalog for backtesting archives. Runs as a systemd service on the production droplet (161.35.114.105). Discovery strategy finds KXHIGH series, subscribes to quote ticks, and persists via StreamingFeatherWriter. Production-hardened: heartbeat timer (tick count/60s), event loop reference caching, background thread error surfacing, consecutive failure escalation, unopened market discovery.

### Critical modules
- **`kalshi/`**: Kalshi ↔ NautilusTrader adapter package. `KalshiDataClient` (WebSocket market data), `KalshiExecutionClient` (order routing), `KalshiInstrumentProvider` (market discovery). REST calls offloaded to `asyncio.to_thread()`. Config overrides via `base_url_http`/`base_url_ws`. Factory singleton `_SHARED_PROVIDER` must be reset to `None` between test runs.
- **`collector.py`**: Discovery strategy + TradingNode bootstrap. Uses `KalshiInstrumentProvider(load_all=False)` since the strategy discovers markets itself. Must call `cache.add_instrument(inst)` before subscribing to quote ticks — StreamingFeatherWriter silently drops ticks for instruments not in cache.

### Deployment
Production droplet at `161.35.114.105`. Collector runs via `systemctl {start,stop,restart} collector.service`. Logs at `/root/kalshi-trader/collector.log`. Data at `kalshi_data_catalog/` (3.6GB+). Deploy by pushing to master and pulling on the droplet (or scp for hotfixes).

## Key Conventions

- **Prices are in cents** (1-99 range for Kalshi contracts). Variables use `_cents` suffix.
- **Kalshi rate limits**: Basic tier allows 10 writes/sec. Batch submissions are capped at 9 orders per request.
- **Kalshi API status filters**: Only `"open"`, `"unopened"`, `"closed"`, `"settled"` are valid for `get_markets()`. `"active"` is a response field but NOT a valid query parameter.
- **NautilusTrader Cython gotchas**: Strategy/Actor attributes (`log`, `clock`, `cache`) are Cython read-only descriptors. Tests use a `_TestableWeatherStrategy` stand-in that binds real methods onto a plain Python object with mocked NT attributes.
- **Async discipline**: Synchronous Kalshi SDK calls must go through `run_in_executor()` to avoid blocking the NT event loop.
- **Environment**: Demo (`demo-api.kalshi.co`) vs Production (`api.elections.kalshi.com`) controlled by `.env` / `KalshiConfig`.
- **Currency**: Kalshi contracts use a synthetic `CONTRACT` currency registered at adapter import time.
- **StreamingFeatherWriter**: Requires instruments in NT cache to persist per-instrument data (QuoteTick, TradeTick, Bar). Use `cache.add_instrument()` before subscribing.

## Testing

- **Confidence tests** (`tests/test_live_confidence.py`): Boot a real TradingNode against a local mock Kalshi exchange. Must run in isolation (`uv run python -m pytest tests/test_live_confidence.py -v`) — event loop contamination with other async tests.
- **Mock exchange** (`tests/mock_exchange/`): REST server (asyncio.start_server) + WS server (websockets.serve) on separate ports. 4 deterministic scenarios: steady_fill, chase_up, market_order_immediate, no_fill_timeout.
- **Factory singleton**: `kalshi.factories._SHARED_PROVIDER = None` between tests that create new mock server instances.
