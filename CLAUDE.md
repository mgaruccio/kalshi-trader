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

# Run live trading (requires .env with Kalshi credentials + Redis running)
uv run main.py                          # live trading
uv run main.py --dry-run                # paper trade (no orders)
uv run main.py --capital 2000 --max-per-ticker 10

# Run standalone evaluator (requires KALSHI_WEATHER_ROOT pointing to kalshi-weather repo)
uv run evaluator.py --db data/trading.db

# Run data collector
uv run collector.py

# Query trading state via CLI
uv run cli.py status
uv run cli.py evals
uv run cli.py positions
uv run cli.py fills
```

## Architecture

This is a **three-process trading system** for Kalshi KXHIGH (daily high temperature) prediction markets, built on NautilusTrader:

### Process 1: Evaluator (`evaluator.py`)
Standalone ML scoring loop. Loads ensemble models (EMOS, NGBoost, DRN) from the **external** `kalshi-weather` repo (path via `KALSHI_WEATHER_ROOT` env var). Fetches open markets from Kalshi REST API, scores each with weather forecast data, writes evaluations to SQLite, and publishes `ModelSignal` to Redis streams.

### Process 2: Trading Node (`main.py`)
NautilusTrader live engine. Consumes `ModelSignal` and `DangerAlert` from Redis `external_streams`. Runs `WeatherStrategy` (2-phase market-making with GTC ladder orders) and `FeatureActor` (climate data polling). Executes orders via `KalshiExecutionClient`.

### Process 3: Collector (`collector.py`)
Silent WebSocket data ingestion into ParquetDataCatalog for backtesting archives.

### Key data flow
```
Evaluator → Redis stream → Trading Node → Kalshi adapter → REST API
                                ↓
                        SQLite (shared state)
```

### Critical modules
- **`kalshi/`**: Kalshi ↔ NautilusTrader adapter package. `KalshiDataClient` (WebSocket market data), `KalshiExecutionClient` (order routing), `KalshiInstrumentProvider` (market discovery). REST calls offloaded to `asyncio.to_thread()`. Config overrides via `base_url_http`/`base_url_ws`. Factory singleton `_SHARED_PROVIDER` must be reset to `None` between test runs.
- **`weather_strategy.py`**: 2-phase strategy — Phase 1 places spread orders in an opening window for next-day contracts; Phase 2 places laddered GTC buy orders below bid with immediate sell-on-fill at target price.
- **`data_types.py`**: Custom NT `Data` subclasses (`ModelSignal`, `DangerAlert`, `ClimateEvent`) that flow through the message bus. NT 1.224+ pattern: private `_ts_event`/`_ts_init` with `@property` accessors, no `super().__init__()`.
- **`db.py`**: SQLite with WAL mode. Shared by evaluator and trading node. All writes via named functions (`write_evaluations`, `upsert_position`, `write_fill`, etc.).
- **`shared_features.py`**: Single source of truth for derived feature computation — imported by both evaluator and feature_actor.
- **`exit_rules.py`**: Climate-specific exit conditions that emit `DangerAlert`.

### External dependency: `kalshi_weather_ml`
The ML models, market parsing (`parse_ticker`, `SERIES_CONFIG`), and forecast fetching live in a **separate repo** at `$KALSHI_WEATHER_ROOT/src`. This is added to `sys.path` at runtime in both `main.py` and `evaluator.py`. It is not a pip package.

## Key Conventions

- **Prices are in cents** (1-99 range for Kalshi contracts). Variables use `_cents` suffix.
- **Kalshi rate limits**: Basic tier allows 10 writes/sec. Batch submissions are capped at 9 orders per request.
- **NautilusTrader Cython gotchas**: Strategy/Actor attributes (`log`, `clock`, `cache`) are Cython read-only descriptors. Tests use a `_TestableWeatherStrategy` stand-in that binds real methods onto a plain Python object with mocked NT attributes.
- **Async discipline**: Synchronous Kalshi SDK calls must go through `run_in_executor()` to avoid blocking the NT event loop.
- **Environment**: Demo (`demo-api.kalshi.co`) vs Production (`api.elections.kalshi.com`) controlled by `.env` / `KalshiConfig`.
- **SQLite WAL mode** enables concurrent reads from CLI/dashboard while evaluator and trading node write.
- **Currency**: Kalshi contracts use a synthetic `CONTRACT` currency registered at adapter import time.

## Testing

- **Confidence tests** (`tests/test_live_confidence.py`): Boot a real TradingNode against a local mock Kalshi exchange. Must run in isolation (`uv run python -m pytest tests/test_live_confidence.py -v`) — event loop contamination with other async tests.
- **Mock exchange** (`tests/mock_exchange/`): REST server (asyncio.start_server) + WS server (websockets.serve) on separate ports. 4 deterministic scenarios: steady_fill, chase_up, market_order_immediate, no_fill_timeout.
- **Factory singleton**: `kalshi.factories._SHARED_PROVIDER = None` between tests that create new mock server instances.
