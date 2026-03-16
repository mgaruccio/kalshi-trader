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

# Run all tests (excluding live tests which need isolation / running services)
uv run python -m pytest --ignore=tests/test_live_confidence.py --ignore=tests/test_live_signals.py -v

# Run data collector
uv run collector.py
```

## Architecture

This repo contains the **Kalshi NautilusTrader adapter**, **data collector**, **backtest pipeline**, and **dry-run trader** for KXHIGH (daily high temperature) prediction markets.

### Collector (`collector.py`)
WebSocket data ingestion into ParquetDataCatalog for backtesting archives. Runs as a systemd service on the production droplet (161.35.114.105). Discovery strategy finds KXHIGH series, subscribes to quote ticks, and persists via StreamingFeatherWriter. Production-hardened: heartbeat timer (tick count/60s), event loop reference caching, background thread error surfacing, consecutive failure escalation, unopened market discovery.

### Critical modules
- **`kalshi/`**: Kalshi ↔ NautilusTrader adapter package. `KalshiDataClient` (WebSocket market data), `KalshiExecutionClient` (order routing), `KalshiInstrumentProvider` (market discovery). REST calls offloaded to `asyncio.to_thread()`. Config overrides via `base_url_http`/`base_url_ws`. Factory singleton `_SHARED_PROVIDER` must be reset to `None` between test runs.
- **`kalshi/strategy.py`**: `WeatherMakerStrategy` — forecast-filtered passive market maker. No dry-run branching — strategy code is identical in live and sandbox modes. Config via `WeatherMakerConfig`.
- **`kalshi/sandbox.py`**: `KalshiSandboxExecClientFactory` — wraps NT's `SandboxExecutionClient` with `BestPriceFillModel` for `--dry-run` mode. Real fills, positions, and portfolio tracking via simulated exchange.
- **`kalshi/backtest.py`** + **`kalshi/backtest_results.py`**: Backtest engine and result extraction (mark-to-market PnL, per-city breakdown).
- **`collector.py`**: Discovery strategy + TradingNode bootstrap. Uses `KalshiInstrumentProvider(load_all=False)` since the strategy discovers markets itself. Must call `cache.add_instrument(inst)` before subscribing to quote ticks — StreamingFeatherWriter silently drops ticks for instruments not in cache.
- **`scripts/run_trader.py`**: Live/demo trader bootstrap. `--dry-run` uses `SandboxExecutionClient` (simulated exchange with real market data). `--environment production --dry-run` is safe (no real orders). KXHIGH instruments loaded via per-city `series_tickers` list (~204 markets in ~1s).

### Deployment
Production droplet at `161.35.114.105`. Deploy by pushing to master and pulling on the droplet (or scp for hotfixes).
- **Collector**: `systemctl {start,stop,restart} collector.service`. Logs at `/root/kalshi-trader/collector.log`. Data at `kalshi_data_catalog/` (3.6GB+).
- **Trader**: `systemctl {start,stop,restart} trader.service`. Logs at `/root/kalshi-trader/trader.log`. Currently in dry-run mode ($20 simulated). Heartbeat at `/tmp/kalshi-trader-heartbeat`.
- **PYTHONPATH**: `trader.service` sets `PYTHONPATH=/root/kalshi-trader` — required because systemd scripts set `sys.path[0]` to the scripts/ directory.

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
