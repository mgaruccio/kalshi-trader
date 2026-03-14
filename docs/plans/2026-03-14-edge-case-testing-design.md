---
date: 2026-03-14
topic: edge-case-testing
---

# Edge-Case Testing for Kalshi Adapter

## What We're Building

A three-layer testing system that combines Combinatorial Testing (CT) for parameter interaction coverage with DST-inspired simulation testing for temporal/sequence bugs in the execution client. The system targets the Kalshi adapter's translation logic, state machines, and failure handling — specifically dedup cache behavior, WebSocket reconnection, and rate limiting.

## Why This Approach

We evaluated three approaches:

- **Approach A (chosen):** CT layer + Fake Exchange + Fault Injector + hand-rolled sequence generator. Each layer targets a different class of bug. The fake exchange enables emergent behavior — finding bugs we didn't think to test for.
- **Approach B (rejected):** CT + pre-recorded scenario replay. Only finds bugs you anticipated. No random exploration.
- **Approach C (rejected):** CT + Hypothesis-only with mocked responses. No realistic fill behavior, and debugging Hypothesis + async + NT infrastructure simultaneously is too much coupling.

CT is justified by NIST data: 3-way covering arrays catch ~90% of parameter interaction bugs in server-like systems, reducing ~3,840 exhaustive combos to ~120 test cases. The fake exchange borrows the highest-value DST concepts (deterministic counterparty, seeded PRNG, simulated clock, fault injection at I/O boundary) without requiring language-level support Python doesn't have.

## Architecture

```
┌─────────────────────────────────────────────────────────┐
│                    Test Harness                          │
│  (seed, clock, fault config, invariant assertions)      │
└────────┬──────────────────┬─────────────────┬───────────┘
         │                  │                 │
    Layer 1            Layer 2           Layer 3
  ┌──────┴──────┐  ┌───────┴────────┐  ┌─────┴──────────┐
  │     CT      │  │ Fake Exchange  │  │   Sequence     │
  │ (allpairspy │  │ + Fault        │  │   Generator    │
  │  3-way)     │  │   Injector     │  │  (seeded ops)  │
  └─────────────┘  └───────┬────────┘  └────────────────┘
                           │
              ┌────────────┼────────────┐
              │            │            │
        ┌─────┴─────┐ ┌───┴───┐ ┌──────┴──────┐
        │  Matching  │ │ Fault │ │  Simulated  │
        │  Engine    │ │Inject │ │  Clock      │
        │  (simple)  │ │(middleware)│ │(time-machine)│
        └───────────┘ └───────┘ └─────────────┘
```

## Layer 1: Combinatorial Testing

**Tool:** `allpairspy` with `pytest.mark.parametrize`

**Coverage scope:** All three adapter clients (execution, data, provider)

**Parameter spaces:**

Execution client (order translation):
- side: BUY, SELL
- contract: YES, NO
- price_cents: 1, 25, 50, 75, 99
- tif: GTC, FOK, IOC
- quantity: 1, 5, 9, 100
- market_state: open, closed, halted, settled
- error: none, timeout, rate_limit, insufficient_funds, invalid_ticker

Data client (quote derivation):
- yes_book_state: empty, single_level, multi_level
- no_book_state: empty, single_level, multi_level
- price_levels: boundary (0.01, 0.99), mid (0.50), typical (0.32, 0.68)
- delta_type: add, update, remove
- sequence: valid, gap, first_message

Provider (instrument loading):
- series: KXHIGH, other
- status_filter: active, open, unopened, settled
- pagination: single_page, multi_page, empty
- ticker_format: valid, malformed_date, missing_threshold

**Strength:** 3-way (n=3) — catches ~90% of parameter interaction bugs per NIST data for server-like systems. Generates ~100-150 test cases per parameter space.

**Constraints:** Filter function rejects impossible combinations (e.g., cannot place orders in settled markets without error).

**Assertions:** Each combination must either produce valid translated output OR raise an expected error. Universal invariants: prices in 1-99 cent range, quantities positive, action/side consistency.

## Layer 2: Fake Exchange + Fault Injector

### FakeKalshiExchange

A deterministic simulated Kalshi exchange that implements the same interface the adapter's HTTP/WS clients call.

**Matching behavior (simple mode):**
- Maintains a per-market order book (YES and NO sides)
- Buy order at price >= best ask → immediate fill
- GTC orders rest in book at submitted price
- FOK orders fill completely or reject
- IOC orders fill what's available, cancel remainder
- No other participants — just adapter's orders against a seeded initial book state

**State maintained:**
- Order book per market (initialized from seed)
- Order registry (id → status, price, qty, filled_qty)
- Fill history (trade_id sequence)
- Position tracker (net contracts per market per side)
- Balance tracker (available funds)

**Interface:**
```python
class FakeKalshiExchange:
    def __init__(self, seed: int, initial_books: dict[str, BookState])

    # REST-like interface (returns dicts matching Kalshi API response shapes)
    def create_order(self, params: dict) -> dict
    def cancel_order(self, order_id: str) -> dict
    def batch_create_orders(self, orders: list[dict]) -> dict
    def get_order(self, order_id: str) -> dict
    def get_fills(self, params: dict) -> dict
    def get_positions(self, params: dict) -> dict

    # WS-like interface (returns pending messages to deliver)
    def drain_ws_messages(self) -> list[dict]

    # Test inspection
    def get_order_state(self, order_id: str) -> OrderState
    def get_book(self, ticker: str) -> BookState
```

### FaultInjector

Middleware that wraps `FakeKalshiExchange` and injects faults between the exchange and the adapter.

**Fault types:**
- **Latency:** Delay REST responses or WS messages by seeded duration
- **Drops:** Drop WS messages (fills, acks) with configurable probability
- **Rate limiting:** Return 429 after N requests in a window
- **Disconnection:** Sever WS connection at arbitrary points, queue missed messages
- **Sequence gaps:** Skip WS sequence numbers to trigger adapter reconciliation
- **Reordering:** Deliver WS messages out of sequence (fill before ack)
- **Duplication:** Deliver the same WS message multiple times (dedup testing)
- **Corruption:** Malformed JSON, missing fields, unexpected status values

**Configuration:**
```python
@dataclass
class FaultProfile:
    seed: int
    ws_drop_rate: float = 0.0          # probability of dropping a WS message
    ws_reorder_window: int = 0         # max messages to buffer and shuffle
    ws_duplicate_rate: float = 0.0     # probability of duplicating a WS message
    rest_latency_range: tuple[float, float] = (0.0, 0.0)
    rate_limit_after: int | None = None  # return 429 after N requests
    disconnect_after_n_msgs: int | None = None
    sequence_gap_rate: float = 0.0     # probability of skipping a seq number
```

All randomness flows through a single `random.Random(seed)` instance.

### NT Infrastructure Wiring

Following the Polymarket adapter test pattern:
- Real: `MessageBus`, `Cache`, `ExecutionEngine`, `RiskEngine`, `Portfolio`
- From test_kit: `TestComponentStubs`, `TestEventStubs`, `TestIdStubs`, `eventually()`
- Mocked: HTTP client (replaced by FaultInjector → FakeExchange), WS client (replaced by simulated message stream)
- Real: `KalshiExecutionClient` (the system under test)

### Simulated Clock

`time-machine` library for deterministic time control. Critical for:
- Token bucket rate limiter behavior
- ACK timeout and REST fallback triggering
- Reconnection backoff timing

## Layer 3: Operation Sequence Generator

A seeded generator that produces sequences of operations to run against the adapter + fake exchange.

**Operations:**
- `submit_order(side, contract, price, qty, tif)`
- `cancel_order(order_id)`
- `trigger_fill(order_id, qty)` — tells fake exchange to fill
- `trigger_partial_fill(order_id, qty)`
- `disconnect_ws()`
- `reconnect_ws()`
- `advance_time(seconds)`
- `settle_market(ticker)`
- `inject_rate_limit()`

**Weighted toward priority scenarios:**
- Dedup: high weight on duplicate fills, settlement fills, fills after cache eviction
- Reconnection: high weight on disconnect/reconnect around fill delivery
- Rate limiting: high weight on burst submissions near token bucket capacity

**Invariants checked after each sequence:**
- Positions reconcile (adapter state matches fake exchange state)
- No duplicate fills emitted to NT (check MessageBus/Cache)
- No orphaned orders (every submitted order has a terminal state)
- Rate limiter never exceeded (no more than 10 writes/sec reached exchange)
- After reconnection, adapter state converges with exchange state

**Sequence length:** Configurable, default 20-50 operations per sequence.
**Sequences per test run:** Configurable, default 100 seeds.

**Future migration:** Once stable, encode operations as Hypothesis `RuleBasedStateMachine` rules for automatic shrinking of failing sequences.

## Key Decisions

- **3-way CT, not pairwise:** Server-like systems need 3-way for ~90% interaction coverage (pairwise only gets ~70% per NIST data). The cost is ~120 vs ~30 test cases — still fast.
- **Fake exchange at Kalshi API level, not NT level:** NT's `SimulatedExchange` operates below the adapter. We need to test the adapter's translation logic, so the fake must speak Kalshi's REST/WS protocol.
- **Fault injector as middleware, not integrated:** Clean separation means we can test the fake exchange without faults, compose fault profiles declaratively, and evolve each independently.
- **Hand-rolled sequences first, Hypothesis later:** Avoid debugging Hypothesis + async + NT + fake exchange simultaneously. Build the foundation, learn what matters, then add shrinking.
- **Execution client only for simulation:** Data client and provider are well-served by CT. The execution client is where temporal/state-machine bugs live.
- **NT test_kit adoption:** Use `TestComponentStubs`, `TestEventStubs`, `TestExecStubs`, `eventually()` instead of current SimpleNamespace hacks. Aligns with Polymarket adapter test pattern.

## Component Independence (Parallelization)

These components have clean interfaces and can be built concurrently:

| Component | Dependencies | Interface |
|-----------|-------------|-----------|
| CT test suite | `allpairspy`, existing adapter code | pytest parametrize |
| FakeKalshiExchange | None (standalone) | dict-based REST/WS interface |
| FaultInjector | FakeKalshiExchange interface (can mock) | wraps exchange, same interface |
| Sequence Generator | FakeExchange + FaultInjector interfaces (can mock) | yields operation lists |
| NT test harness | NT test_kit, adapter code | wires real NT infra + fake exchange |
| Invariant checkers | NT Cache/MessageBus types | assertion functions |

## Resolved Questions

- **Multiple concurrent markets:** Yes — cross-market testing catches dedup/position bugs that single-market misses. Fake exchange maintains independent books per ticker.
- **Initial book states:** Empty, thin (1 level each side), deep (5+ levels), one-sided (only YES or only NO bids).
- **CI cadence:** CT tests run in CI on every commit. Simulation tests (Layer 2+3) are manual/local only for now — not in CI.

## Next Steps

→ writing-plans skill for implementation plan with task breakdown suitable for parallel agent execution
