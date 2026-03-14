# Kalshi Adapter Rewrite Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use forge:executing-plans to implement this plan task-by-task.

**Goal:** Replace the current broken Kalshi adapter with a properly-architected one following NautilusTrader's canonical adapter pattern (Polymarket-style).

**Architecture:** Two-layer design. The `kalshi-python` SDK handles RSA-PSS auth and HTTP transport (called via `asyncio.to_thread`). Responses are parsed from raw JSON (avoiding SDK deserialization bugs). WebSocket connections use `nautilus_pyo3.WebSocketClient` (Rust networking). Message parsing uses `msgspec.Raw` two-pass decode for Kalshi's nested envelope format.

**Tech Stack:** nautilus-trader 1.224.0, kalshi-python 2.1.4, msgspec 0.20.0 (NT dependency), Python 3.12

**Required Skills:**
- `forge:writing-tests`: Invoke before every test-writing step — covers assertion quality, edge case selection
- `forge:verification-before-completion`: Invoke before any milestone — verify test output before claiming success
- `forge:nautilus-testing`: Invoke before Tasks 12, 16, 20 — covers NT Cython mocking, `--noconftest`, test stubs (TestComponentStubs, TestIdStubs)

## Enhancement Summary (from 10-agent parallel review)

**Deepened on:** 2026-03-13
**Agents:** architecture-strategist, security-sentinel, performance-oracle, error-propagation-reviewer, python-reviewer, pattern-recognition-specialist, + 4 skill/research agents

### Critical Fixes (will crash or lose money if not applied)
1. **msgspec union type replaced with `msgspec.Raw` two-pass decode** — union of multiple Structs + dict crashes on import. Use `WsEnvelope(msg: msgspec.Raw)` for zero-copy first pass, then per-type decoders.
2. **Stale WS auth headers on reconnect** — RSA-PSS signatures include a timestamp. `WebSocketConfig` bakes headers at connect time; auto-reconnect reuses stale headers → silent auth failure. Must tear down and rebuild the `WebSocketClient` with fresh headers.
3. **`RetryManagerPool.run()` returns `None` on exhaustion** — callers MUST check for `None` and call `generate_order_rejected()`. Without this, failed orders silently vanish from NT's state machine.
4. **Settlement fills leave phantom positions** — "skip" is not safe. Must trigger `generate_position_status_reports()` to reconcile positions. Skipping leaves NT tracking a position that no longer exists.
5. **`MAX_BATCH_CREATE` must be 9, not 20** — API allows 20 per batch, but each order = 1 write against 10 writes/sec Basic tier limit.
6. **Batch retry creates duplicate orders** — 429 can arrive after partial processing. Must check order status by `client_order_id` before resubmitting.

### High-Priority Additions (will produce incorrect behavior)
7. **Sequence gap recovery** — `_check_sequence` returning False must clear local book and resubscribe. Stale book = garbage quotes = wrong trading signals.
8. **`time_in_force` mapping** — must use full strings: `"fill_or_kill"` not `"fok"`, `"good_till_canceled"` not `"gtc"`. Kalshi rejects shorthand.
9. **Token-bucket rate limiter** — replace batch-and-sleep with `TokenBucket(rate=10.0, capacity=10)`. Current approach is fragile under bursty submission.
10. **`_stop()` method** on execution client — `self._retry_manager_pool.shutdown()`. NT calls this during shutdown lifecycle.
11. **`asyncio.ensure_future` → tracked tasks** in reconnect handler — add `done_callback` for error logging. Fire-and-forget silently loses resubscription failures.
12. **All pytest commands: add `--noconftest`** — NT's global conftest triggers Cython import chains.
13. **Execution client tests: use NT test stubs** — `TestComponentStubs.cache()`, `TestIdStubs.trader_id()`, register exec engine + strategy + instruments.
14. **Call `initialize()` not `load_all_async()` directly** — NT's `InstrumentProvider` base handles idempotent initialization.
15. **`BinaryOption.description`** — use `None` not `""`. `Condition.valid_string` rejects empty strings.
16. **Capital locking** — Kalshi locks capital at order placement. `AccountBalance` must use `balance` field (already net of resting order collateral). Add `_update_account_state()`.
17. **Observation date from ticker, not `close_time`** — `close_time` is the day AFTER observation. Parse from ticker, store in `info["observation_date"]`.
18. **`lru_cache` must not cache credentials as dict keys** — use module-level singleton variable instead.
19. **REST call timeouts** — set `api_config.request_timeout = 10` to prevent thread pool exhaustion.
20. **ACK timeout with REST fallback** — on `asyncio.wait_for` timeout, query REST for order status.
21. **Instrument loading failures are fatal** — do not catch; let propagate from `_connect()`.
22. **Bounded deduplication caches** — `OrderedDict`-based, 10K cap for `seen_trade_ids`.
23. **Duplicate `no_price_dollars` field** removed from `FillMsg` schema.
24. **Config test assertions** — use `config.ws_url` (property) not `config.base_url_ws` (field, defaults to `None`).
25. **`asyncio.Lock`** on WS client for thread-safe subscription management.
26. **`name` parameter** — factories pass it but client constructors must accept it: `ClientId(name or "KALSHI")`.

## Context for Executor

### Open Questions Resolved

1. **BinaryOption exists in NT 1.224** — use it instead of CurrencyPair. Import: `from nautilus_trader.model.instruments import BinaryOption`. Use `price_precision=2` with `Price.from_str("0.01")` for dollar amounts (confirmed correct for `_dollars` field migration).
2. **Two WebSocket connections** — one for market data, one for user data. **CRITICAL: Must tear down and rebuild with fresh RSA-PSS auth headers on reconnect** (timestamps expire).
3. **Settlement fills must trigger position reconciliation** — not just be skipped. Call `generate_position_status_reports()` to sync state.
4. **msgspec is already a dependency** of nautilus-trader. Do NOT add it to pyproject.toml.
5. **msgspec union types do NOT work** for Kalshi's nested envelope. Use `msgspec.Raw` two-pass decode (see Task 5).
6. **`@property` works on frozen msgspec Structs** — verified against NT's `NautilusConfig` base class.

### Key Imports (verified from installed NT 1.224.0)

```python
# Networking (Rust pyo3 bindings)
from nautilus_trader.core.nautilus_pyo3 import HttpClient, HttpMethod, HttpResponse
from nautilus_trader.core.nautilus_pyo3 import WebSocketClient, WebSocketConfig
from nautilus_trader.core.nautilus_pyo3 import Quota

# Retry management
from nautilus_trader.live.retry import RetryManagerPool

# Instrument
from nautilus_trader.model.instruments import BinaryOption

# Base classes
from nautilus_trader.live.data_client import LiveMarketDataClient
from nautilus_trader.live.execution_client import LiveExecutionClient
from nautilus_trader.live.factories import LiveDataClientFactory, LiveExecClientFactory

# Data types
from nautilus_trader.model.data import QuoteTick
from nautilus_trader.model.objects import Price, Quantity, Currency, Money, AccountBalance
from nautilus_trader.model.identifiers import (
    Venue, InstrumentId, Symbol, ClientId, AccountId,
    ClientOrderId, VenueOrderId,
)
from nautilus_trader.model.enums import (
    AccountType, AssetClass, OmsType, OrderSide, OrderType,
    TimeInForce, OrderStatus, PositionSide,
)
from nautilus_trader.config import LiveDataClientConfig, LiveExecClientConfig

# Kalshi SDK
from kalshi_python.api_client import KalshiAuth
from kalshi_python.api.portfolio_api import PortfolioApi
from kalshi_python.api.markets_api import MarketsApi
from kalshi_python.models.create_order_request import CreateOrderRequest
from kalshi_python.models.batch_create_orders_request import BatchCreateOrdersRequest
from kalshi_python.exceptions import ApiException
import kalshi_python
```

### Key API Signatures

**WebSocketClient.connect** (classmethod, async):
```python
await WebSocketClient.connect(
    loop_=asyncio.get_running_loop(),
    config=WebSocketConfig(
        url="wss://...",
        headers=[("KALSHI-ACCESS-KEY", key), ...],  # list of tuples
        heartbeat=None,                               # Kalshi has no heartbeat
        reconnect_timeout_ms=10_000,
        reconnect_delay_initial_ms=2_000,
        reconnect_delay_max_ms=30_000,
    ),
    handler=self._handle_ws_message,    # handler(raw: bytes) -> None
    post_reconnection=self._on_reconnect,
)
```

**WebSocketClient.send_text**: `await client.send_text(msgspec.json.encode(msg))`
Note: `send_text` takes `bytes` (not str). `msgspec.json.encode()` returns bytes.

**BinaryOption constructor**:
```python
BinaryOption(
    instrument_id=InstrumentId(Symbol("TICKER-YES"), Venue("KALSHI")),
    raw_symbol=Symbol("TICKER-YES"),
    asset_class=AssetClass.ALTERNATIVE,
    currency=Currency.from_str("USD"),
    price_precision=2,
    size_precision=0,
    price_increment=Price.from_str("0.01"),
    size_increment=Quantity.from_int(1),
    activation_ns=0,
    expiration_ns=expiration_ns,
    ts_event=ts_init,
    ts_init=ts_init,
    maker_fee=Decimal("0.0175"),  # 1.75%
    taker_fee=Decimal("0.07"),    # 7%
    outcome="Yes",  # or "No"
    description="Will X happen?",
    info={"kalshi_ticker": "TICKER", "side": "yes"},
)
```

**RetryManagerPool usage**:
```python
retry_mgr = await self._retry_pool.acquire()
try:
    result = await retry_mgr.run(
        "operation_name", [identifiers],
        asyncio.to_thread, self.k_portfolio.some_method_with_http_info, *args,
    )
finally:
    await self._retry_pool.release(retry_mgr)
```

### Kalshi API Quick Reference

**REST base URLs**: Production `https://api.elections.kalshi.com/trade-api/v2`, Demo `https://demo-api.kalshi.co/trade-api/v2`
**WS base URLs**: Production `wss://api.elections.kalshi.com/trade-api/ws/v2`, Demo `wss://demo-api.kalshi.co/trade-api/ws/v2`

**Rate limits (Basic)**: 10 writes/sec, 20 reads/sec. Batch of N orders = N writes. Batch of N cancels = 0.2*N writes.

**WS channels**: `orderbook_delta` (snapshot + deltas), `user_orders` (order status), `fill` (fill notifications), `market_lifecycle_v2` (market state changes)

**WS subscribe format**: `{"id": N, "cmd": "subscribe", "params": {"channels": ["orderbook_delta"], "market_tickers": ["TICKER"]}}`

**Orderbook snapshot fields**: `yes_dollars_fp: [["0.32", "5.00"], ...]`, `no_dollars_fp: [["0.68", "3.00"], ...]` (price_dollars_str, qty_fp_str)

**Fill WS fields**: `trade_id`, `order_id`, `client_order_id`, `market_ticker`, `side`, `action`, `yes_price_dollars`, `no_price_dollars`, `count_fp`, `fee_cost`, `is_taker`, `ts` (unix seconds)

**User order WS fields**: `order_id`, `client_order_id`, `ticker`, `status` ("resting"/"canceled"/"executed"), `side`, `action`, `remaining_count_fp`, `fill_count_fp`

**Positions REST**: `GET /portfolio/positions` returns `market_positions[].position_fp` (positive=YES, negative=NO) and `event_positions[]`

**SDK bug**: `BatchCreateOrdersResponse` uses `responses` key in SDK model, but API returns `orders` key. Always use `_with_http_info()` + `json.loads(raw_data)`.

### Existing Files to Reference

- `adapter.py:216-270` — current quote derivation logic (port to new data client)
- `adapter.py:90-99` — KalshiConfig using pydantic-settings (replace with NT config pattern)
- `adapter.py:989-1015` — instrument building logic (rewrite for BinaryOption)
- `adapter.py:102-112` — `_get_ticker_and_side()` helper (reuse)
- `.env.example` — credential template
- Polymarket data client: `.venv/.../nautilus_trader/adapters/polymarket/data.py`
- Polymarket execution client: `.venv/.../nautilus_trader/adapters/polymarket/execution.py`
- Polymarket websocket client: `.venv/.../nautilus_trader/adapters/polymarket/websocket/client.py`
- Polymarket providers: `.venv/.../nautilus_trader/adapters/polymarket/providers.py`
- Polymarket config: `.venv/.../nautilus_trader/adapters/polymarket/config.py`
- Polymarket factories: `.venv/.../nautilus_trader/adapters/polymarket/factories.py`

---

## Task 1: Archive existing code and set up package structure

**Files:**
- Move to `archive/`: `adapter.py`, `weather_strategy.py`, `feature_actor.py`, `evaluator.py`, `exit_rules.py`, `shared_features.py`, `backtest_runner.py`, `strategy.py`, `main.py`, `cli.py`, `db.py`, `data_types.py`, `api.py`, all `test_*.py` files
- Create directories: `kalshi/`, `kalshi/common/`, `kalshi/websocket/`, `tests/`
- Create `__init__.py` files for each package

**Step 1: Create archive directory and move files**

```bash
mkdir -p archive
git mv adapter.py archive/adapter_v1.py
git mv weather_strategy.py feature_actor.py evaluator.py exit_rules.py archive/
git mv shared_features.py backtest_runner.py strategy.py archive/
git mv main.py cli.py db.py data_types.py api.py archive/
git mv test_weather_strategy.py test_adapter.py test_evaluator.py archive/
git mv test_db.py test_data_types.py test_feature_actor.py archive/
git mv test_exit_rules.py test_integration.py test_backtest_runner.py archive/
git mv test_api.py test_strategy.py test_redis_signal_flow.py archive/
git mv test_score_parity.py test_shared_features.py test_live_pollers.py archive/
git mv test_collector_unit.py archive/
```

Note: Files only in the working tree (untracked) should use `mv` not `git mv`. Check `git status` first. `adapter_archived.py` already exists at the top level — move it too.

**Step 2: Create package structure**

```bash
mkdir -p kalshi/common kalshi/websocket tests
touch kalshi/__init__.py kalshi/common/__init__.py kalshi/websocket/__init__.py
touch tests/__init__.py
```

**Step 3: Commit**

```bash
git add -A
git commit -m "chore: archive strategy/ML code, scaffold kalshi adapter package"
```

---

## Task 2: Implement constants and errors modules

**Files:**
- Create: `kalshi/common/constants.py`
- Create: `kalshi/common/errors.py`

**Step 1: Write tests for constants**

Create `tests/test_constants.py`:
```python
"""Tests for kalshi.common.constants."""
from nautilus_trader.model.identifiers import Venue


def test_kalshi_venue_is_venue():
    from kalshi.common.constants import KALSHI_VENUE
    assert isinstance(KALSHI_VENUE, Venue)
    assert KALSHI_VENUE.value == "KALSHI"


def test_base_urls_are_strings():
    from kalshi.common.constants import (
        DEMO_REST_URL, DEMO_WS_URL, PROD_REST_URL, PROD_WS_URL,
    )
    assert "demo-api.kalshi.co" in DEMO_REST_URL
    assert "demo-api.kalshi.co" in DEMO_WS_URL
    assert "api.elections.kalshi.com" in PROD_REST_URL
    assert "api.elections.kalshi.com" in PROD_WS_URL
```

**Step 2: Run tests to verify they fail**

```bash
pytest tests/test_constants.py -v
```

Expected: `ModuleNotFoundError: No module named 'kalshi.common.constants'` (file doesn't exist yet)

**Step 3: Implement constants**

Write `kalshi/common/constants.py`:
```python
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

# Max batch sizes (API limit)
MAX_BATCH_CREATE = 20
MAX_BATCH_CANCEL = 20
```

**Step 4: Write tests for errors**

Add to `tests/test_errors.py`:
```python
"""Tests for kalshi.common.errors."""
from kalshi_python.exceptions import ApiException
from kalshi.common.errors import KalshiApiError, should_retry


def test_should_retry_on_429():
    exc = ApiException(status=429, reason="Too Many Requests")
    assert should_retry(exc) is True


def test_should_retry_on_500():
    exc = ApiException(status=500, reason="Internal Server Error")
    assert should_retry(exc) is True


def test_should_not_retry_on_400():
    exc = ApiException(status=400, reason="Bad Request")
    assert should_retry(exc) is False


def test_should_not_retry_on_non_api_exception():
    assert should_retry(ValueError("bad")) is False


def test_kalshi_api_error():
    err = KalshiApiError(status=429, message="rate limited", raw="...")
    assert err.status == 429
    assert "rate limited" in str(err)
```

**Step 5: Implement errors**

Write `kalshi/common/errors.py`:
```python
"""Kalshi API error types and retry logic."""
from kalshi_python.exceptions import ApiException


class KalshiApiError(Exception):
    """Kalshi API error with status code and raw response."""

    def __init__(self, status: int, message: str, raw: str = ""):
        self.status = status
        self.message = message
        self.raw = raw
        super().__init__(f"Kalshi API {status}: {message}")


def should_retry(exc: BaseException) -> bool:
    """Return True if the exception is retryable (429 or 5xx)."""
    if isinstance(exc, ApiException):
        return exc.status == 429 or (500 <= exc.status < 600)
    if isinstance(exc, KalshiApiError):
        return exc.status == 429 or (500 <= exc.status < 600)
    return False
```

**Step 6: Run all tests**

```bash
pytest tests/test_constants.py tests/test_errors.py -v
```

Expected: All PASS

**Step 7: Commit**

```bash
git add kalshi/common/constants.py kalshi/common/errors.py tests/test_constants.py tests/test_errors.py
git commit -m "feat(kalshi): add constants and error handling modules"
```

---

## Task 3: Review Tasks 1-2

**Spec Review — verify these specific items:**
- [ ] All archived files exist in `archive/` — `ls archive/` should show all files from the move list
- [ ] No strategy/ML imports remain at top level — `ls *.py` should show only `collector.py`, `check_orders.py`, `get_schemas.py` (and any untracked test files that weren't committed)
- [ ] `kalshi/` package structure matches the design doc file tree
- [ ] `KALSHI_VENUE.value` equals `"KALSHI"` (not `"Kalshi"` or `"kalshi"`)
- [ ] `should_retry()` returns `True` for status 429, 500, 502, 503 — returns `False` for 400, 401, 403, 404
- [ ] All `__init__.py` files exist: `kalshi/`, `kalshi/common/`, `kalshi/websocket/`, `tests/`

**Code Quality Review — verify these specific items:**
- [ ] No hardcoded URLs outside `constants.py`
- [ ] `should_retry()` handles both `ApiException` and `KalshiApiError` (two different exception types)
- [ ] Tests assert on specific values, not just `assert result` or `assert result is not None`

---

## Task 4: Milestone — Project reset and foundation

**Present to user:**
- All strategy/ML code archived to `archive/`
- Clean `kalshi/` package structure created
- Constants module with venue, URLs, rate limit costs
- Error handling with retry logic for 429/5xx
- Test results for constants and errors

**Wait for user response before proceeding to Task 5.**

---

## Task 5: Implement WebSocket message schemas (msgspec)

**Files:**
- Create: `kalshi/websocket/types.py`
- Create: `tests/test_ws_types.py`

**Step 1: Write tests with real Kalshi JSON fixtures**

Create `tests/test_ws_types.py`:
```python
"""Tests for kalshi.websocket.types — msgspec WS message schemas."""
import msgspec

from kalshi.websocket.types import (
    OrderbookSnapshot,
    OrderbookDelta,
    UserOrderMsg,
    FillMsg,
    WsMessage,
)


# -- Orderbook Snapshot --

SNAPSHOT_JSON = b'''{
  "type": "orderbook_snapshot",
  "sid": 2,
  "seq": 1,
  "msg": {
    "market_ticker": "KXHIGHCHI-26MAR14-T55",
    "market_id": "abc-123",
    "yes_dollars_fp": [["0.3200", "5.00"], ["0.3100", "10.00"]],
    "no_dollars_fp": [["0.6800", "3.00"]]
  }
}'''


def test_decode_orderbook_snapshot():
    msg = msgspec.json.decode(SNAPSHOT_JSON, type=WsMessage)
    assert msg.type == "orderbook_snapshot"
    assert msg.sid == 2
    assert msg.seq == 1
    snap = msg.msg
    assert snap.market_ticker == "KXHIGHCHI-26MAR14-T55"
    assert len(snap.yes_dollars_fp) == 2
    assert snap.yes_dollars_fp[0] == ["0.3200", "5.00"]
    assert len(snap.no_dollars_fp) == 1


# -- Orderbook Delta --

DELTA_JSON = b'''{
  "type": "orderbook_delta",
  "sid": 2,
  "seq": 3,
  "msg": {
    "market_ticker": "KXHIGHCHI-26MAR14-T55",
    "price_dollars": "0.3200",
    "delta_fp": "-5.00",
    "side": "yes"
  }
}'''


def test_decode_orderbook_delta():
    msg = msgspec.json.decode(DELTA_JSON, type=WsMessage)
    assert msg.type == "orderbook_delta"
    assert msg.msg.side == "yes"
    assert msg.msg.price_dollars == "0.3200"
    assert msg.msg.delta_fp == "-5.00"


# -- User Order --

USER_ORDER_JSON = b'''{
  "type": "user_order",
  "sid": 5,
  "seq": 1,
  "msg": {
    "order_id": "uuid-123",
    "client_order_id": "my-order-1",
    "ticker": "KXHIGHCHI-26MAR14-T55",
    "status": "resting",
    "side": "yes",
    "action": "buy",
    "remaining_count_fp": "10.00",
    "fill_count_fp": "0.00"
  }
}'''


def test_decode_user_order():
    msg = msgspec.json.decode(USER_ORDER_JSON, type=WsMessage)
    assert msg.type == "user_order"
    assert msg.msg.order_id == "uuid-123"
    assert msg.msg.status == "resting"
    assert msg.msg.side == "yes"
    assert msg.msg.remaining_count_fp == "10.00"


# -- Fill --

FILL_JSON = b'''{
  "type": "fill",
  "sid": 6,
  "seq": 1,
  "msg": {
    "trade_id": "trade-uuid",
    "order_id": "order-uuid",
    "client_order_id": "my-order-1",
    "market_ticker": "KXHIGHCHI-26MAR14-T55",
    "side": "yes",
    "action": "buy",
    "count_fp": "5.00",
    "yes_price_dollars": "0.3200",
    "no_price_dollars": "0.6800",
    "fee_cost": "0.0056",
    "is_taker": false,
    "ts": 1703123456
  }
}'''


def test_decode_fill():
    msg = msgspec.json.decode(FILL_JSON, type=WsMessage)
    assert msg.type == "fill"
    assert msg.msg.trade_id == "trade-uuid"
    assert msg.msg.market_ticker == "KXHIGHCHI-26MAR14-T55"
    assert msg.msg.count_fp == "5.00"
    assert msg.msg.fee_cost == "0.0056"
    assert msg.msg.is_taker is False
    assert msg.msg.ts == 1703123456


# -- Settlement fill (no ticker) --

SETTLEMENT_FILL_JSON = b'''{
  "type": "fill",
  "sid": 6,
  "seq": 2,
  "msg": {
    "trade_id": "settle-uuid",
    "order_id": "settle-order",
    "side": "yes",
    "action": "buy",
    "count_fp": "5.00",
    "yes_price_dollars": "1.0000",
    "fee_cost": "0.0000",
    "is_taker": false,
    "ts": 1703200000
  }
}'''


def test_settlement_fill_has_no_ticker():
    msg = msgspec.json.decode(SETTLEMENT_FILL_JSON, type=WsMessage)
    assert msg.type == "fill"
    assert msg.msg.market_ticker is None  # settlement fills omit ticker


# -- Edge: delta_fp of "0.00" means remove level --

DELTA_REMOVE_JSON = b'''{
  "type": "orderbook_delta",
  "sid": 2,
  "seq": 4,
  "msg": {
    "market_ticker": "KXHIGHCHI-26MAR14-T55",
    "price_dollars": "0.3200",
    "delta_fp": "0.00",
    "side": "yes"
  }
}'''


def test_delta_zero_means_remove():
    msg = msgspec.json.decode(DELTA_REMOVE_JSON, type=WsMessage)
    assert float(msg.msg.delta_fp) == 0.0
```

**Step 2: Run tests to verify they fail**

```bash
pytest tests/test_ws_types.py -v
```

Expected: `ModuleNotFoundError`

**Step 3: Implement WebSocket message schemas**

Write `kalshi/websocket/types.py`:
```python
"""msgspec schema classes for Kalshi WebSocket messages.

All fields use the exact names from the Kalshi WS API.
Optional fields are typed as such to handle settlement fills
and other edge cases where fields may be absent.
"""
import msgspec


class OrderbookSnapshotMsg(msgspec.Struct):
    """Orderbook snapshot — sent on initial subscribe."""
    market_ticker: str
    yes_dollars_fp: list[list[str]]  # [[price_str, qty_str], ...]
    no_dollars_fp: list[list[str]]
    market_id: str | None = None


class OrderbookDeltaMsg(msgspec.Struct):
    """Orderbook delta — incremental update."""
    market_ticker: str
    price_dollars: str
    delta_fp: str
    side: str  # "yes" or "no"


class UserOrderMsg(msgspec.Struct):
    """User order status update."""
    order_id: str
    ticker: str
    status: str  # "resting", "canceled", "executed"
    side: str
    action: str
    client_order_id: str | None = None
    remaining_count_fp: str | None = None
    fill_count_fp: str | None = None
    initial_count_fp: str | None = None
    yes_price_dollars: str | None = None
    no_price_dollars: str | None = None
    created_time: str | None = None
    last_update_time: str | None = None


class FillMsg(msgspec.Struct):
    """Fill notification."""
    trade_id: str
    order_id: str
    side: str
    action: str
    count_fp: str
    is_taker: bool
    ts: int  # unix seconds
    client_order_id: str | None = None
    market_ticker: str | None = None  # None for settlement fills
    yes_price_dollars: str | None = None
    no_price_dollars: str | None = None
    fee_cost: str | None = None
    no_price_dollars: str | None = None


class WsMessage(msgspec.Struct):
    """Top-level WebSocket message envelope."""
    type: str
    sid: int = 0
    seq: int = 0
    msg: (
        OrderbookSnapshotMsg
        | OrderbookDeltaMsg
        | UserOrderMsg
        | FillMsg
        | dict  # fallback for unknown message types
    ) = {}
```

Note: The `WsMessage.msg` union type may need adjustment depending on how `msgspec` handles tagged unions. If decoding fails because msgspec can't distinguish between the union members, you'll need to decode `msg` as `dict` first and then decode the inner type based on the `type` field. Test this — if the union approach doesn't work, switch to:

```python
class WsMessage(msgspec.Struct):
    type: str
    sid: int = 0
    seq: int = 0
    msg: dict = {}  # manually decode based on type field
```

And add decoder helper:
```python
_DECODERS = {
    "orderbook_snapshot": msgspec.json.Decoder(OrderbookSnapshotMsg),
    "orderbook_delta": msgspec.json.Decoder(OrderbookDeltaMsg),
    "user_order": msgspec.json.Decoder(UserOrderMsg),
    "fill": msgspec.json.Decoder(FillMsg),
}


def decode_ws_msg(raw: bytes) -> tuple[str, int, int, object]:
    """Decode a raw WS message. Returns (type, sid, seq, typed_msg)."""
    envelope = msgspec.json.decode(raw)  # dict
    msg_type = envelope.get("type", "")
    sid = envelope.get("sid", 0)
    seq = envelope.get("seq", 0)
    inner = envelope.get("msg", {})
    decoder = _DECODERS.get(msg_type)
    if decoder is not None:
        typed_msg = decoder.decode(msgspec.json.encode(inner))
    else:
        typed_msg = inner
    return msg_type, sid, seq, typed_msg
```

Update the tests to use whichever approach works. The key requirement is: every test fixture must parse successfully and fields must be accessible by name.

**Step 4: Run tests**

```bash
pytest tests/test_ws_types.py -v
```

Expected: All PASS. If union type decoding fails, switch to the `decode_ws_msg()` approach and update tests.

**Step 5: Commit**

```bash
git add kalshi/websocket/types.py tests/test_ws_types.py
git commit -m "feat(kalshi): add msgspec schemas for WebSocket messages"
```

---

## Task 6: Implement WebSocket client wrapper

**Files:**
- Create: `kalshi/websocket/client.py`
- Create: `tests/test_ws_client.py`

This wraps `nautilus_pyo3.WebSocketClient` with Kalshi-specific subscription management, auth header generation, and sequence tracking.

**Step 1: Write tests**

Create `tests/test_ws_client.py`:
```python
"""Tests for KalshiWebSocketClient — subscription management and auth.

These are unit tests with mocked WebSocketClient. Integration tests
against the real Kalshi WS API are in tests/test_integration.py.
"""
import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from kalshi.websocket.client import KalshiWebSocketClient


@pytest.fixture
def mock_clock():
    clock = MagicMock()
    clock.timestamp_ns.return_value = 1_000_000_000
    return clock


def test_init_sets_state(mock_clock):
    client = KalshiWebSocketClient(
        clock=mock_clock,
        base_url="wss://demo-api.kalshi.co/trade-api/ws/v2",
        channels=["orderbook_delta"],
        handler=lambda raw: None,
        api_key_id="test-key",
        private_key_path="/dev/null",
        loop=asyncio.new_event_loop(),
    )
    assert client._channels == ["orderbook_delta"]
    assert client._subscribed_tickers == set()
    assert client._ws_client is None


def test_add_subscription_tracks_tickers(mock_clock):
    client = KalshiWebSocketClient(
        clock=mock_clock,
        base_url="wss://demo-api.kalshi.co/trade-api/ws/v2",
        channels=["orderbook_delta"],
        handler=lambda raw: None,
        api_key_id="test-key",
        private_key_path="/dev/null",
        loop=asyncio.new_event_loop(),
    )
    client.add_ticker("KXHIGHCHI-26MAR14-T55")
    assert "KXHIGHCHI-26MAR14-T55" in client._subscribed_tickers


def test_sequence_gap_detected(mock_clock):
    """Sequence tracker should detect gaps."""
    client = KalshiWebSocketClient(
        clock=mock_clock,
        base_url="wss://demo-api.kalshi.co/trade-api/ws/v2",
        channels=["orderbook_delta"],
        handler=lambda raw: None,
        api_key_id="test-key",
        private_key_path="/dev/null",
        loop=asyncio.new_event_loop(),
    )
    # First message sets the baseline
    assert client._check_sequence(sid=1, seq=1) is True
    # Sequential is fine
    assert client._check_sequence(sid=1, seq=2) is True
    # Gap detected
    assert client._check_sequence(sid=1, seq=5) is False
```

**Step 2: Run tests to verify they fail**

```bash
pytest tests/test_ws_client.py -v
```

**Step 3: Implement WebSocket client**

Write `kalshi/websocket/client.py`:
```python
"""Kalshi WebSocket client wrapping nautilus_pyo3.WebSocketClient.

Handles:
- Auth header generation via KalshiAuth
- Channel subscription management
- Sequence number gap detection
- Reconnection with resubscription
"""
import asyncio
import logging
import time

import msgspec

from nautilus_trader.core.nautilus_pyo3 import WebSocketClient, WebSocketConfig

from kalshi_python.api_client import KalshiAuth

log = logging.getLogger(__name__)


class KalshiWebSocketClient:
    """Wraps nautilus_pyo3.WebSocketClient for Kalshi-specific behavior."""

    def __init__(
        self,
        clock,
        base_url: str,
        channels: list[str],
        handler,  # Callable[[bytes], None]
        api_key_id: str,
        private_key_path: str,
        loop: asyncio.AbstractEventLoop,
    ):
        self._clock = clock
        self._base_url = base_url
        self._channels = channels
        self._handler = handler
        self._loop = loop
        self._auth = KalshiAuth(api_key_id, private_key_path)

        self._ws_client: WebSocketClient | None = None
        self._subscribed_tickers: set[str] = set()
        self._seq_tracker: dict[int, int] = {}  # sid -> last_seq
        self._next_cmd_id = 1

    def add_ticker(self, ticker: str) -> None:
        """Register a ticker for subscription (or resubscription on reconnect)."""
        self._subscribed_tickers.add(ticker)

    def remove_ticker(self, ticker: str) -> None:
        self._subscribed_tickers.discard(ticker)

    def _check_sequence(self, sid: int, seq: int) -> bool:
        """Track sequence numbers per subscription. Returns False on gap."""
        last = self._seq_tracker.get(sid)
        self._seq_tracker[sid] = seq
        if last is None:
            return True  # first message for this sid
        if seq != last + 1:
            log.warning(f"Sequence gap on sid={sid}: expected {last+1}, got {seq}")
            return False
        return True

    async def connect(self) -> None:
        """Connect to Kalshi WebSocket with auth headers."""
        headers = self._auth.create_auth_headers("GET", "/trade-api/ws/v2")
        # KalshiAuth returns a dict; WebSocketConfig expects list of tuples
        header_list = [(k, v) for k, v in headers.items()]

        config = WebSocketConfig(
            url=self._base_url,
            headers=header_list,
            heartbeat=None,  # Kalshi has no heartbeat requirement
            reconnect_timeout_ms=10_000,
            reconnect_delay_initial_ms=2_000,
            reconnect_delay_max_ms=30_000,
            reconnect_backoff_factor=1.5,
        )

        self._ws_client = await WebSocketClient.connect(
            loop_=self._loop,
            config=config,
            handler=self._handler,
            post_reconnection=self._on_reconnect,
        )

        # Subscribe to channels for all registered tickers
        await self._subscribe_all()

    async def _subscribe_all(self) -> None:
        """Send subscribe commands for all registered tickers and channels."""
        if not self._ws_client or not self._subscribed_tickers:
            return

        tickers = list(self._subscribed_tickers)
        cmd = {
            "id": self._next_cmd_id,
            "cmd": "subscribe",
            "params": {
                "channels": self._channels,
                "market_tickers": tickers,
            },
        }
        self._next_cmd_id += 1
        await self._ws_client.send_text(msgspec.json.encode(cmd))
        log.info(f"Subscribed to {self._channels} for {len(tickers)} tickers")

    async def subscribe_ticker(self, ticker: str) -> None:
        """Subscribe to a single ticker (for dynamic subscription after connect)."""
        self.add_ticker(ticker)
        if self._ws_client:
            cmd = {
                "id": self._next_cmd_id,
                "cmd": "subscribe",
                "params": {
                    "channels": self._channels,
                    "market_tickers": [ticker],
                },
            }
            self._next_cmd_id += 1
            await self._ws_client.send_text(msgspec.json.encode(cmd))

    def _on_reconnect(self) -> None:
        """Called after WebSocketClient auto-reconnects. Resubscribe all."""
        self._seq_tracker.clear()  # Reset sequence tracking
        log.info("WebSocket reconnected, resubscribing...")
        asyncio.ensure_future(self._subscribe_all())

    async def disconnect(self) -> None:
        if self._ws_client:
            await self._ws_client.disconnect()
            self._ws_client = None

    @property
    def is_connected(self) -> bool:
        return self._ws_client is not None and self._ws_client.is_active()
```

**Step 4: Run tests**

```bash
pytest tests/test_ws_client.py -v
```

**Step 5: Commit**

```bash
git add kalshi/websocket/client.py tests/test_ws_client.py
git commit -m "feat(kalshi): add WebSocket client wrapper with sequence tracking"
```

---

## Task 7: Review Tasks 5-6

**Spec Review — verify these specific items:**
- [ ] Every Kalshi WS message type has a test fixture: `orderbook_snapshot`, `orderbook_delta`, `user_order`, `fill`, settlement fill (no ticker)
- [ ] `FillMsg.market_ticker` is `Optional[str]` — not required — so settlement fills parse without error
- [ ] `FillMsg.fee_cost` is `Optional[str]` — Kalshi may omit it on settlement fills
- [ ] `OrderbookSnapshotMsg.yes_dollars_fp` is `list[list[str]]` not `list[tuple[str, str]]` (JSON arrays decode as lists)
- [ ] Sequence gap detection in `KalshiWebSocketClient._check_sequence()` returns `False` on gap, `True` on sequential
- [ ] Reconnection handler (`_on_reconnect`) clears sequence tracker and resubscribes all tickers
- [ ] Auth headers are generated via `KalshiAuth.create_auth_headers("GET", "/trade-api/ws/v2")` with correct method and path

**Code Quality Review — verify these specific items:**
- [ ] `WebSocketConfig.headers` receives `list[tuple[str, str]]` (not dict) — check that conversion from `KalshiAuth` dict output is correct
- [ ] `send_text` receives `bytes` (from `msgspec.json.encode`) not `str`
- [ ] No duplicate `no_price_dollars` field in `FillMsg` (there's a typo risk in the schema — check for duplicate field names)

---

## Task 8: Milestone — WebSocket foundation

**Present to user:**
- msgspec schemas for all WS message types with test fixtures
- WebSocket client wrapper with sequence tracking and reconnection
- Test results

**Wait for user response before proceeding to Task 9.**

---

## Task 9: Implement instrument provider

**Files:**
- Create: `kalshi/providers.py`
- Create: `tests/test_providers.py`

The instrument provider loads Kalshi markets via the SDK and creates `BinaryOption` instruments.

**Step 1: Write tests**

Create `tests/test_providers.py`:
```python
"""Tests for KalshiInstrumentProvider — instrument creation and loading.

Unit tests with mocked SDK. Integration tests are in tests/test_integration.py.
"""
import json
from decimal import Decimal
from unittest.mock import MagicMock, patch

import pytest

from nautilus_trader.model.identifiers import InstrumentId, Symbol, Venue
from nautilus_trader.model.instruments import BinaryOption
from nautilus_trader.model.enums import AssetClass

from kalshi.common.constants import KALSHI_VENUE
from kalshi.providers import KalshiInstrumentProvider, parse_instrument_id


def test_parse_instrument_id_yes():
    ticker, side = parse_instrument_id(
        InstrumentId(Symbol("KXHIGHCHI-26MAR14-T55-YES"), KALSHI_VENUE)
    )
    assert ticker == "KXHIGHCHI-26MAR14-T55"
    assert side == "yes"


def test_parse_instrument_id_no():
    ticker, side = parse_instrument_id(
        InstrumentId(Symbol("KXHIGHCHI-26MAR14-T55-NO"), KALSHI_VENUE)
    )
    assert ticker == "KXHIGHCHI-26MAR14-T55"
    assert side == "no"


def test_parse_instrument_id_invalid():
    with pytest.raises(ValueError, match="Cannot parse side"):
        parse_instrument_id(
            InstrumentId(Symbol("KXHIGHCHI-26MAR14-T55"), KALSHI_VENUE)
        )


def _mock_market(ticker="KXHIGHCHI-26MAR14-T55", status="active"):
    """Create a mock market object resembling Kalshi SDK response."""
    m = MagicMock()
    m.ticker = ticker
    m.status = status
    m.title = "Will Chicago high temp be 55°F or above on Mar 14?"
    m.close_time = "2026-03-14T12:00:00Z"
    m.expiration_time = "2026-03-15T00:00:00Z"
    return m


def test_build_instrument_creates_binary_option():
    provider = KalshiInstrumentProvider.__new__(KalshiInstrumentProvider)
    # Bypass __init__ — we just want to test _build_instrument
    instrument = provider._build_instrument(_mock_market(), "YES")
    assert isinstance(instrument, BinaryOption)
    assert instrument.id == InstrumentId(Symbol("KXHIGHCHI-26MAR14-T55-YES"), KALSHI_VENUE)
    assert instrument.asset_class == AssetClass.ALTERNATIVE
    assert instrument.price_precision == 2
    assert instrument.size_precision == 0
    assert instrument.outcome == "Yes"
    assert instrument.info["kalshi_ticker"] == "KXHIGHCHI-26MAR14-T55"


def test_build_instrument_no_side():
    provider = KalshiInstrumentProvider.__new__(KalshiInstrumentProvider)
    instrument = provider._build_instrument(_mock_market(), "NO")
    assert instrument.id == InstrumentId(Symbol("KXHIGHCHI-26MAR14-T55-NO"), KALSHI_VENUE)
    assert instrument.outcome == "No"
```

**Step 2: Implement**

Write `kalshi/providers.py`:
```python
"""Kalshi instrument provider — loads markets and creates BinaryOption instruments."""
import asyncio
import json
import logging
from datetime import datetime, timezone
from decimal import Decimal

import kalshi_python
from kalshi_python.api.markets_api import MarketsApi
from kalshi_python.api_client import KalshiAuth

from nautilus_trader.common.providers import InstrumentProvider
from nautilus_trader.model.enums import AssetClass
from nautilus_trader.model.identifiers import InstrumentId, Symbol
from nautilus_trader.model.instruments import BinaryOption
from nautilus_trader.model.objects import Currency, Price, Quantity

from kalshi.common.constants import KALSHI_VENUE

log = logging.getLogger(__name__)


def parse_instrument_id(instrument_id: InstrumentId) -> tuple[str, str]:
    """Extract (ticker, side) from an InstrumentId like 'TICKER-YES.KALSHI'."""
    val = instrument_id.symbol.value
    if val.endswith("-YES"):
        return val[:-4], "yes"
    elif val.endswith("-NO"):
        return val[:-3], "no"
    else:
        raise ValueError(
            f"Cannot parse side from instrument {val!r}. "
            f"Expected suffix '-YES' or '-NO'."
        )


class KalshiInstrumentProvider(InstrumentProvider):
    """Loads Kalshi markets and provides BinaryOption instruments."""

    def __init__(
        self,
        api_key_id: str,
        private_key_path: str,
        rest_host: str,
    ):
        super().__init__()
        self._api_config = kalshi_python.Configuration()
        self._api_config.host = rest_host
        self._client = kalshi_python.KalshiClient(self._api_config)
        self._client.set_kalshi_auth(
            key_id=api_key_id, private_key_path=private_key_path,
        )
        self._markets_api = MarketsApi(self._client)

    async def load_all_async(
        self,
        filters: dict | None = None,
    ) -> None:
        """Load instruments from Kalshi API. Filters: series_ticker, status, event_ticker."""
        await asyncio.to_thread(self._load_all_sync, filters)

    def _load_all_sync(self, filters: dict | None = None) -> None:
        """Synchronous market loading — called via asyncio.to_thread."""
        filters = filters or {}
        series_ticker = filters.get("series_ticker")
        statuses = filters.get("statuses", ["open", "unopened"])

        for status in statuses:
            cursor = None
            while True:
                resp = self._markets_api.get_markets(
                    limit=200,
                    cursor=cursor,
                    status=status,
                    series_ticker=series_ticker,
                )
                for m in getattr(resp, "markets", None) or []:
                    if m.status not in ("active", "open", "unopened"):
                        continue
                    self.add(self._build_instrument(m, "YES"))
                    self.add(self._build_instrument(m, "NO"))

                cursor = getattr(resp, "cursor", None)
                if not cursor:
                    break

        log.info(f"Loaded {self.count} instruments from Kalshi")

    def _build_instrument(self, market, side: str) -> BinaryOption:
        """Create a BinaryOption instrument from a Kalshi market object."""
        ticker = market.ticker
        instrument_id = InstrumentId(
            Symbol(f"{ticker}-{side}"), KALSHI_VENUE,
        )

        # Parse expiration from market close_time
        expiration_ns = 0
        close_time = getattr(market, "close_time", None) or getattr(market, "expiration_time", None)
        if close_time:
            try:
                dt = datetime.fromisoformat(close_time.replace("Z", "+00:00"))
                expiration_ns = int(dt.timestamp() * 1_000_000_000)
            except (ValueError, AttributeError):
                pass

        ts_now = int(datetime.now(timezone.utc).timestamp() * 1_000_000_000)

        return BinaryOption(
            instrument_id=instrument_id,
            raw_symbol=Symbol(f"{ticker}-{side}"),
            asset_class=AssetClass.ALTERNATIVE,
            currency=Currency.from_str("USD"),
            price_precision=2,
            size_precision=0,
            price_increment=Price.from_str("0.01"),
            size_increment=Quantity.from_int(1),
            activation_ns=0,
            expiration_ns=expiration_ns,
            ts_event=ts_now,
            ts_init=ts_now,
            maker_fee=Decimal("0.0175"),
            taker_fee=Decimal("0.07"),
            outcome="Yes" if side == "YES" else "No",
            description=getattr(market, "title", ""),
            info={
                "kalshi_ticker": ticker,
                "side": side.lower(),
                "status": getattr(market, "status", ""),
            },
        )
```

**Step 3: Run tests**

```bash
pytest tests/test_providers.py -v
```

**Step 4: Commit**

```bash
git add kalshi/providers.py tests/test_providers.py
git commit -m "feat(kalshi): add instrument provider with BinaryOption support"
```

---

## Task 10: Review Task 9

**Spec Review — verify these specific items:**
- [ ] `parse_instrument_id()` correctly strips `-YES`/`-NO` suffix and returns lowercase side
- [ ] `_build_instrument()` creates `BinaryOption` not `CurrencyPair` — check the actual return type
- [ ] `maker_fee=Decimal("0.0175")` and `taker_fee=Decimal("0.07")` match Kalshi's fee schedule
- [ ] `price_precision=2` and `size_precision=0` — prices are 2 decimal places (dollars), sizes are integers
- [ ] `outcome` is `"Yes"` or `"No"` (capitalized) — matches Polymarket convention
- [ ] `info` dict includes `kalshi_ticker` (the raw ticker without side suffix) for use in API calls
- [ ] No singleton pattern (the old adapter used a global `_SHARED_PROVIDER` — this must NOT exist)
- [ ] `load_all_async` uses `asyncio.to_thread` for the SDK call (not ThreadPoolExecutor)

**Code Quality Review — verify these specific items:**
- [ ] Cursor pagination loop terminates when `cursor` is None/empty
- [ ] Market status filter matches design doc: `("active", "open", "unopened")`

---

## Task 11: Milestone — Instrument provider

**Present to user:**
- Instrument provider with BinaryOption instruments
- `parse_instrument_id()` utility
- Cursor-paginated market loading
- Test results

**Wait for user response before proceeding to Task 12.**

---

## Task 12: Implement config and factories

> **Before starting:** Invoke `forge:nautilus-testing` skill — covers NT config/factory patterns.

**Files:**
- Create: `kalshi/config.py`
- Create: `kalshi/factories.py`
- Create: `tests/test_config.py`

**Step 1: Write tests**

Create `tests/test_config.py`:
```python
"""Tests for Kalshi config and factory classes."""
from kalshi.config import KalshiDataClientConfig, KalshiExecClientConfig
from kalshi.common.constants import KALSHI_VENUE


def test_data_config_defaults():
    config = KalshiDataClientConfig(
        api_key_id="test", private_key_path="/tmp/key.pem",
    )
    assert config.venue == KALSHI_VENUE
    assert config.environment == "demo"
    assert "demo-api" in config.base_url_ws


def test_exec_config_defaults():
    config = KalshiExecClientConfig(
        api_key_id="test", private_key_path="/tmp/key.pem",
    )
    assert config.venue == KALSHI_VENUE
    assert config.ack_timeout_secs == 5.0


def test_prod_config_uses_prod_urls():
    config = KalshiDataClientConfig(
        api_key_id="test", private_key_path="/tmp/key.pem",
        environment="production",
    )
    assert "api.elections.kalshi.com" in config.base_url_ws
```

**Step 2: Implement config**

Write `kalshi/config.py`:
```python
"""Kalshi adapter configuration."""
from nautilus_trader.config import LiveDataClientConfig, LiveExecClientConfig, PositiveFloat, PositiveInt
from nautilus_trader.model.identifiers import Venue

from kalshi.common.constants import (
    KALSHI_VENUE,
    DEMO_REST_URL, DEMO_WS_URL,
    PROD_REST_URL, PROD_WS_URL,
)


class KalshiDataClientConfig(LiveDataClientConfig, frozen=True):
    venue: Venue = KALSHI_VENUE
    api_key_id: str = ""
    private_key_path: str = ""
    environment: str = "demo"  # "demo" or "production"
    base_url_http: str | None = None  # override auto-detected URL
    base_url_ws: str | None = None
    series_ticker: str | None = None  # filter instruments on load
    update_instruments_interval_mins: PositiveInt | None = None

    @property
    def rest_url(self) -> str:
        if self.base_url_http:
            return self.base_url_http
        return PROD_REST_URL if self.environment == "production" else DEMO_REST_URL

    @property
    def ws_url(self) -> str:
        if self.base_url_ws:
            return self.base_url_ws
        return PROD_WS_URL if self.environment == "production" else DEMO_WS_URL


class KalshiExecClientConfig(LiveExecClientConfig, frozen=True):
    venue: Venue = KALSHI_VENUE
    api_key_id: str = ""
    private_key_path: str = ""
    environment: str = "demo"
    base_url_http: str | None = None
    base_url_ws: str | None = None
    max_retries: PositiveInt | None = 3
    retry_delay_initial_ms: PositiveInt | None = 1_000
    retry_delay_max_ms: PositiveInt | None = 10_000
    ack_timeout_secs: PositiveFloat = 5.0

    @property
    def rest_url(self) -> str:
        if self.base_url_http:
            return self.base_url_http
        return PROD_REST_URL if self.environment == "production" else DEMO_REST_URL

    @property
    def ws_url(self) -> str:
        if self.base_url_ws:
            return self.base_url_ws
        return PROD_WS_URL if self.environment == "production" else DEMO_WS_URL
```

Note: NT config classes use `frozen=True` (msgspec Struct). Properties may not work on frozen structs — if so, use a method `def get_rest_url(self)` instead, or make the URLs computed at factory time and passed as fields.

**Step 3: Implement factories**

Write `kalshi/factories.py`:
```python
"""Kalshi adapter factories."""
import functools

from nautilus_trader.live.factories import LiveDataClientFactory, LiveExecClientFactory

from kalshi.config import KalshiDataClientConfig, KalshiExecClientConfig
from kalshi.providers import KalshiInstrumentProvider


@functools.lru_cache(1)
def get_kalshi_instrument_provider(
    api_key_id: str,
    private_key_path: str,
    rest_host: str,
) -> KalshiInstrumentProvider:
    """Singleton instrument provider shared between data and exec clients."""
    return KalshiInstrumentProvider(
        api_key_id=api_key_id,
        private_key_path=private_key_path,
        rest_host=rest_host,
    )


class KalshiLiveDataClientFactory(LiveDataClientFactory):
    @staticmethod
    def create(loop, name, config, msgbus, cache, clock):
        from kalshi.data import KalshiDataClient  # deferred to avoid circular import

        provider = get_kalshi_instrument_provider(
            config.api_key_id, config.private_key_path, config.rest_url,
        )
        return KalshiDataClient(
            loop=loop,
            config=config,
            instrument_provider=provider,
            msgbus=msgbus,
            cache=cache,
            clock=clock,
        )


class KalshiLiveExecClientFactory(LiveExecClientFactory):
    @staticmethod
    def create(loop, name, config, msgbus, cache, clock):
        from kalshi.execution import KalshiExecutionClient

        provider = get_kalshi_instrument_provider(
            config.api_key_id, config.private_key_path, config.rest_url,
        )
        return KalshiExecutionClient(
            loop=loop,
            config=config,
            instrument_provider=provider,
            msgbus=msgbus,
            cache=cache,
            clock=clock,
        )
```

**Step 4: Run tests and commit**

```bash
pytest tests/test_config.py -v
git add kalshi/config.py kalshi/factories.py tests/test_config.py
git commit -m "feat(kalshi): add config classes and factory pattern"
```

---

## Task 13: Review Task 12

**Spec Review — verify these specific items:**
- [ ] Config `environment` field defaults to `"demo"` — never defaults to production
- [ ] URL resolution: `environment="demo"` → demo URLs, `environment="production"` → prod URLs
- [ ] Factory uses `@lru_cache(1)` for singleton instrument provider (shared between data + exec)
- [ ] Factory `create()` methods match NT signature: `(loop, name, config, msgbus, cache, clock)`
- [ ] `frozen=True` on config classes — if properties don't work on frozen structs, verify URL resolution still works

**Code Quality Review:**
- [ ] No credential values hardcoded — all come from config
- [ ] Factory defers import of data/execution client to avoid circular imports

---

## Task 14: Milestone — Config, factories, and instrument provider

**Present to user:**
- Config classes with environment-based URL resolution
- Factory pattern with singleton instrument provider
- Full package structure so far: `kalshi/common/`, `kalshi/websocket/`, `kalshi/config.py`, `kalshi/factories.py`, `kalshi/providers.py`
- All test results

**Wait for user response before proceeding to Task 15.**

---

## Task 15: Implement data client

**Files:**
- Create: `kalshi/data.py`
- Create: `tests/test_data_client.py`

The data client subscribes to orderbook data via WebSocket and emits QuoteTicks.

**Step 1: Write tests for quote derivation logic**

Create `tests/test_data_client.py`:
```python
"""Tests for KalshiDataClient — quote derivation and book management.

Tests the core logic: orderbook snapshot/delta → QuoteTick emission.
Uses mocked NT internals (clock, cache, msgbus).
"""
import pytest
from unittest.mock import MagicMock, call
from decimal import Decimal

from nautilus_trader.model.data import QuoteTick
from nautilus_trader.model.identifiers import InstrumentId, Symbol
from nautilus_trader.model.objects import Price, Quantity
from nautilus_trader.model.instruments import BinaryOption
from nautilus_trader.model.enums import AssetClass

from kalshi.common.constants import KALSHI_VENUE
from kalshi.data import KalshiDataClient, _derive_quotes


def _make_instrument(ticker, side):
    """Create a minimal BinaryOption for testing."""
    return BinaryOption(
        instrument_id=InstrumentId(Symbol(f"{ticker}-{side}"), KALSHI_VENUE),
        raw_symbol=Symbol(f"{ticker}-{side}"),
        asset_class=AssetClass.ALTERNATIVE,
        currency=MagicMock(),  # Currency.from_str("USD") may need full NT init
        price_precision=2,
        size_precision=0,
        price_increment=Price.from_str("0.01"),
        size_increment=Quantity.from_int(1),
        activation_ns=0,
        expiration_ns=0,
        ts_event=0,
        ts_init=0,
        outcome="Yes" if side == "YES" else "No",
    )


class TestDeriveQuotes:
    """Test the _derive_quotes function that converts orderbook state to QuoteTicks."""

    def test_normal_book(self):
        """Both YES and NO bids present → computes bid/ask for both sides."""
        yes_book = {0.32: 5.0, 0.31: 10.0}
        no_book = {0.68: 3.0, 0.67: 7.0}
        quotes = _derive_quotes("TICKER", yes_book, no_book)

        # YES: bid=0.32, ask=1-0.68=0.32 → exact match (thin spread)
        assert quotes["YES"]["bid"] == 0.32
        assert quotes["YES"]["ask"] == pytest.approx(1.0 - 0.68)
        assert quotes["YES"]["bid_size"] == 5.0
        assert quotes["YES"]["ask_size"] == 3.0

        # NO: bid=0.68, ask=1-0.32=0.68
        assert quotes["NO"]["bid"] == 0.68
        assert quotes["NO"]["ask"] == pytest.approx(1.0 - 0.32)

    def test_empty_yes_book(self):
        """Only NO bids → YES has no bid, NO has no ask."""
        quotes = _derive_quotes("TICKER", {}, {0.68: 3.0})
        assert quotes["YES"]["bid"] == 0.0
        assert quotes["YES"]["ask"] == pytest.approx(1.0 - 0.68)
        assert quotes["NO"]["bid"] == 0.68
        assert quotes["NO"]["ask"] == 1.0  # no YES bids → ask is 1.0

    def test_empty_both_books(self):
        """Empty book → no quotes."""
        quotes = _derive_quotes("TICKER", {}, {})
        assert quotes is None

    def test_single_level_each_side(self):
        """One bid each side."""
        quotes = _derive_quotes("TICKER", {0.50: 10.0}, {0.50: 10.0})
        assert quotes["YES"]["bid"] == 0.50
        assert quotes["YES"]["ask"] == 0.50
        assert quotes["NO"]["bid"] == 0.50
        assert quotes["NO"]["ask"] == 0.50
```

**Step 2: Implement data client**

Write `kalshi/data.py`. This is a large file — implement `_derive_quotes` as a standalone function (testable without NT), and the `KalshiDataClient` class with mocked internals in tests.

Key methods:
- `_connect()`: init instrument provider, start WS, send instruments to data engine
- `_disconnect()`: disconnect WS, clean up
- `_subscribe_quote_ticks()`: add ticker to WS subscriptions
- `_handle_ws_message(raw: bytes)`: decode, route to snapshot/delta/lifecycle handler
- `_handle_orderbook_snapshot()`: replace local book, emit QuoteTicks
- `_handle_orderbook_delta()`: update local book level, emit QuoteTicks
- `_emit_quotes()`: derive quotes from local book, emit QuoteTick for YES and NO

Port the quote derivation logic from `archive/adapter_v1.py:216-270`.

**Step 3: Run tests and commit**

```bash
pytest tests/test_data_client.py -v
git add kalshi/data.py tests/test_data_client.py
git commit -m "feat(kalshi): add data client with quote derivation from bids-only book"
```

---

## Task 16: Implement execution client

> **Before starting:** Invoke `forge:nautilus-testing` skill — covers mocking NT execution internals.

**Files:**
- Create: `kalshi/execution.py`
- Create: `tests/test_execution.py`

This is the largest component. The execution client handles order submission, cancellation, fill processing, and reconciliation.

**Step 1: Write tests for order translation**

Create `tests/test_execution.py` — start with the order translation logic (NT order → Kalshi CreateOrderRequest), then fill processing, then reconciliation.

Key test cases for order translation:
- BUY YES at 0.32 → `action="buy", side="yes", yes_price=32`
- BUY NO at 0.68 → `action="buy", side="no", no_price=68`
- SELL YES at 0.95 → `action="sell", side="yes", yes_price=95`
- SELL NO at 0.90 → `action="sell", side="no", no_price=90`

Key test cases for fill processing:
- Fill with fee_cost → commission correctly parsed as Money
- Settlement fill (no ticker) → skipped without error
- Fill before accepted event → generate_order_accepted called first

Key test cases for reconciliation:
- `generate_order_status_reports` → maps SDK order objects to OrderStatusReport
- `generate_position_status_reports` → maps position_fp to PositionStatusReport
- `generate_fill_reports` → maps SDK fill objects to FillReport

**Step 2: Implement execution client**

Write `kalshi/execution.py`. Key patterns from Polymarket adapter:

```python
class KalshiExecutionClient(LiveExecutionClient):
    def __init__(self, loop, config, instrument_provider, msgbus, cache, clock):
        super().__init__(
            loop=loop,
            client_id=ClientId("KALSHI"),
            venue=KALSHI_VENUE,
            oms_type=OmsType.HEDGING,
            instrument_provider=instrument_provider,
            account_type=AccountType.CASH,
            base_currency=Currency.from_str("USD"),
            msgbus=msgbus, cache=cache, clock=clock,
        )
        # SDK client for REST calls
        self._config = config
        api_config = kalshi_python.Configuration()
        api_config.host = config.rest_url
        self._k_client = kalshi_python.KalshiClient(api_config)
        self._k_client.set_kalshi_auth(
            key_id=config.api_key_id,
            private_key_path=config.private_key_path,
        )
        self._portfolio = PortfolioApi(self._k_client)

        # Retry manager
        self._retry_pool = RetryManagerPool(
            pool_size=100,
            max_retries=config.max_retries or 3,
            delay_initial_ms=config.retry_delay_initial_ms or 1_000,
            delay_max_ms=config.retry_delay_max_ms or 10_000,
            backoff_factor=2,
            logger=self._log,
            exc_types=(ApiException,),
            retry_check=should_retry,
        )

        # WebSocket for user_orders + fill channels
        self._ws_client = KalshiWebSocketClient(...)

        # ACK synchronization
        self._ack_events: dict[str, asyncio.Event] = {}
        self._accepted_orders: dict[ClientOrderId, VenueOrderId] = {}
```

Implement these methods using `asyncio.to_thread` + `_with_http_info` + `json.loads(raw_data)` pattern:
- `_submit_order`, `_cancel_order`, `_cancel_all_orders`, `_modify_order`
- `generate_order_status_reports`, `generate_fill_reports`, `generate_position_status_reports`
- `_handle_ws_message` — route user_order and fill messages

**Step 3: Run tests and commit**

```bash
pytest tests/test_execution.py -v
git add kalshi/execution.py tests/test_execution.py
git commit -m "feat(kalshi): add execution client with order management and reconciliation"
```

---

## Task 17: Review Tasks 15-16

**Spec Review — verify these specific items:**
- [ ] Quote derivation: `YES ask = 1.0 - max(no_bids)`, `NO ask = 1.0 - max(yes_bids)` — verify in `_derive_quotes()`
- [ ] Empty book handling: returns None (no quotes emitted), not zero prices
- [ ] One-sided book: the missing side gets `bid=0.0` and the present side gets `ask=1.0`
- [ ] Order translation: `BUY` on `TICKER-YES` → SDK `action="buy", side="yes", yes_price_dollars=...`
- [ ] Fee parsing: `fee_cost` from fill message → `commission=Money(float(fee_cost), USD)` — NOT hardcoded 0
- [ ] Settlement fill: `market_ticker is None` → logged and skipped, no crash
- [ ] `OmsType.HEDGING` — not NETTING (because YES/NO are separate instruments in NT)
- [ ] All REST calls use `asyncio.to_thread` — no ThreadPoolExecutor
- [ ] All SDK calls use `_with_http_info()` + `json.loads(raw_data)` — no direct SDK model access
- [ ] `generate_fill_reports` returns `FillReport` objects (not empty list)
- [ ] ACK events use `asyncio.Event` with timeout (`ack_timeout_secs` from config)

**Code Quality Review:**
- [ ] `_retry_pool.shutdown()` called in `_stop()` method
- [ ] WebSocket disconnect called in `_disconnect()` method
- [ ] No `strategy_id=None` hack — NT should resolve strategy_id from client_order_id cache

---

## Task 18: Milestone — Complete adapter

**Present to user:**
- Full adapter package: data client, execution client, config, factories, providers, WS client
- Quote derivation from bids-only orderbook
- Order lifecycle: submit → accept → fill with fee tracking
- Reconciliation: order reports, fill reports, position reports
- All unit test results

**Wait for user response before proceeding to Task 19.**

---

## Task 19: Implement integration tests against Kalshi demo API

> **Before starting:** Verify `.env` has valid demo credentials. Run `python -c "from kalshi.providers import KalshiInstrumentProvider"` to confirm imports work.

**Files:**
- Create: `tests/test_integration.py`

**Step 1: Write auth and instrument loading tests**

```python
"""Integration tests against Kalshi demo API.

Requires valid demo credentials in .env:
  KALSHI_API_KEY_ID, KALSHI_PRIVATE_KEY_PATH

Run with: pytest tests/test_integration.py -v -s --timeout=30
Skip with: pytest tests/test_integration.py -v -k "not integration"
"""
import os
import pytest

# Skip entire module if no credentials
pytestmark = pytest.mark.skipif(
    not os.environ.get("KALSHI_API_KEY_ID"),
    reason="KALSHI_API_KEY_ID not set — skipping integration tests",
)


@pytest.fixture(scope="module")
def credentials():
    from dotenv import load_dotenv
    load_dotenv()
    return {
        "api_key_id": os.environ["KALSHI_API_KEY_ID"],
        "private_key_path": os.environ["KALSHI_PRIVATE_KEY_PATH"],
        "rest_host": os.environ.get("KALSHI_REST_HOST", "https://demo-api.kalshi.co/trade-api/v2"),
        "ws_host": os.environ.get("KALSHI_WS_HOST", "wss://demo-api.kalshi.co/trade-api/ws/v2"),
    }


class TestAuth:
    def test_fetch_balance(self, credentials):
        """Verify auth works by fetching account balance."""
        import kalshi_python
        from kalshi_python.api.portfolio_api import PortfolioApi
        import json

        config = kalshi_python.Configuration()
        config.host = credentials["rest_host"]
        client = kalshi_python.KalshiClient(config)
        client.set_kalshi_auth(
            key_id=credentials["api_key_id"],
            private_key_path=credentials["private_key_path"],
        )
        portfolio = PortfolioApi(client)
        resp = portfolio.get_balance_with_http_info()
        data = json.loads(resp.raw_data)
        assert "balance" in data
        assert isinstance(data["balance"], int)


class TestInstrumentProvider:
    def test_load_markets(self, credentials):
        """Load instruments from demo API."""
        from kalshi.providers import KalshiInstrumentProvider
        import asyncio

        provider = KalshiInstrumentProvider(
            api_key_id=credentials["api_key_id"],
            private_key_path=credentials["private_key_path"],
            rest_host=credentials["rest_host"],
        )
        asyncio.run(provider.load_all_async())
        instruments = provider.list_all()
        assert len(instruments) > 0
        # Every instrument should be a BinaryOption
        from nautilus_trader.model.instruments import BinaryOption
        for inst in instruments[:5]:
            assert isinstance(inst, BinaryOption)
            # Instrument ID should end with -YES or -NO
            sym = inst.id.symbol.value
            assert sym.endswith("-YES") or sym.endswith("-NO"), f"Bad symbol: {sym}"
```

**Step 2: Write order lifecycle tests**

Add tests for the full order lifecycle on demo:
- Create a limit order at a non-marketable price (e.g., YES bid at 1 cent)
- Verify it appears in `get_orders(status="resting")`
- Cancel it
- Verify it no longer appears as resting
- Test batch create and batch cancel

**Step 3: Write WebSocket connection test**

Test that the WebSocket client can connect, subscribe to `orderbook_delta` for a real market,
receive at least one snapshot, and disconnect cleanly.

**Step 4: Run and commit**

```bash
pytest tests/test_integration.py -v -s --timeout=60
git add tests/test_integration.py
git commit -m "test(kalshi): add integration tests against demo API"
```

---

## Task 20: Implement order combinatorics tests

> **Before starting:** Invoke `forge:writing-tests` skill — covers edge case selection and assertion quality.

**Files:**
- Extend: `tests/test_execution.py` or create `tests/test_order_combinatorics.py`

Test every combination that could occur in practice:

| Side | Action | Expected SDK params |
|------|--------|-------------------|
| YES  | BUY    | `side="yes", action="buy", yes_price_dollars=X` |
| YES  | SELL   | `side="yes", action="sell", yes_price_dollars=X` |
| NO   | BUY    | `side="no", action="buy", no_price_dollars=X` |
| NO   | SELL   | `side="no", action="sell", no_price_dollars=X` |

For each combination, test:
- Order accepted (resting)
- Order rejected (insufficient funds, invalid price)
- Order cancelled
- Order partially filled
- Order fully filled → position updated
- Sell order on existing position

Edge cases:
- Order at price 0.01 (minimum) and 0.99 (maximum)
- Order for 1 contract and for maximum contracts
- Cancel of already-filled order (should fail gracefully)
- Fill with `is_taker=True` vs `is_taker=False` (different fee coefficients)

```bash
pytest tests/test_order_combinatorics.py -v
git add tests/test_order_combinatorics.py
git commit -m "test(kalshi): add order combinatorics tests"
```

---

## Task 21: Review Tasks 19-20

**Spec Review — verify these specific items:**
- [ ] Integration test connects to demo API and successfully fetches balance (auth works)
- [ ] Instrument loading returns >0 instruments, all are BinaryOption type
- [ ] Order lifecycle: create → verify resting → cancel → verify cancelled — complete cycle tested
- [ ] All 4 side/action combinations tested with correct SDK parameter mapping
- [ ] Edge case: price 0.01 and 0.99 both produce valid orders
- [ ] Edge case: settlement fill (no ticker) doesn't crash
- [ ] Fee parsing: `is_taker=True` → `taker_fee` coefficient used, `is_taker=False` → `maker_fee`

**Validation Data:**
- Check that the demo API balance response has the expected structure: `{"balance": int, "portfolio_value": int}`
- Verify loaded instruments have `price_precision=2` and `size_precision=0`

---

## Task 22: Milestone — Integration testing complete

**Present to user:**
- Integration tests passing against Kalshi demo API
- Auth, instrument loading, order lifecycle all verified
- Order combinatorics coverage (side x action x outcome matrix)
- Edge cases: settlement fills, min/max prices, fee calculation
- All test results

**Wait for user response before proceeding to Task 23.**

---

## Task 23: Implement reconciliation tests

**Files:**
- Create: `tests/test_reconciliation.py`

Test the three report generators against demo API state:

1. Place 2-3 resting orders on demo
2. Call `generate_order_status_reports()` — verify each order appears with correct fields
3. Cancel 1 order, verify it disappears from reports
4. If any fills exist, call `generate_fill_reports()` — verify fill data
5. Call `generate_position_status_reports()` — verify position data matches

Also test restart recovery:
1. Create the execution client
2. Place orders
3. Destroy and recreate the execution client (simulating restart)
4. Call all three report generators
5. Verify state matches what Kalshi has

```bash
pytest tests/test_reconciliation.py -v -s --timeout=120
git add tests/test_reconciliation.py
git commit -m "test(kalshi): add reconciliation tests for state recovery"
```

---

## Task 24: Review Task 23

**Spec Review — verify these specific items:**
- [ ] `generate_order_status_reports()` returns `OrderStatusReport` objects with correct `venue_order_id`, `order_side`, `quantity`, `price`
- [ ] `generate_position_status_reports()` returns `PositionStatusReport` with correct `quantity` matching Kalshi's `position_fp`
- [ ] `generate_fill_reports()` returns `FillReport` objects (not empty list) with `last_qty`, `last_px`, `commission`
- [ ] Restart recovery: fresh client can reconstruct state from Kalshi REST API alone

---

## Task 25: Milestone — Reconciliation verified

**Present to user:**
- All three report generators tested against real demo API state
- Restart recovery works — fresh client reconstructs state correctly
- All test results
- Summary of remaining work (soak test is manual/future)

**Wait for user response before proceeding to Task 26.**

---

## Task 26: Final cleanup and entry point

**Files:**
- Update: `kalshi/__init__.py` — export public API
- Update: `pyproject.toml` — ensure no unused deps, add any missing ones
- Create: `main.py` — minimal entry point for testing the adapter with a TradingNode

**Step 1: Update `kalshi/__init__.py`**

```python
"""Kalshi NautilusTrader adapter."""
from kalshi.common.constants import KALSHI_VENUE
from kalshi.config import KalshiDataClientConfig, KalshiExecClientConfig
from kalshi.factories import KalshiLiveDataClientFactory, KalshiLiveExecClientFactory
from kalshi.providers import KalshiInstrumentProvider, parse_instrument_id

__all__ = [
    "KALSHI_VENUE",
    "KalshiDataClientConfig",
    "KalshiExecClientConfig",
    "KalshiLiveDataClientFactory",
    "KalshiLiveExecClientFactory",
    "KalshiInstrumentProvider",
    "parse_instrument_id",
]
```

**Step 2: Create minimal main.py**

A simple script that creates a TradingNode with the Kalshi adapter, loads instruments,
subscribes to quotes, and prints them. No strategy — just adapter verification.

**Step 3: Run full test suite**

```bash
pytest tests/ -v --timeout=120
```

**Step 4: Commit**

```bash
git add kalshi/__init__.py main.py pyproject.toml
git commit -m "feat(kalshi): finalize adapter package with entry point"
```

---

## Task 27: Review Task 26

**Spec Review — verify these specific items:**
- [ ] `kalshi/__init__.py` exports all public classes listed in the design doc
- [ ] `main.py` creates a `TradingNode` with `KalshiLiveDataClientFactory` and `KalshiLiveExecClientFactory`
- [ ] `pyproject.toml` has no new dependencies added (msgspec is an NT dependency, kalshi-python already present)
- [ ] All tests pass: `pytest tests/ -v`
- [ ] No files at top level that should be in `kalshi/` or `archive/`

---

## Task 28: Milestone — Adapter complete

**Present to user:**
- Complete adapter package with all components
- Full test suite: unit tests, integration tests, order combinatorics, reconciliation
- Entry point for manual verification
- Summary of what was built vs what was designed
- Open items: soak test (24h run, manual), production deployment

**This is the final milestone. Wait for user response.**
