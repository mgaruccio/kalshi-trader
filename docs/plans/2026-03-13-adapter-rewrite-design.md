---
date: 2026-03-13
topic: kalshi-adapter-rewrite
---

# Kalshi NautilusTrader Adapter Rewrite

## What We're Building

A properly-architected Kalshi adapter following the canonical NautilusTrader pattern
(Polymarket-style: Python adapter using Rust networking primitives from `nautilus_pyo3`).
This replaces the current adapter which uses Python `websockets` + `ThreadPoolExecutor` —
the wrong networking foundation for NT.

The adapter is generic (any Kalshi market, not KXHIGH-specific) and must pass exhaustive
edge-case testing against the Kalshi demo API before being trusted for live trading.

All existing strategy/ML code is archived. The deliverable is a production-quality adapter
with a test harness that proves correctness.

## Why This Approach

### Approaches Considered

1. **Fix existing adapter** — surgically repair batching, rate limiting, SDK workarounds.
   Rejected because the networking foundation is wrong (Python `websockets` instead of
   `nautilus_pyo3.WebSocketClient`, `ThreadPoolExecutor` instead of `asyncio.to_thread`).
   Fixing individual bugs doesn't fix the architecture.

2. **Full rewrite following NT conventions** (chosen) — new adapter modeled after the
   Polymarket adapter, using `nautilus_pyo3.WebSocketClient` for WS and `asyncio.to_thread`
   for SDK REST calls. Selectively ports proven logic (instrument building, quote derivation).

3. **Rust-first adapter** — full Rust crate with PyO3 bindings (Architect AX pattern).
   Rejected as premature; requires building against NT's Rust workspace and the Kalshi API
   is simple enough that the Polymarket-style Python approach is correct.

### Why keep the `kalshi-python` SDK

The SDK's `KalshiAuth` handles RSA-PSS signing correctly. The deserialization bugs are
solved by using `_with_http_info()` + parsing `raw_data` directly (pattern already proven
in our codebase). Going fully raw (httpx/aiohttp) would require reimplementing RSA-PSS
signing for no benefit.

## Key Decisions

### Kalshi-Specific Mechanics (vs Polymarket)

These are the mechanical differences that drive adapter design choices:

1. **YES and NO are separate markets on Kalshi's end** — not views into one market.
   Positions are tracked per-side. We model them as separate NautilusTrader instruments
   (`TICKER-YES`, `TICKER-NO`), each long-only, with `OmsType.HEDGING`.

2. **Orderbook is bids-only** — Kalshi returns `yes_dollars_fp` and `no_dollars_fp` bid
   arrays. Asks must be computed synthetically: `YES ask = 1.0 - best NO bid`. This is a
   Kalshi-specific transformation in the data client.

3. **Single WebSocket endpoint** — one WS URL for both data and execution, with
   channel-based multiplexing. Polymarket uses separate data/user WS URLs. We'll use two
   `WebSocketClient` instances for isolation (one for market data, one for user/execution)
   connecting to the same endpoint but subscribing to different channels.

4. **No heartbeat requirement** — Kalshi has no PING/PONG protocol. Connection health
   relies on TCP keepalive and the `WebSocketClient`'s built-in timeout detection.

5. **No per-order signing** — orders are plain REST calls authenticated with request-level
   RSA-PSS headers. No EIP-712, no blockchain, no wallet management.

6. **Market-level subscriptions** — one `orderbook_delta` subscription per `market_ticker`
   delivers both YES and NO book data. Polymarket requires per-token subscriptions.

7. **Fees are always non-zero** — quadratic formula:
   `fee = ceil(coefficient * C * P * (1-P))`. Taker: 7%, maker: 1.75%. Must parse
   `fee_cost` from fill WebSocket messages and pass as `commission` to NT. Current adapter
   hardcodes 0 — this is a known bug.

8. **Batching does NOT help rate limits** — a batch of 5 orders counts as 5 writes against
   the per-second limit. Batching only reduces HTTP round-trips. Cancels are cheaper:
   each cancel = 0.2 writes. Max batch size: 20 per request.

9. **Rate limiting is the #1 engineering constraint** — Basic tier: 10 writes/sec. This is
   35x more restrictive than Polymarket. Hard 429 rejection (not soft throttling). The
   adapter must enforce write budgets internally, not just react to 429s.

10. **Settlement fills have no ticker** — Kalshi sends fills with empty ticker for settled
    contracts. These must be detected and skipped (not crashed on).

11. **`market_lifecycle_v2` channel** — must subscribe to detect market closures and clean
    up state proactively. Not present in current adapter.

12. **Price format migration** — cent-based integer fields (`yes_price`, `no_price`) are
    being deprecated. Use `yes_price_dollars`/`no_price_dollars` (strings) and `count_fp`
    (strings) exclusively.

### Architecture: SDK for Auth, Raw Parsing for Data

- **`kalshi-python` SDK** handles auth (`KalshiAuth`) and HTTP transport
- **All responses parsed via `_with_http_info()` + `json.loads(raw_data)`** to avoid SDK
  deserialization bugs
- **`msgspec.json.Decoder`** with typed schema classes for WebSocket messages
- **`nautilus_pyo3.WebSocketClient`** for WebSocket connections (reconnection, heartbeat)
- **`asyncio.to_thread()`** for SDK REST calls (replacing `ThreadPoolExecutor`)

### Rate Limiter Design

Token-bucket rate limiter that tracks write budget:
- Creates cost 1.0 per order
- Cancels cost 0.2 per cancel
- Amends cost 1.0 per amend
- Basic tier budget: 10.0 writes/sec, refilled continuously
- Pre-check before submission: if insufficient budget, queue and flush after refill
- Never rely on 429 retry as primary flow control

### File Structure

```
kalshi/
  __init__.py
  common/
    __init__.py
    constants.py          # KALSHI_VENUE, base URLs, enums
    credentials.py        # KalshiAuth wrapper (thin, delegates to SDK)
    types.py              # msgspec schema classes for REST API responses
    errors.py             # KalshiApiError, should_retry(), error mapping
  websocket/
    __init__.py
    client.py             # KalshiWebSocketClient wrapping nautilus_pyo3.WebSocketClient
    types.py              # msgspec schema classes for WS messages
  config.py               # KalshiDataClientConfig, KalshiExecClientConfig
  data.py                 # KalshiDataClient (LiveMarketDataClient)
  execution.py            # KalshiExecutionClient (LiveExecutionClient)
  factories.py            # KalshiLiveDataClientFactory, KalshiLiveExecClientFactory
  providers.py            # KalshiInstrumentProvider
```

### Instrument Model

Each Kalshi market ticker (e.g., `KXHIGHCHI-26MAR14-T55`) becomes two NT instruments:
- `InstrumentId(Symbol("KXHIGHCHI-26MAR14-T55-YES"), Venue("KALSHI"))`
- `InstrumentId(Symbol("KXHIGHCHI-26MAR14-T55-NO"), Venue("KALSHI"))`

Instrument type: `BinaryOption` (if available in NT) or `CurrencyPair` with:
- `price_precision=2` (cents as dollars: 0.01 - 0.99)
- `size_precision=0` (whole contracts only)
- `price_increment=Price(0.01, precision=2)`
- `size_increment=Quantity(1, precision=0)`
- `margin_init=1.0`, `margin_maint=1.0` (fully collateralized)
- `maker_fee` and `taker_fee` set from Kalshi fee schedule (not 0)

### Data Client

Subscribes to:
- `orderbook_delta` — per market_ticker, emits QuoteTicks for both YES and NO instruments
- `market_lifecycle_v2` — per market_ticker, detects closures/settlements

Quote derivation (preserved from current adapter):
- YES: `bid = max(yes_bids)`, `ask = 1.0 - max(no_bids)`
- NO:  `bid = max(no_bids)`, `ask = 1.0 - max(yes_bids)`

Sequence gap detection: track `seq` per `sid`. On gap, log warning and resubscribe to
force a fresh snapshot.

### Execution Client

Implements all required methods:
- `_submit_order()` — single order via SDK `create_order()` through `asyncio.to_thread()`,
  with rate limiter pre-check and retry manager
- `_cancel_order()` — single cancel via SDK `cancel_order()`
- `_cancel_all_orders()` — fetch resting orders, batch cancel
- `_modify_order()` — via SDK `amend_order()` (new — not in current adapter)
- `_batch_cancel_orders()` — via SDK `batch_cancel_orders()`
- `generate_order_status_reports()` — fetch resting orders via REST with cursor pagination
- `generate_fill_reports()` — fetch fills via REST with cursor pagination
- `generate_position_status_reports()` — fetch positions via REST (two-step: events → markets)
- `generate_mass_status()` — combines all three report types

WebSocket subscriptions: `user_orders` + `fill` channels.

ACK synchronization: `asyncio.Event` to coordinate HTTP responses (which provide
`VenueOrderId`) with WebSocket confirmations. Timeout after configurable seconds.

Fill processing: parse `fee_cost` from fill messages, compute `commission` accurately.

### Testing Strategy

#### Unit tests (no network)
- **msgspec schema parsing**: Every API response format and WS message type parsed through
  typed schemas. Test fixtures with real Kalshi JSON payloads.
- **Quote derivation**: Orderbook snapshot → QuoteTick emission for all edge cases (empty
  book, one-sided book, crossed book).
- **Rate limiter**: Budget tracking, queue-and-flush, write cost accounting.
- **Instrument building**: Ticker parsing, side extraction, price/size precision.

#### Integration tests (demo API)
- **Auth round-trip**: Connect, fetch balance, verify response.
- **Instrument loading**: Load markets by series_ticker, verify instrument fields.
- **Order lifecycle combinatorics**: Every combination of:
  - Side: yes, no
  - Action: buy, sell
  - Time-in-force: GTC, FOK, IOC
  - Special flags: post_only, reduce_only
  - Outcomes: accepted, rejected (insufficient funds), partially filled, fully filled,
    cancelled, expired
- **Batch operations**: Batch create (verify each order individually), batch cancel.
- **Position tracking**: Open position via fill, verify position report matches, close
  position, verify zero.
- **Fill reporting**: Submit and fill order, verify fill report has correct price, qty,
  fee, side.
- **WebSocket reconnection**: Force disconnect, verify resubscription and state recovery.
- **Settlement simulation**: If possible on demo, verify settlement fill handling.

#### Reconciliation tests
- Start with open orders + positions on demo
- Restart adapter
- Verify `generate_order_status_reports()`, `generate_fill_reports()`,
  `generate_position_status_reports()` all match Kalshi state exactly

#### Soak test
- Run adapter connected to demo for 24h+ with a trivial strategy
- Verify no state drift, no memory growth, WebSocket stays connected
- Log and verify all WS sequence numbers (no gaps)

## External Prerequisites

- **Kalshi demo API credentials**: Configured in `.env` (have them)
- **`kalshi-python` SDK**: Already in pyproject.toml (v2.1.4+)
- **`nautilus-trader` >=1.224.0**: Already in pyproject.toml (provides `nautilus_pyo3`)
- **`msgspec`**: Need to add to pyproject.toml
- **Redis**: Needed for NT message bus (already available)

## Open Questions

1. Does NT 1.224 ship `BinaryOption` as an instrument type, or do we stick with
   `CurrencyPair`? Need to check the Polymarket adapter's instrument type.
2. Should we use one or two WebSocket connections? One is simpler and Kalshi allows it;
   two gives better isolation between data and execution message handling.
3. What's the demo API's behavior for settlement — can we trigger it, or do we need to
   wait for a real market to settle?

## What Gets Archived

All current strategy/ML code moves to `archive/`:
- `weather_strategy.py`, `feature_actor.py`, `evaluator.py`, `exit_rules.py`
- `shared_features.py`, `backtest_runner.py`, `strategy.py`
- `main.py`, `cli.py`, `db.py`, `data_types.py`
- Current `adapter.py` → `archive/adapter_v1.py`
- All test files (rewritten from scratch)

## Next Steps

→ writing-plans skill for implementation plan with task breakdown
