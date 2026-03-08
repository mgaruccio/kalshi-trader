# Kalshi Trader

## Project Overview
Kalshi Trader is a Python-based trading project integrating the Nautilus Trader framework with the Kalshi prediction market. It implements a custom Nautilus adapter (`KalshiDataClient` and `KalshiExecutionClient`) that translates Kalshi's REST and WebSocket APIs into Nautilus's event-driven data and execution model.

The project is structured to:
1. Provide a robust foundation for building algorithmic strategies on Kalshi.
2. Support real-time data collection (e.g. streaming orderbook deltas to Parquet catalogs).
3. Demonstrate a sample strategy (`KalshiSpreadCaptureStrategy`) that attempts to capture bid/ask spreads.

### Main Technologies
- **Python:** >= 3.12
- **Nautilus Trader:** Event-driven trading framework (`nautilus-trader`).
- **Kalshi SDK:** Official REST client (`kalshi-python`).
- **WebSockets:** Used for real-time market data and order updates (`websockets`).
- **Package Management:** `uv`

## Architecture & Key Files
- `adapter.py`: The core integration layer. Contains `KalshiDataClient` for consuming WebSockets, `KalshiExecutionClient` for order routing, and `KalshiInstrumentProvider` for caching market definitions.
- `strategy.py`: Defines a sample Nautilus `Strategy` (`KalshiSpreadCaptureStrategy`).
- `main.py`: Entry point for running strategies using the Kalshi Demo environment. Includes setup for `TradingNode`.
- `collector.py`: A specialized standalone script utilizing a `TradingNode` with `StreamingConfig` to silently collect live Kalshi market data into a local `ParquetDataCatalog` directory without executing trades. Features an asynchronous background polling loop for dynamic market discovery (specifically tuned for high temperature markets).
- `docker-compose.yml` & `Dockerfile`: Support for containerized execution.

## Building and Running

### Installation
Dependencies are managed via `uv`.

```bash
uv sync
```

### Configuration
Create a `.env` file referencing your Kalshi API credentials. Note: Kalshi separates production (`api.elections.kalshi.com`) and demo (`demo-api.kalshi.co`) domains.

```env
KALSHI_API_KEY_ID="your_key_id"
KALSHI_PRIVATE_KEY_PATH="/path/to/private_key.pem"
KALSHI_ENVIRONMENT="demo"
KALSHI_REST_HOST="https://demo-api.kalshi.co/trade-api/v2"
KALSHI_WS_HOST="wss://demo-api.kalshi.co/trade-api/ws/v2"
```

### Execution
Run the demo trading strategy locally:
```bash
uv run main.py
```

Run the data collector (ensure `.env` points to the production URL to capture real data):
```bash
uv run collector.py
```

Run using Docker (ensure `~/.config/kalshi/` contains your keys as mounted in `docker-compose.yml`):
```bash
docker compose up --build
```

## Development Conventions
- **Asynchronous Execution:** The integration relies heavily on Python's `asyncio` loop running inside Nautilus components. Synchronous external HTTP calls (like Kalshi SDK REST requests) must be explicitly offloaded to thread executors (`asyncio.get_running_loop().run_in_executor`) to prevent blocking the trading engine.
- **Data Persistence:** Use Nautilus's native `StreamingConfig` for data persistence instead of writing custom disk-flushing logic.
- **Instrument Filtering:** Because Kalshi manages thousands of ephemeral markets, instrument discovery (like fetching event definitions) should be heavily filtered via `SeriesApi` and targeted requests to respect API rate limits (HTTP 429).
