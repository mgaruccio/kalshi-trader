---
date: 2026-03-14
topic: adapter-confidence-test
---

# Adapter Confidence Test via Mock Exchange

## What We're Building

A local mock Kalshi server (HTTP + WebSocket) and a test harness that boots a real NautilusTrader `TradingNode`, connects the unmodified adapter to the mock server, runs a simplified strategy that places market and limit orders on NO weather contracts, and asserts the full event loop works: quotes flow in, orders go out, fills come back, positions update.

## Why This Approach

The adapter has comprehensive unit tests and canned-message edge-case tests, but nothing exercises the full stack under a real NT event loop. A local mock server lets us test the actual adapter code — including HTTP serialization, WebSocket handling, and factory wiring — without hitting Kalshi's API or needing credentials.

Alternative considered: in-process mocks that replace the clients at the NT interface level. Rejected because it doesn't exercise the real network path, serialization, or auth plumbing.

## Components

### 1. Mock Kalshi Server (`tests/mock_exchange/server.py`)
- `aiohttp` HTTP server + WebSocket endpoint on localhost
- In-memory order book per ticker with deterministic price scripts
- Implements 5 REST endpoints:
  - `POST /trade-api/v2/portfolio/orders` (submit order)
  - `DELETE /trade-api/v2/portfolio/orders/{id}` (cancel order)
  - `GET /trade-api/v2/portfolio/orders` (reconciliation on connect)
  - `GET /trade-api/v2/markets` (instrument loading)
  - `POST /trade-api/v2/login` (auth — no-op, returns dummy token)
- Implements 3 WebSocket channels:
  - `orderbook_delta` (market data)
  - `fill` (fill notifications)
  - `user_orders` (order ACKs)
- Matching engine: fills resting limits when scripted price crosses them; fills market orders immediately at best price

### 2. Scenario Scripts (`tests/mock_exchange/scenarios.py`)
- Each scenario is a named sequence of `(delay_ms, bid_price, ask_price)` tuples
- Starting scenarios:
  - **`steady_fill`**: Bid at 35c, limit at 34c, bid walks down to 34c → fill
  - **`chase_up`**: Bid walks 30→35c, strategy amends limit each tick, bid reverses to 34c → fill at 34c
  - **`market_order_immediate`**: Market order fills instantly at current ask
  - **`no_fill_timeout`**: Bid walks away from limit, order never fills, strategy handles gracefully

### 3. Test Strategy (`tests/mock_exchange/test_strategy.py`)
- Minimal `Strategy` subclass
- Subscribes to quotes on one NO contract
- On first quote: submits market buy + limit buy at bid - 1c
- On subsequent quotes: amends limit to new bid - 1c
- Records all events (fills, accepts, rejects) for assertion

### 4. Test Runner (`tests/test_live_confidence.py`)
- Starts mock server in a background task
- Configures `TradingNode` with real factories pointed at `localhost:{port}`
- Runs each scenario as a separate test case
- Asserts: order accepted, fill received, position in cache, correct prices

## Key Decisions

- **`aiohttp` for mock server**: Already in the dependency tree via NautilusTrader, no new deps
- **Auth is a no-op**: Testing adapter mechanics, not RSA-PSS signing
- **Deterministic price scripts**: Makes assertions reliable and tests reproducible
- **Real factories, real adapter code**: Zero modifications to production code; only the URL in config changes

## Coverage Matrix

| Concern | Covered |
|---------|---------|
| Factory → client wiring | Yes — real TradingNode boot |
| REST serialization | Yes — real HTTP calls to mock |
| WebSocket subscription + parsing | Yes — real WS connection |
| Quote derivation from orderbook | Yes — mock publishes deltas |
| Order submission + ACK flow | Yes — REST submit + WS ack |
| Fill dedup | Yes — mock can replay fills |
| Order amendment | Yes — limit chases bid |
| Position tracking in cache | Yes — assert after fills |

## Out of Scope

- Rate limiting under load (stress test, not confidence test)
- RSA-PSS auth verification (mocked out)
- Real Kalshi API edge cases (malformed responses)
- Multi-instrument concurrency

## Open Questions

- Exact `aiohttp` WebSocket message framing to match what the adapter expects
- Whether `TradingNode` teardown needs special handling in test cleanup
- Port allocation strategy (random free port vs fixed)

## Next Steps

→ writing-plans skill for implementation details
