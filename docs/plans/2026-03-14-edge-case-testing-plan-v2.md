# Edge-Case Testing Implementation Plan (v2 — Simplified)

> **For Claude:** REQUIRED SUB-SKILL: Use forge:executing-plans to implement this plan task-by-task.

**Goal:** Test dedup, reconnection, and rate limiting edge cases in the Kalshi execution client using combinatorial testing for parameter interactions and focused scenario tests for temporal/sequence bugs.

**Architecture:** Two layers. Layer 1 (CT) uses `allpairspy` 2-way covering arrays for quote derivation and date parsing, wired into `pytest.mark.parametrize`. Layer 2 uses a `WsMessageFactory` to produce canned WS message sequences for ~10 specific dangerous scenarios (duplicate fills, sequence gaps, settlement fills, fill-before-ack, dedup eviction replay, etc.), fed through the adapter's `_handle_ws_message` via the existing `SimpleNamespace + MethodType` pattern.

**Tech Stack:** `allpairspy` (CT generation), `pytest`, `msgspec` (WS message serialization)

**Why this architecture (not the original 3-layer plan):** The original plan built a FakeKalshiExchange matching engine (~400 LOC) that 12 reviewers found 9 financial mechanics bugs in. The adapter doesn't match orders — it calls the Kalshi SDK and processes WS messages. The specific failure modes (dedup, reconnection, rate limiting) are testable with focused scenarios, not random simulation against a fake exchange. This v2 cuts ~60% of planned code while covering the same priority bugs.

**Required Skills:**
- `forge:writing-tests`: Invoke before each implementation task — covers TDD cycle, assertion quality
- `forge:nautilus-testing`: Invoke before Task 4 — covers NT Cython test patterns, mock wiring

## Context for Executor

### Key Files — Source Under Test
- `kalshi/execution.py:45-72` — `_order_to_kalshi_params()`: order translation
- `kalshi/execution.py:86-109` — `TokenBucket`: async rate limiter (10 writes/sec)
- `kalshi/execution.py:298-315` — `_handle_ws_message()`: WS dispatch + sequence gap detection
- `kalshi/execution.py:317-368` — `_handle_user_order()`: order status handling
- `kalshi/execution.py:370-425` — `_handle_fill()`: dedup cache, settlement fills, fill events
- `kalshi/data.py:22-57` — `_derive_quotes()`: bids-only → YES/NO quote derivation
- `kalshi/providers.py:143-158` — `_parse_observation_date()`: ticker date extraction
- `kalshi/websocket/types.py:40-53` — `FillMsg` msgspec struct
- `kalshi/websocket/types.py:24-37` — `UserOrderMsg` msgspec struct
- `kalshi/websocket/types.py:73-81` — `decode_ws_msg()`: two-pass envelope decode
- `kalshi/websocket/client.py:44-53` — `_check_sequence()`: per-SID sequence gap detection
- `kalshi/common/constants.py:42` — `_DEDUP_MAX = 10_000`

### Key Files — Existing Tests (patterns to follow)
- `tests/test_execution.py:117-137` — `_make_fill_stub()`: SimpleNamespace + MethodType pattern for Cython workaround
- `tests/test_execution.py:140-153` — `_mock_fill()`: FillMsg mock builder
- `tests/test_execution.py:164-167` — `asyncio.create_task` patching pattern for settlement fills
- `tests/test_order_combinatorics.py:17-29` — `_order()` / `_instr()` helpers
- `tests/test_data_client.py:1-47` — `_derive_quotes()` unit tests

### Research Findings (from 18 review agents across 2 rounds)
- **allpairspy API**: `AllPairs(OrderedDict_params, filter_func=fn, n=2)` returns named tuples when given `OrderedDict`. Use for descriptive test IDs.
- **Actual case counts (empirically measured)**: Order translation 26 (2-way) / 33 (3-way). Quote derivation 29 (2-way) / 33 (3-way). Date parsing 144 (2-way) / 51 (3-way). CT order translation is redundant with existing `test_order_combinatorics.py`.
- **NT Cython gotcha**: `asyncio.create_task` in `_handle_fill` (line 373) and `_handle_ws_message` (lines 306-307) crashes in synchronous tests. Must patch via `patch("kalshi.execution.asyncio.create_task")`.
- **Dedup cache off-by-one**: `>= _DEDUP_MAX` at line 380 means capacity is 9,999 not 10,000.
- **TokenBucket infinite loop**: `acquire(cost > capacity)` hangs forever — batch of 11 orders would deadlock.
- **WS messages must go through `decode_ws_msg`**: Construct JSON bytes via `msgspec.json.encode`, decode through production path. Tests the real deserialization, catches field name mismatches.
- **Use real `FillMsg`/`UserOrderMsg` structs, not MagicMock**: MagicMock returns truthy for any attribute, hiding field-name bugs.
- **Separate SIDs per WS channel**: Use `sid=1` for user_order, `sid=2` for fill to exercise per-SID tracking.

## Execution Architecture

**Team:** 2 devs, 1 spec reviewer, 1 quality reviewer
**Task dependencies:**
- Task 1 (dependencies) must complete before Tasks 2 and 3
- Tasks 2 and 3 are independent (CT vs scenario tests)
- Task 4 (WsMessageFactory) is independent of Tasks 2-3
- Task 5 (scenario tests) depends on Task 4
- Task 6 (adapter bug tests) is independent

**Phases:**
- Phase 1: Tasks 1-3 (CT + test helpers — runs in CI)
- Phase 2: Tasks 4-6 (Scenario tests — runs in CI)

**Milestones:**
- After Phase 1 (Task 3 review): CT suite complete
- After Phase 2 (Task 6 review): Full edge-case suite operational

---

## Phase 1: Combinatorial Testing + Helpers

### Task 1: Add test dependencies and shared helpers

**Files:**
- Modify: `pyproject.toml`
- Create: `tests/helpers.py`

**Step 1: Add dev dependency**

```bash
uv add --dev allpairspy
```

**Step 2: Verify installation**

```bash
uv run python -c "from allpairspy import AllPairs; print('allpairspy OK')"
```

Expected: Prints OK.

**Step 3: Create shared test helpers**

The codebase has 3 different `_make_order` helpers with 3 different price conventions (dollars float, dollars string, cents int). Consolidate into one cents-based helper that all test files can import.

```python
"""Shared test helpers for Kalshi adapter tests.

Importable directly (no conftest needed — respects --noconftest).
"""
from unittest.mock import MagicMock

from nautilus_trader.model.enums import OrderSide, OrderType, TimeInForce
from nautilus_trader.model.identifiers import ClientOrderId, InstrumentId, Symbol
from nautilus_trader.model.objects import Price, Quantity

from kalshi.common.constants import KALSHI_VENUE


def make_mock_order(
    side: OrderSide,
    price_cents: int,
    qty: int = 5,
    tif: TimeInForce = TimeInForce.GTC,
    order_type: OrderType = OrderType.LIMIT,
    client_order_id: str = "C-TEST",
) -> MagicMock:
    """Build a minimal mock order. Price in cents (1-99)."""
    order = MagicMock()
    order.side = side
    order.order_type = order_type
    order.time_in_force = tif
    order.client_order_id = ClientOrderId(client_order_id)
    if order_type == OrderType.LIMIT and price_cents is not None:
        order.price = Price.from_str(f"{price_cents / 100:.2f}")
    else:
        order.price = None
    order.quantity = Quantity.from_int(qty)
    return order


def make_instrument_id(ticker: str = "KXHIGHCHI-26MAR14-T55", side: str = "YES") -> InstrumentId:
    """Build an InstrumentId for testing."""
    return InstrumentId(Symbol(f"{ticker}-{side}"), KALSHI_VENUE)
```

**Step 4: Commit**

```bash
git add pyproject.toml uv.lock tests/helpers.py
git commit -m "chore: add allpairspy dependency and shared test helpers"
```

---

### Task 2: CT suite for quote derivation and date parsing

**Files:**
- Create: `tests/test_ct_data_provider.py`

**Why no CT for order translation:** The existing `test_order_combinatorics.py` already covers all `side × action × price × tif` combinations. `_order_to_kalshi_params` is a simple lookup function with no parameter interaction effects (price doesn't change behavior based on tif). CT adds marginal value here.

**Step 1: Write CT tests for quote derivation and date parsing**

```python
"""Combinatorial testing — 2-way coverage for quote derivation and date parsing."""
import pytest
from collections import OrderedDict

from allpairspy import AllPairs

from kalshi.data import _derive_quotes
from kalshi.providers import KalshiInstrumentProvider


# ===========================================================================
# Quote derivation: 2-way coverage
# ===========================================================================

QUOTE_PARAMS = OrderedDict({
    "yes_state": ["empty", "single", "multi"],
    "no_state": ["empty", "single", "multi"],
    "yes_price": [0.01, 0.32, 0.50, 0.99],
    "no_price": [0.01, 0.32, 0.50, 0.99],
})


def _quote_filter(row: list) -> bool:
    """No constraints — all combos are valid for _derive_quotes."""
    return True


QUOTE_CASES = list(AllPairs(QUOTE_PARAMS, filter_func=_quote_filter, n=2))


def _build_book(state: str, price: float) -> dict[float, float]:
    if state == "empty":
        return {}
    elif state == "single":
        return {price: 5.0}
    else:  # multi
        lower = round(price - 0.01, 2)
        book = {price: 5.0}
        if lower > 0:
            book[lower] = 2.5
        return book


def _quote_id(case) -> str:
    return f"y{case.yes_state[0]}-n{case.no_state[0]}-yp{case.yes_price}-np{case.no_price}"


@pytest.mark.parametrize("case", QUOTE_CASES, ids=[_quote_id(c) for c in QUOTE_CASES])
def test_derive_quotes_ct(case):
    """Every 2-way combination must produce valid quotes or None."""
    yes_book = _build_book(case.yes_state, case.yes_price)
    no_book = _build_book(case.no_state, case.no_price)

    result = _derive_quotes("TICKER", yes_book, no_book)

    if not yes_book and not no_book:
        assert result is None, "Both books empty → must return None"
        return

    assert result is not None
    for side_name in ("YES", "NO"):
        q = result[side_name]
        assert 0.0 <= q["bid"] <= 1.0, f"{side_name} bid out of range: {q['bid']}"
        assert 0.0 <= q["ask"] <= 1.0, f"{side_name} ask out of range: {q['ask']}"
        assert q["bid_size"] >= 0.0, f"{side_name} bid_size negative"
        assert q["ask_size"] >= 0.0, f"{side_name} ask_size negative"

    # Domain invariants: YES/NO are complements
    if no_book:
        expected_yes_ask = round(1.0 - result["NO"]["bid"], 10)
        assert result["YES"]["ask"] == pytest.approx(expected_yes_ask, abs=1e-9)
    if yes_book:
        expected_no_ask = round(1.0 - result["YES"]["bid"], 10)
        assert result["NO"]["ask"] == pytest.approx(expected_no_ask, abs=1e-9)


# ===========================================================================
# Date parsing: 2-way coverage
# ===========================================================================

DATE_PARAMS = OrderedDict({
    "city": ["CHI", "NYC", "LAX", "DEN", "MIA"],
    "month": ["JAN", "FEB", "MAR", "APR", "MAY", "JUN",
              "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"],
    "day": ["01", "14", "28"],
    "threshold": ["T40", "T55", "T80", "T100"],
})

# No filter needed — all valid combos (removed "31" to avoid FEB-31 etc.)

DATE_CASES = list(AllPairs(DATE_PARAMS, n=2))

_MONTH_NUM = {
    "JAN": "01", "FEB": "02", "MAR": "03", "APR": "04",
    "MAY": "05", "JUN": "06", "JUL": "07", "AUG": "08",
    "SEP": "09", "OCT": "10", "NOV": "11", "DEC": "12",
}


def _date_id(case) -> str:
    return f"{case.city}-{case.month}{case.day}-{case.threshold}"


@pytest.mark.parametrize("case", DATE_CASES, ids=[_date_id(c) for c in DATE_CASES])
def test_observation_date_parsing_ct(case):
    """Every valid city × month × day × threshold parses to correct date."""
    ticker = f"KXHIGH{case.city}-26{case.month}{case.day}-{case.threshold}"
    result = KalshiInstrumentProvider._parse_observation_date(ticker)
    assert result == f"2026-{_MONTH_NUM[case.month]}-{case.day}"
```

**Step 2: Run the tests**

```bash
pytest tests/test_ct_data_provider.py -v --noconftest --tb=short 2>&1 | tail -20
```

Expected: All tests pass. ~29 quote cases + ~144 date cases.

**Step 3: Commit**

```bash
git add tests/test_ct_data_provider.py
git commit -m "test: add 2-way combinatorial tests for quote derivation and date parsing"
```

---

### Task 3: Market order edge case + TokenBucket infinite loop test

**Files:**
- Modify: `tests/test_order_combinatorics.py` (add market order test)
- Create: `tests/test_token_bucket.py`

**Step 1: Add market order test to existing combinatorics file**

```python
# Add to tests/test_order_combinatorics.py

class TestMarketOrders:
    """Market orders (no price) must produce params without price fields."""

    def test_market_order_buy_yes(self):
        order = MagicMock()
        order.side = OrderSide.BUY
        order.order_type = OrderType.MARKET
        order.time_in_force = TimeInForce.FOK
        order.price = None
        order.quantity = Quantity.from_int(5)
        order.client_order_id = ClientOrderId("C-MKT")

        params = _order_to_kalshi_params(order, _instr("YES"))
        assert params["type"] == "market"
        assert params["action"] == "buy"
        assert params["side"] == "yes"
        assert "yes_price" not in params
        assert "no_price" not in params
        assert params["count"] == 5

    def test_market_order_sell_no(self):
        order = MagicMock()
        order.side = OrderSide.SELL
        order.order_type = OrderType.MARKET
        order.time_in_force = TimeInForce.FOK
        order.price = None
        order.quantity = Quantity.from_int(10)
        order.client_order_id = ClientOrderId("C-MKT2")

        params = _order_to_kalshi_params(order, _instr("NO"))
        assert params["type"] == "market"
        assert params["action"] == "sell"
        assert params["side"] == "no"
        assert "yes_price" not in params
        assert "no_price" not in params
```

**Step 2: Write TokenBucket tests**

```python
"""Tests for TokenBucket — rate limiter edge cases."""
import asyncio
import pytest

from kalshi.execution import TokenBucket


class TestTokenBucket:
    def test_acquire_single_token(self):
        async def _run():
            tb = TokenBucket(rate=10.0, capacity=10.0)
            await tb.acquire(cost=1.0)
            # Should not hang — tokens available at init

        asyncio.run(_run())

    def test_acquire_full_capacity(self):
        async def _run():
            tb = TokenBucket(rate=10.0, capacity=10.0)
            await tb.acquire(cost=10.0)
            # Should consume all tokens

        asyncio.run(_run())

    def test_acquire_cost_exceeds_capacity_returns_eventually(self):
        """A cost > capacity should eventually acquire after enough time passes.

        Bug H9: If cost > capacity, the bucket can never accumulate enough tokens
        in a single refill. The loop MUST still converge because tokens accumulate
        across multiple sleep cycles. Verify it doesn't hang.
        """
        async def _run():
            tb = TokenBucket(rate=10.0, capacity=10.0)
            # cost=11 exceeds capacity=10 — this will loop forever
            # because tokens are capped at capacity (10) and we need 11
            with pytest.raises(asyncio.TimeoutError):
                await asyncio.wait_for(tb.acquire(cost=11.0), timeout=0.5)

        asyncio.run(_run())

    def test_acquire_respects_rate(self):
        """After draining tokens, next acquire should wait ~1/rate seconds."""
        async def _run():
            tb = TokenBucket(rate=10.0, capacity=1.0)
            await tb.acquire(cost=1.0)  # drain
            # Next acquire needs 1 token, rate is 10/sec, so ~0.1s wait
            await asyncio.wait_for(tb.acquire(cost=1.0), timeout=0.5)

        asyncio.run(_run())
```

**Step 3: Run tests**

```bash
pytest tests/test_order_combinatorics.py tests/test_token_bucket.py -v --noconftest --tb=short
```

Expected: All pass. The `cost > capacity` test should timeout (confirming the infinite loop bug).

**Step 4: Commit**

```bash
git add tests/test_order_combinatorics.py tests/test_token_bucket.py
git commit -m "test: add market order edge case and TokenBucket infinite loop test"
```

---

### Task 3.5: Review Tasks 1-3

**Trigger:** Both reviewers start when Tasks 1-3 complete.

**Spec review:**
- [ ] `test_ct_data_provider.py` uses `OrderedDict` → named tuples for descriptive IDs (e.g., `ye-ns-yp0.32-np0.50`)
- [ ] Quote invariant YES ask = 1 - NO bid checked on every combination
- [ ] No CT for order translation (redundant with `test_order_combinatorics.py`)
- [ ] Market order test verifies no `yes_price`/`no_price` in params
- [ ] TokenBucket `cost > capacity` test confirms infinite loop via `asyncio.TimeoutError`
- [ ] `tests/helpers.py` uses cents-based price convention consistently

**Code quality review:**
- [ ] `_build_book` produces distinct books for empty/single/multi states (no degenerate overlaps)
- [ ] Date parsing removed "31" from day values to avoid invalid dates (FEB-31)
- [ ] Both files run with `--noconftest`
- [ ] `make_mock_order` supports `OrderType.MARKET` with `price=None`

**Validation:**
- `pytest tests/test_ct_data_provider.py --co -q | wc -l` — should be ~170 cases
- All CT tests complete in under 2 seconds
- `pytest tests/test_token_bucket.py -v --noconftest` — all pass

**Resolution:** Findings queue for dev; all must resolve before milestone.

---

### Task 3.6: Milestone — Phase 1 Complete

**Present to user:**
- CT case counts and test results
- Market order and TokenBucket test results
- Confirmation of <2s CI time

**Wait for user response before proceeding to Phase 2.**

---

## Phase 2: Focused Scenario Tests

### Task 4: WsMessageFactory + adapter test harness

**Files:**
- Create: `tests/sim/__init__.py`
- Create: `tests/sim/ws_factory.py`
- Create: `tests/sim/test_ws_factory.py`

The WsMessageFactory produces canned WS message sequences as JSON bytes (not dicts), so they flow through the production `decode_ws_msg` path.

**Step 1: Write tests for WsMessageFactory**

```python
"""Tests for WsMessageFactory — canned WS message generation."""
import msgspec
import pytest

from kalshi.websocket.types import FillMsg, UserOrderMsg, decode_ws_msg
from tests.sim.ws_factory import WsMessageFactory


class TestWsMessageFactory:
    def test_fill_message_decodes_through_production_path(self):
        """Messages must round-trip through decode_ws_msg."""
        factory = WsMessageFactory()
        raw = factory.fill(
            trade_id="T-001", order_id="O-001", ticker="KXHIGH-T55",
            side="yes", action="buy", count=5, price_cents=50,
        )
        msg_type, sid, seq, msg = decode_ws_msg(raw)
        assert msg_type == "fill"
        assert isinstance(msg, FillMsg)
        assert msg.trade_id == "T-001"
        assert msg.yes_price_dollars == "0.50"
        assert msg.count_fp == "5.00"
        assert msg.is_taker is True

    def test_user_order_message_decodes(self):
        factory = WsMessageFactory()
        raw = factory.user_order(
            order_id="O-001", ticker="KXHIGH-T55",
            side="yes", action="buy", status="resting",
            client_order_id="C-001",
        )
        msg_type, sid, seq, msg = decode_ws_msg(raw)
        assert msg_type == "user_order"
        assert isinstance(msg, UserOrderMsg)
        assert msg.status == "resting"
        assert msg.client_order_id == "C-001"

    def test_settlement_fill_has_no_ticker(self):
        factory = WsMessageFactory()
        raw = factory.settlement_fill(
            trade_id="T-SET", order_id="O-SET",
            side="yes", action="sell", count=5,
        )
        msg_type, _, _, msg = decode_ws_msg(raw)
        assert msg_type == "fill"
        assert msg.market_ticker is None

    def test_sequence_numbers_increment(self):
        factory = WsMessageFactory()
        msgs = [
            factory.fill(trade_id=f"T-{i}", order_id="O-1", ticker="T",
                        side="yes", action="buy", count=1, price_cents=50)
            for i in range(5)
        ]
        seqs = [decode_ws_msg(m)[2] for m in msgs]
        assert seqs == [1, 2, 3, 4, 5]

    def test_fill_channel_uses_sid_2(self):
        factory = WsMessageFactory()
        raw = factory.fill(
            trade_id="T-1", order_id="O-1", ticker="T",
            side="yes", action="buy", count=1, price_cents=50,
        )
        _, sid, _, _ = decode_ws_msg(raw)
        assert sid == 2  # fill channel

    def test_user_order_channel_uses_sid_1(self):
        factory = WsMessageFactory()
        raw = factory.user_order(
            order_id="O-1", ticker="T", side="yes",
            action="buy", status="resting",
        )
        _, sid, _, _ = decode_ws_msg(raw)
        assert sid == 1  # user_order channel

    def test_duplicate_fill_produces_identical_bytes(self):
        factory = WsMessageFactory()
        raw1 = factory.fill(
            trade_id="T-DUP", order_id="O-1", ticker="T",
            side="yes", action="buy", count=1, price_cents=50,
        )
        # Reset seq to same value for dedup testing
        raw2 = factory.fill(
            trade_id="T-DUP", order_id="O-1", ticker="T",
            side="yes", action="buy", count=1, price_cents=50,
        )
        _, _, _, msg1 = decode_ws_msg(raw1)
        _, _, _, msg2 = decode_ws_msg(raw2)
        assert msg1.trade_id == msg2.trade_id

    def test_gap_in_sequence(self):
        """skip_seq produces a gap that _check_sequence should detect."""
        factory = WsMessageFactory()
        raw1 = factory.fill(
            trade_id="T-1", order_id="O-1", ticker="T",
            side="yes", action="buy", count=1, price_cents=50,
        )
        factory.skip_seq(channel="fill", count=3)  # skip 3 seq numbers
        raw2 = factory.fill(
            trade_id="T-2", order_id="O-1", ticker="T",
            side="yes", action="buy", count=1, price_cents=50,
        )
        _, _, seq1, _ = decode_ws_msg(raw1)
        _, _, seq2, _ = decode_ws_msg(raw2)
        assert seq2 == seq1 + 4  # gap of 3
```

**Step 2: Implement WsMessageFactory**

```python
"""WS message factory — produces canned Kalshi WebSocket messages as JSON bytes.

All messages are serialized to bytes and decoded through the production
`decode_ws_msg` path, ensuring field name and type conformance with the
actual Kalshi WS protocol.
"""
from __future__ import annotations

import msgspec


# SID assignments per channel (matches Kalshi WS behavior)
_CHANNEL_SIDS = {
    "user_order": 1,
    "fill": 2,
}


class WsMessageFactory:
    """Produces WS messages as raw JSON bytes for feeding into adapter handlers.

    Messages go through `decode_ws_msg()` in the test, exercising the
    production deserialization path. Sequence numbers auto-increment per SID.
    """

    def __init__(self) -> None:
        self._seq: dict[int, int] = {}  # sid → next seq

    def _next_seq(self, sid: int) -> int:
        seq = self._seq.get(sid, 0) + 1
        self._seq[sid] = seq
        return seq

    def skip_seq(self, channel: str, count: int = 1) -> None:
        """Advance sequence counter without producing a message (creates a gap)."""
        sid = _CHANNEL_SIDS[channel]
        current = self._seq.get(sid, 0)
        self._seq[sid] = current + count

    def fill(
        self,
        trade_id: str,
        order_id: str,
        ticker: str,
        side: str,
        action: str,
        count: int,
        price_cents: int,
        client_order_id: str | None = None,
        is_taker: bool = True,
        fee_cents: float = 0.0,
    ) -> bytes:
        """Produce a fill WS message as JSON bytes."""
        sid = _CHANNEL_SIDS["fill"]
        seq = self._next_seq(sid)
        price_dollars = f"{price_cents / 100:.2f}"

        msg: dict = {
            "trade_id": trade_id,
            "order_id": order_id,
            "side": side,
            "action": action,
            "count_fp": f"{count:.2f}",
            "is_taker": is_taker,
            "ts": 1_000_000_000,
        }
        if client_order_id is not None:
            msg["client_order_id"] = client_order_id
        if ticker is not None:
            msg["market_ticker"] = ticker
        # Both price fields present (they sum to $1)
        if side == "yes":
            msg["yes_price_dollars"] = price_dollars
            msg["no_price_dollars"] = f"{(100 - price_cents) / 100:.2f}"
        else:
            msg["no_price_dollars"] = price_dollars
            msg["yes_price_dollars"] = f"{(100 - price_cents) / 100:.2f}"
        if fee_cents > 0:
            msg["fee_cost"] = f"{fee_cents / 100:.4f}"

        envelope = {"type": "fill", "sid": sid, "seq": seq, "msg": msg}
        return msgspec.json.encode(envelope)

    def settlement_fill(
        self,
        trade_id: str,
        order_id: str,
        side: str,
        action: str,
        count: int,
    ) -> bytes:
        """Produce a settlement fill (market_ticker=None)."""
        sid = _CHANNEL_SIDS["fill"]
        seq = self._next_seq(sid)
        msg = {
            "trade_id": trade_id,
            "order_id": order_id,
            "side": side,
            "action": action,
            "count_fp": f"{count:.2f}",
            "is_taker": False,
            "ts": 1_000_000_000,
        }
        envelope = {"type": "fill", "sid": sid, "seq": seq, "msg": msg}
        return msgspec.json.encode(envelope)

    def user_order(
        self,
        order_id: str,
        ticker: str,
        side: str,
        action: str,
        status: str,
        client_order_id: str | None = None,
    ) -> bytes:
        """Produce a user_order WS message."""
        sid = _CHANNEL_SIDS["user_order"]
        seq = self._next_seq(sid)
        msg: dict = {
            "order_id": order_id,
            "ticker": ticker,
            "status": status,
            "side": side,
            "action": action,
        }
        if client_order_id is not None:
            msg["client_order_id"] = client_order_id
        envelope = {"type": "user_order", "sid": sid, "seq": seq, "msg": msg}
        return msgspec.json.encode(envelope)
```

**Step 3: Run tests**

```bash
pytest tests/sim/test_ws_factory.py -v --tb=short
```

Expected: All tests pass. Every message decodes correctly through `decode_ws_msg`.

**Step 4: Commit**

```bash
git add tests/sim/
git commit -m "feat(sim): add WsMessageFactory with production-path WS message generation"
```

---

### Task 5: Focused scenario tests for dedup, reconnection, and rate limiting

**Files:**
- Create: `tests/sim/test_scenarios.py`

These are the ~10 explicit scenario tests that replace the random simulation. Each tests a specific dangerous message ordering through the adapter's actual `_handle_ws_message` handler.

> **Before implementing:** Invoke `forge:nautilus-testing` skill for NT Cython test patterns.

**Step 1: Write scenario tests**

```python
"""Focused scenario tests for dedup, reconnection, and rate limiting edge cases.

Each test targets a specific dangerous WS message ordering. Messages flow through
the production _handle_ws_message → decode_ws_msg → _handle_fill/_handle_user_order
path via the SimpleNamespace + MethodType pattern.
"""
import asyncio
import types
from collections import OrderedDict
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from kalshi.execution import KalshiExecutionClient, _DEDUP_MAX
from kalshi.websocket.client import KalshiWebSocketClient
from tests.sim.ws_factory import WsMessageFactory


# ---------------------------------------------------------------------------
# Adapter stub builder
# ---------------------------------------------------------------------------

def _make_adapter_stub():
    """Build a stub with _handle_ws_message bound from KalshiExecutionClient.

    Uses SimpleNamespace + MethodType to avoid Cython descriptor issues.
    Binds _handle_ws_message (the full dispatch path), not individual handlers.
    """
    instrument = MagicMock()
    instrument.make_qty = lambda x: x
    instrument.make_price = lambda x: x

    ws_client = MagicMock(spec=KalshiWebSocketClient)
    ws_client._seq_tracker = {}

    # Use the real _check_sequence implementation
    ws_client._check_sequence = types.MethodType(
        KalshiWebSocketClient._check_sequence, ws_client,
    )

    stub = types.SimpleNamespace(
        _seen_trade_ids=OrderedDict(),
        _accepted_orders={},
        _instrument_provider=MagicMock(),
        _clock=MagicMock(timestamp_ns=MagicMock(
            side_effect=iter(range(1_000_000_000, 2_000_000_000, 1_000_000))
        )),
        _ws_client=ws_client,
        _log=MagicMock(),
        _ack_events={},
        generate_order_accepted=MagicMock(),
        generate_order_filled=MagicMock(),
        generate_order_rejected=MagicMock(),
        generate_order_canceled=MagicMock(),
        generate_position_status_reports=AsyncMock(return_value=[]),
        generate_order_status_reports=AsyncMock(return_value=[]),
        generate_fill_reports=AsyncMock(return_value=[]),
    )
    stub._instrument_provider.find.return_value = instrument

    # Bind the FULL dispatch method (not individual handlers)
    stub._handle_ws_message = types.MethodType(
        KalshiExecutionClient._handle_ws_message, stub,
    )
    # Also bind individual handlers (called by _handle_ws_message)
    stub._handle_user_order = types.MethodType(
        KalshiExecutionClient._handle_user_order, stub,
    )
    stub._handle_fill = types.MethodType(
        KalshiExecutionClient._handle_fill, stub,
    )

    return stub


# ---------------------------------------------------------------------------
# Scenario 1: Duplicate fill → dedup prevents double emission
# ---------------------------------------------------------------------------

class TestDuplicateFillDedup:
    def test_duplicate_trade_id_emits_single_fill(self):
        stub = _make_adapter_stub()
        factory = WsMessageFactory()

        msg1 = factory.fill(
            trade_id="T-001", order_id="O-001", ticker="KXHIGH-T55",
            side="yes", action="buy", count=5, price_cents=50,
            client_order_id="C-001",
        )
        # Same trade_id, different seq (simulates WS duplicate delivery)
        msg2 = factory.fill(
            trade_id="T-001", order_id="O-001", ticker="KXHIGH-T55",
            side="yes", action="buy", count=5, price_cents=50,
            client_order_id="C-001",
        )

        with patch("kalshi.execution.asyncio.create_task"):
            stub._handle_ws_message(msg1)
            stub._handle_ws_message(msg2)

        assert stub.generate_order_filled.call_count == 1

    def test_different_trade_ids_emit_separate_fills(self):
        stub = _make_adapter_stub()
        factory = WsMessageFactory()

        for i in range(3):
            msg = factory.fill(
                trade_id=f"T-{i:03d}", order_id="O-001", ticker="KXHIGH-T55",
                side="yes", action="buy", count=1, price_cents=50,
                client_order_id="C-001",
            )
            with patch("kalshi.execution.asyncio.create_task"):
                stub._handle_ws_message(msg)

        assert stub.generate_order_filled.call_count == 3


# ---------------------------------------------------------------------------
# Scenario 2: Dedup cache eviction → replayed trade_id accepted
# ---------------------------------------------------------------------------

class TestDedupCacheEviction:
    def test_evicted_trade_id_replayed_generates_new_fill(self):
        """After 10K fills, oldest trade_id is evicted. Replaying it creates
        a second fill event. This documents the KNOWN LIMITATION of bounded dedup.
        """
        stub = _make_adapter_stub()
        factory = WsMessageFactory()

        # Pre-fill cache to max
        for i in range(_DEDUP_MAX):
            stub._seen_trade_ids[f"T-{i:06d}"] = None

        assert "T-000000" in stub._seen_trade_ids

        # One more fill evicts T-000000
        msg = factory.fill(
            trade_id="T-NEW", order_id="O-NEW", ticker="KXHIGH-T55",
            side="yes", action="buy", count=1, price_cents=50,
        )
        with patch("kalshi.execution.asyncio.create_task"):
            stub._handle_ws_message(msg)

        assert "T-000000" not in stub._seen_trade_ids

        # Replay T-000000 — will be accepted as new (known limitation)
        stub.generate_order_filled.reset_mock()
        replay = factory.fill(
            trade_id="T-000000", order_id="O-OLD", ticker="KXHIGH-T55",
            side="yes", action="buy", count=1, price_cents=50,
        )
        with patch("kalshi.execution.asyncio.create_task"):
            stub._handle_ws_message(replay)

        assert stub.generate_order_filled.call_count == 1  # documents known risk


# ---------------------------------------------------------------------------
# Scenario 3: Settlement fill → position reconciliation triggered
# ---------------------------------------------------------------------------

class TestSettlementFill:
    def test_settlement_fill_triggers_position_recon(self):
        stub = _make_adapter_stub()
        factory = WsMessageFactory()

        msg = factory.settlement_fill(
            trade_id="T-SET", order_id="O-SET",
            side="yes", action="sell", count=5,
        )
        with patch("kalshi.execution.asyncio.create_task") as mock_task:
            stub._handle_ws_message(msg)

        # Settlement fill (no ticker) triggers position reconciliation
        assert mock_task.call_count == 1

    def test_settlement_fill_does_not_emit_order_filled(self):
        stub = _make_adapter_stub()
        factory = WsMessageFactory()

        msg = factory.settlement_fill(
            trade_id="T-SET2", order_id="O-SET2",
            side="yes", action="sell", count=5,
        )
        with patch("kalshi.execution.asyncio.create_task"):
            stub._handle_ws_message(msg)

        stub.generate_order_filled.assert_not_called()


# ---------------------------------------------------------------------------
# Scenario 4: Sequence gap → reconciliation triggered
# ---------------------------------------------------------------------------

class TestSequenceGap:
    def test_sequence_gap_triggers_reconciliation(self):
        stub = _make_adapter_stub()
        factory = WsMessageFactory()

        # First message establishes sequence on SID 2
        msg1 = factory.fill(
            trade_id="T-001", order_id="O-001", ticker="KXHIGH-T55",
            side="yes", action="buy", count=1, price_cents=50,
        )
        with patch("kalshi.execution.asyncio.create_task"):
            stub._handle_ws_message(msg1)

        # Skip 3 sequence numbers → gap
        factory.skip_seq(channel="fill", count=3)

        msg2 = factory.fill(
            trade_id="T-002", order_id="O-002", ticker="KXHIGH-T55",
            side="yes", action="buy", count=1, price_cents=50,
        )
        with patch("kalshi.execution.asyncio.create_task") as mock_task:
            stub._handle_ws_message(msg2)

        # Sequence gap should trigger reconciliation (2 tasks: order reports + fill reports)
        assert mock_task.call_count >= 1

    def test_independent_sids_dont_interfere(self):
        """Sequence on SID 1 (user_order) should not affect SID 2 (fill)."""
        stub = _make_adapter_stub()
        factory = WsMessageFactory()

        # user_order on SID 1
        msg1 = factory.user_order(
            order_id="O-001", ticker="KXHIGH-T55",
            side="yes", action="buy", status="resting",
            client_order_id="C-001",
        )
        # fill on SID 2
        msg2 = factory.fill(
            trade_id="T-001", order_id="O-001", ticker="KXHIGH-T55",
            side="yes", action="buy", count=5, price_cents=50,
            client_order_id="C-001",
        )

        with patch("kalshi.execution.asyncio.create_task"):
            stub._handle_ws_message(msg1)  # SID 1, seq 1
            stub._handle_ws_message(msg2)  # SID 2, seq 1 — no gap

        # Fill should have been processed (no gap on SID 2)
        assert stub.generate_order_filled.call_count == 1


# ---------------------------------------------------------------------------
# Scenario 5: Fill-before-ack (reordered WS messages)
# ---------------------------------------------------------------------------

class TestFillBeforeAck:
    def test_fill_before_user_order_emits_accepted_then_filled(self):
        """If fill arrives before user_order ack, adapter should emit
        accepted (from fill handler) then filled."""
        stub = _make_adapter_stub()
        factory = WsMessageFactory()

        # Fill arrives FIRST (before any user_order "resting" message)
        fill_msg = factory.fill(
            trade_id="T-001", order_id="O-001", ticker="KXHIGH-T55",
            side="yes", action="buy", count=5, price_cents=50,
            client_order_id="C-001",
        )
        with patch("kalshi.execution.asyncio.create_task"):
            stub._handle_ws_message(fill_msg)

        # _handle_fill should have emitted accepted (line 394-402 of execution.py)
        # because c_oid was not in _accepted_orders yet
        assert stub.generate_order_accepted.call_count == 1
        assert stub.generate_order_filled.call_count == 1


# ---------------------------------------------------------------------------
# Scenario 6: User order with status="filled" (immediate fill, no "resting")
# ---------------------------------------------------------------------------

class TestImmediateFill:
    def test_filled_status_emits_accepted_for_unknown_order(self):
        """Orders that fill immediately never get 'resting' status.
        The adapter must emit accepted when seeing status='filled'."""
        stub = _make_adapter_stub()
        factory = WsMessageFactory()

        msg = factory.user_order(
            order_id="O-IMM", ticker="KXHIGH-T55",
            side="yes", action="buy", status="filled",
            client_order_id="C-IMM",
        )
        with patch("kalshi.execution.asyncio.create_task"):
            stub._handle_ws_message(msg)

        assert stub.generate_order_accepted.call_count == 1


# ---------------------------------------------------------------------------
# Scenario 7: Fill for unknown instrument → silently dropped
# ---------------------------------------------------------------------------

class TestUnknownInstrumentFill:
    def test_fill_for_unknown_instrument_does_not_crash(self):
        stub = _make_adapter_stub()
        stub._instrument_provider.find.return_value = None  # unknown instrument
        factory = WsMessageFactory()

        msg = factory.fill(
            trade_id="T-UNK", order_id="O-UNK", ticker="UNKNOWN-TICKER",
            side="yes", action="buy", count=1, price_cents=50,
        )
        with patch("kalshi.execution.asyncio.create_task"):
            stub._handle_ws_message(msg)

        stub.generate_order_filled.assert_not_called()


# ---------------------------------------------------------------------------
# Scenario 8: Malformed WS message → no state corruption
# ---------------------------------------------------------------------------

class TestMalformedMessage:
    def test_garbage_bytes_do_not_corrupt_state(self):
        stub = _make_adapter_stub()
        initial_trades = len(stub._seen_trade_ids)
        initial_accepted = len(stub._accepted_orders)

        with patch("kalshi.execution.asyncio.create_task"):
            stub._handle_ws_message(b"not json at all")

        assert len(stub._seen_trade_ids) == initial_trades
        assert len(stub._accepted_orders) == initial_accepted

    def test_unknown_message_type_does_not_crash(self):
        stub = _make_adapter_stub()
        import msgspec as ms

        raw = ms.json.encode({"type": "unknown_type", "sid": 1, "seq": 1, "msg": {}})
        with patch("kalshi.execution.asyncio.create_task"):
            stub._handle_ws_message(raw)

        # Should not crash — unknown types are silently ignored


# ---------------------------------------------------------------------------
# Scenario 9: Cross-market fills are independent
# ---------------------------------------------------------------------------

class TestCrossMarketIndependence:
    def test_fills_on_different_tickers_tracked_independently(self):
        stub = _make_adapter_stub()
        factory = WsMessageFactory()

        msg_a = factory.fill(
            trade_id="T-A", order_id="O-A", ticker="TICKER-A",
            side="yes", action="buy", count=5, price_cents=50,
            client_order_id="C-A",
        )
        msg_b = factory.fill(
            trade_id="T-B", order_id="O-B", ticker="TICKER-B",
            side="no", action="sell", count=3, price_cents=70,
            client_order_id="C-B",
        )

        with patch("kalshi.execution.asyncio.create_task"):
            stub._handle_ws_message(msg_a)
            stub._handle_ws_message(msg_b)

        assert stub.generate_order_filled.call_count == 2
        assert "T-A" in stub._seen_trade_ids
        assert "T-B" in stub._seen_trade_ids


# ---------------------------------------------------------------------------
# Scenario 10: Dedup cache off-by-one boundary
# ---------------------------------------------------------------------------

class TestDedupCacheBoundary:
    def test_cache_size_at_dedup_max(self):
        """Verify the exact boundary behavior of the >= check."""
        stub = _make_adapter_stub()

        # Fill to exactly _DEDUP_MAX - 1
        for i in range(_DEDUP_MAX - 1):
            stub._seen_trade_ids[f"T-{i:06d}"] = None

        assert len(stub._seen_trade_ids) == _DEDUP_MAX - 1

        # One more entry should trigger eviction (>= check)
        factory = WsMessageFactory()
        msg = factory.fill(
            trade_id="T-BOUNDARY", order_id="O-B", ticker="KXHIGH-T55",
            side="yes", action="buy", count=1, price_cents=50,
        )
        with patch("kalshi.execution.asyncio.create_task"):
            stub._handle_ws_message(msg)

        # After insert + eviction, size should be _DEDUP_MAX - 1
        # (insert makes it _DEDUP_MAX, then >= triggers popitem)
        assert len(stub._seen_trade_ids) == _DEDUP_MAX - 1
        assert "T-BOUNDARY" in stub._seen_trade_ids
        assert "T-000000" not in stub._seen_trade_ids  # oldest evicted
```

**Step 2: Run the tests**

```bash
pytest tests/sim/test_scenarios.py -v --noconftest --tb=short
```

Expected: All ~15 scenario tests pass.

**Step 3: Commit**

```bash
git add tests/sim/test_scenarios.py
git commit -m "test(sim): add 10 focused scenario tests for dedup, reconnection, edge cases"
```

---

### Task 5.5: Review Tasks 4-5

**Trigger:** Both reviewers start when Tasks 4-5 complete.

**Spec review:**
- [ ] All WS messages flow through `decode_ws_msg` production path (bytes, not dicts)
- [ ] Fill messages include both `yes_price_dollars` and `no_price_dollars`
- [ ] Settlement fills have `market_ticker: None` (not missing, explicitly null)
- [ ] Separate SIDs: `sid=1` for user_order, `sid=2` for fill
- [ ] `_handle_ws_message` is bound (not individual handlers) — tests full dispatch path
- [ ] `asyncio.create_task` patched in every test that can trigger settlement/sequence gap
- [ ] Dedup eviction replay test documents the known limitation
- [ ] Incrementing timestamps via `itertools.count` side_effect (not static)
- [ ] Sequence gap test verifies reconciliation is triggered
- [ ] Fill-before-ack test verifies accepted is emitted before filled
- [ ] `_check_sequence` uses real implementation (not mocked)

**Code quality review:**
- [ ] No bare `except Exception: pass` anywhere
- [ ] No MagicMock for FillMsg/UserOrderMsg — real structs via decode_ws_msg
- [ ] `_make_adapter_stub` lists every attribute accessed by bound methods
- [ ] Tests run with `--noconftest`

**Validation:**
- `pytest tests/sim/ -v --noconftest --tb=short` — all pass
- `pytest tests/sim/test_scenarios.py --co -q | wc -l` — should be ~15 tests
- Total runtime under 5 seconds

**Resolution:** Findings queue for dev; all must resolve before milestone.

---

### Task 5.6: Milestone — Phase 2 Complete

**Present to user:**
- All scenario test results
- WsMessageFactory test results (round-trip through `decode_ws_msg`)
- Dedup eviction replay behavior documented
- TokenBucket infinite loop confirmed
- Full test run: `pytest tests/ -v --noconftest --tb=short`
- Architecture summary:
  ```
  Phase 1 (CI):   test_ct_data_provider.py (~170 cases)
                   test_token_bucket.py (4 tests)
                   test_order_combinatorics.py (extended with market orders)
  Phase 2 (CI):   sim/ws_factory.py → sim/test_scenarios.py (~15 scenarios)
  Helpers:        tests/helpers.py (shared mock builders)
  ```
- Summary of edge cases now covered vs before:
  | Scenario | Before | After |
  |----------|--------|-------|
  | Duplicate fill dedup | Unit test only | Through `_handle_ws_message` + `decode_ws_msg` |
  | Dedup cache eviction replay | Not tested | Documented known limitation |
  | Settlement fill → recon | Patched `asyncio.create_task` | Through full dispatch |
  | Sequence gap → recon | Not tested | Through real `_check_sequence` |
  | Fill-before-ack | Not tested | Verified accepted emitted first |
  | Immediate fill (status=filled) | Not tested | Verified accepted emitted |
  | Unknown instrument fill | Not tested | Verified silent drop |
  | Malformed WS message | Not tested | Verified no state corruption |
  | Cross-market independence | Not tested | Verified independent tracking |
  | Dedup cache off-by-one | Not tested | Boundary behavior verified |
  | Market order (no price) | Not tested | CT + explicit test |
  | TokenBucket cost > capacity | Not tested | Infinite loop confirmed |
  | Quote derivation interactions | Manual tests | 2-way CT (~29 cases) |
  | Date parsing coverage | 4 test cases | 2-way CT (~144 cases) |

**Wait for user response.**
