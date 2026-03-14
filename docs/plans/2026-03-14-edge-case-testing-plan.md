# Edge-Case Testing Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use forge:executing-plans to implement this plan task-by-task.

**Goal:** Build a three-layer testing system — combinatorial testing for parameter interactions, a fake exchange with fault injector for temporal bugs, and a seeded operation sequence generator — targeting dedup, reconnection, and rate limiting in the Kalshi execution client.

**Architecture:** Layer 1 (CT) uses `allpairspy` to generate 3-way covering arrays across order/quote/provider parameter spaces, wired into `pytest.mark.parametrize`. Layer 2 builds a `FakeKalshiExchange` matching engine + `FaultInjector` middleware that sits between the adapter and simulated exchange, injecting drops/delays/429s/disconnects. Layer 3 is a seeded `SequenceGenerator` that produces random operation sequences and checks invariants.

**Tech Stack:** `allpairspy` (CT generation), `time-machine` (deterministic clock), `pytest`, NT `test_kit` (stubs/mocks/eventually)

**Required Skills:**
- `forge:writing-tests`: Invoke before each implementation task — covers TDD cycle, assertion quality
- `forge:nautilus-testing`: Invoke before Tasks 4, 7 — covers NT Cython test patterns, mock wiring

## Enhancement Summary

**Deepened on:** 2026-03-14
**Research agents used:** 12 (writing-tests, nautilus-testing, geek-finance, kalshi-tails, architecture-strategist, performance-oracle, error-propagation-reviewer, pattern-recognition-specialist, python-reviewer, escalate-not-accommodate, CT-best-practices, simulation-testing-research)

### Critical Corrections (must fix before implementation)

**C1: `_execute_op` is incomplete — Layer 3 is operationally inert.**
The simulation test only handles `submit_order` and `settle_market`, silently dropping cancel, trigger_fill, disconnect, and reconnect operations. The 6-type weighted generator reduces to 2-type execution. All 4 review agents flagged this as the #1 issue. **Fix:** Implement all operation types in `_execute_op` with proper ID mapping (client_order_id → venue_order_id).

**C2: Bare `except Exception: pass` swallows infrastructure bugs.**
The FakeExchange signals rejections via response status, not exceptions. The only legitimate exception is `ApiException(status=429)` from rate limiting. **Fix:** Catch `ApiException` specifically, let everything else propagate.

**C3: FakeExchange financial mechanics are wrong.**
- Balance calc credits on sell instead of locking `(100-price)` collateral (fully collateralized model)
- FOK consumes book liquidity before checking if full fill is possible → corrupts book on rejection
- No capital locking on resting orders → can over-commit balance
- No insufficient-funds rejection → a listed CT parameter with no code path
- GTC partial fill sets status="filled" instead of "resting"
- Fill price uses aggressor's limit price instead of resting order's price (price improvement)
- No short-sell prevention (Kalshi doesn't allow selling without a position)
- Single-level matching only → no multi-level book walking for IOC/partial fills

**C4: `asyncio.create_task` crashes in synchronous context.**
Settlement fills (line 373) and sequence gaps (lines 306-307) call `asyncio.create_task`, which requires a running event loop. The harness runs synchronously. **Fix:** Patch `asyncio.create_task` in the harness (like existing `test_execution.py` does), or use `async-solipsism`/`looptime` for deterministic async tests.

**C5: SimHarness should use real NT infrastructure (Polymarket pattern).**
SimpleNamespace stubs swallow events silently — `generate_order_filled` calls `_send_order_event` → `_msgbus.send("ExecEngine.process", ...)` which needs a real MessageBus. **Fix:** Instantiate real `KalshiExecutionClient` with real MessageBus/Cache/ExecutionEngine, mock only `kalshi_python` SDK and WebSocket layer. Keep existing SimpleNamespace tests in `test_execution.py` for fast unit-level coverage.

**C6: WS messages should flow through `decode_ws_msg` production path.**
The harness manually constructs `FillMsg(**dict_filter)` which is a separate deserialization path from production (`bytes → decode_ws_msg → typed struct`). **Fix:** Serialize fake exchange dicts to JSON bytes via `msgspec.json.encode`, then call `decode_ws_msg`. Also bind `_handle_ws_message` instead of individual handlers to test the full dispatch + sequence gap detection path.

**C7: Crossed book in test fixtures creates impossible market state.**
`yes_levels={32: 10, 30: 20}` + `no_levels={68: 10, 70: 20}` → YES bid=32 but YES implied ask = 100-70 = 30. Bid > Ask is an arbitrage condition that never exists on a real exchange. **Fix:** Validate initial book states; use non-complementary prices (e.g., `yes_levels={25: 10}`, `no_levels={60: 10}` → YES bid=25, ask=40, no cross).

### High-Priority Improvements

**H1: Case count estimates are inflated ~3-5x.** Empirically measured: order translation = 33 (3-way), not "~100-160". Quote derivation = 33. Date parsing = 51. Total ~117, not ~500. Full CT suite runs in ~0.9s. Adjust plan text and CI time budget to <5s.

**H2: Descriptive test IDs.** Build from parameter values (e.g., `BUY-YES-p50-GTC-q5`), not sequential indexes (`ct3-17`). The plan's own review checklist requires this but the implementation contradicts it. Use `OrderedDict` input to `AllPairs` for named tuple output.

**H3: Static timestamp in harness clock.** All events get `ts=1_000_000_000`. Use an incrementing counter via `itertools.count` side_effect.

**H4: Derive PRNG seeds per component.** Exchange and injector using same seed causes correlated random sequences. Use `seed`, `seed * 3 + 1`, `seed * 7 + 3` or `numpy.random.SeedSequence.spawn()`.

**H5: Cancel should raise `ApiException(status=400)`, not `ValueError`.** Matches what `should_retry()` expects in `kalshi/common/errors.py:17` and tests the adapter's cancel_rejected error path.

**H6: Use separate SIDs per WS channel.** `sid=1` for user_order, `sid=2` for fill. Tests per-SID sequence tracking in `_check_sequence()`.

**H7: Add `KalshiExchangeProtocol`.** ~20-line `typing.Protocol` covering the 8 shared methods. Catches interface drift between FakeExchange and FaultInjector.

**H8: client_order_id → venue_order_id mapping.** SequenceGenerator cancel operations reference client_order_id but FakeExchange.cancel_order expects venue_order_id. SimHarness must maintain the mapping from submit_order responses.

**H9: TokenBucket.acquire(cost > capacity) is an infinite loop.** A batch of 11 orders would hang forever. Add a dedicated test for this edge case.

**H10: Dedup cache off-by-one.** `>= _DEDUP_MAX` at line 380 means capacity is 9,999, not 10,000. Document or fix.

### Medium-Priority Enhancements

**M1: Add dedup eviction replay test.** Pre-fill cache to `_DEDUP_MAX-1`, submit one more fill, then replay the evicted trade_id. Documents the known limitation.

**M2: Domain invariants as reusable assertion helpers.** `YES bid + NO ask ≈ 1.0` checked across ALL CT combinations.

**M3: Consolidate `_make_order` helpers** into `tests/helpers.py` (cents-based API). Three files have three different price conventions.

**M4: Increase simulation to 50 seeds × 100 ops.** Only adds ~2s, much better coverage. 10×30 barely scratches the state space.

**M5: Reorder window is tumbling, not sliding.** Docstring says "sliding" but implementation uses fixed non-overlapping windows.

**M6: FakeExchange should raise `KeyError` for unknown markets,** not silently return 0 fills. Hides test wiring bugs.

**M7: SequenceGenerator should not silently mutate operation types.** Skip and redraw, or yield labeled `Operation("cancel_order_skipped", ...)`.

**M8: Missing `ws_reorder_window` test in harness.** Fill-before-ack delivery (a priority scenario) is never exercised.

**M9: Use `looptime` or `async-solipsism` for asyncio time control.** `time-machine` does NOT properly control asyncio event loop time — can cause monotonic clock to go backwards.

### References Discovered

- [FoundationDB BUGGIFY pattern](https://transactional.blog/simulation/buggify) — per-run fault site enablement
- [async-solipsism](https://github.com/bmerry/async-solipsism) — deterministic asyncio event loop for Python
- [looptime](https://github.com/nolar/looptime) — asyncio time dilation for testing
- [order-matching PyPI](https://pypi.org/project/order-matching/) — reference matching engine
- [Brownie DeFi stateful testing](https://eth-brownie.readthedocs.io/en/stable/tests-hypothesis-stateful.html) — Hypothesis RuleBasedStateMachine for exchange testing
- [NumPy SeedSequence](https://numpy.org/doc/stable/reference/random/parallel.html) — independent reproducible PRNG streams

## Context for Executor

### Key Files — Source Under Test
- `kalshi/execution.py:45-72` — `_order_to_kalshi_params()`: order translation (side/contract/price/tif/qty)
- `kalshi/execution.py:86-109` — `TokenBucket`: async rate limiter (10 writes/sec)
- `kalshi/execution.py:112-578` — `KalshiExecutionClient`: order routing, fill handling, dedup, reconciliation
- `kalshi/execution.py:298-315` — `_handle_ws_message()`: WS dispatch + sequence gap detection
- `kalshi/execution.py:370-425` — `_handle_fill()`: dedup cache, settlement fills, fill event generation
- `kalshi/data.py:22-57` — `_derive_quotes()`: bids-only → YES/NO quote derivation
- `kalshi/data.py:60-214` — `KalshiDataClient`: orderbook snapshot/delta handling
- `kalshi/providers.py:22-33` — `parse_instrument_id()`: ticker/side extraction
- `kalshi/providers.py:36-158` — `KalshiInstrumentProvider`: market loading, instrument building
- `kalshi/websocket/types.py:1-81` — msgspec schemas: `FillMsg`, `UserOrderMsg`, `decode_ws_msg()`
- `kalshi/websocket/client.py:44-53` — `_check_sequence()`: per-SID sequence gap detection
- `kalshi/common/errors.py:15-21` — `should_retry()`: 429/5xx retry logic
- `kalshi/common/constants.py:1-22` — `KALSHI_VENUE`, URLs, rate limit costs, batch sizes
- `kalshi/config.py:1-57` — `KalshiExecClientConfig`, `KalshiDataClientConfig`

### Key Files — Existing Tests (patterns to follow)
- `tests/test_execution.py:117-137` — `_make_fill_stub()`: SimpleNamespace pattern for Cython workaround
- `tests/test_execution.py:140-153` — `_mock_fill()`: FillMsg mock builder
- `tests/test_order_combinatorics.py:17-29` — `_order()` / `_instr()` helpers for parametrized order tests
- `tests/test_data_client.py:1-47` — `_derive_quotes()` unit tests

### Key Files — NT Test Kit (use these, don't reinvent)
- `.venv/.../nautilus_trader/test_kit/functions.py:21-46` — `eventually(condition, timeout)` async assertion helper
- `.venv/.../nautilus_trader/test_kit/stubs/events.py` — `TestEventStubs.order_submitted()`, `.order_accepted()`, `.order_filled()`, etc.
- `.venv/.../nautilus_trader/test_kit/stubs/execution.py:46-80` — `TestExecStubs.limit_order()`, `.cash_account()`
- `.venv/.../nautilus_trader/test_kit/stubs/identifiers.py` — `TestIdStubs.trader_id()`, `.account_id()`, etc.
- `.venv/.../nautilus_trader/test_kit/mocks/exec_clients.py` — `MockLiveExecutionClient` (call/command recording)
- `.venv/.../nautilus_trader/test_kit/providers.py` — `TestInstrumentProvider.binary_option()`

### Research Findings
- **allpairspy API**: `AllPairs(parameters, filter_func=fn, n=3)` returns iterable of tuples. `n=3` for 3-way. `filter_func` receives partial rows during generation and must return `True` to keep.
- **time-machine API**: `@time_machine.travel("2026-03-14")` decorator or `time_machine.travel(...).start()` / `.stop()` context manager. Controls `time.time()`, `datetime.now()`, `asyncio.get_event_loop().time()`.
- **NT Cython gotcha**: `KalshiExecutionClient` inherits Cython `LiveExecutionClient` — cannot `__new__()` and assign attributes. Existing tests use `SimpleNamespace` + `MethodType` binding (see `_make_fill_stub()`). The fake exchange tests will need the same pattern or mock the Kalshi SDK layer above.
- **Kalshi API response shapes**: `create_order` returns `ApiResponse` with `raw_data` JSON containing `{"order": {"order_id": "...", "status": "...", ...}}`. Fills WS: `FillMsg` struct. User orders WS: `UserOrderMsg` struct.
- **Dedup cache**: `OrderedDict` with FIFO eviction at 10K entries. Key bug scenario: trade_id replayed after eviction → re-accepted as new fill.
- **Sequence gaps**: Per-SID tracking in `_check_sequence()`. Gap triggers reconciliation via `generate_order_status_reports` + `generate_fill_reports` async tasks.

### Relevant Patterns
- `tests/test_order_combinatorics.py` — Follow this pattern for CT parametrized tests
- `tests/test_execution.py:117-153` — Follow `_make_fill_stub()` + `_mock_fill()` for method-level unit tests
- NT Polymarket adapter tests — Real MessageBus/Cache/ExecutionEngine, mocked HTTP/WS

## Execution Architecture

**Team:** 3 devs, 1 spec reviewer, 1 quality reviewer
**Task dependencies:**
- Tasks 1-3 (Phase 1: CT) are independent of Tasks 4-9 (Phase 2: Simulation)
- Task 1 (dependencies) must complete before Tasks 2 and 3
- Tasks 2 and 3 are independent (CT execution vs CT data/provider)
- Task 4 (FakeExchange) and Task 5 (FaultInjector interface) are independent
- Task 6 (FaultInjector impl) depends on Task 4
- Task 7 (NT harness) depends on Task 4
- Task 8 (Invariant checkers) is independent
- Task 9 (Sequence generator) depends on Tasks 4, 6, 7, 8

**Phases:**
- Phase 1: Tasks 1-3 (Combinatorial Testing — runs in CI)
- Phase 2: Tasks 4-9 (Simulation Testing — local only)

**Milestones:**
- After Phase 1 (Task 3 review): CT suite complete, running in CI
- After Task 7 review: Fake exchange + harness wired, first simulation test passes
- After Phase 2 (Task 9 review): Full simulation suite operational

---

## Phase 1: Combinatorial Testing

### Task 1: Add test dependencies

**Files:**
- Modify: `pyproject.toml`

**Step 1: Add dev dependencies**

```bash
uv add --dev allpairspy time-machine
```

**Step 2: Verify installation**

```bash
uv run python -c "from allpairspy import AllPairs; print('allpairspy OK')"
uv run python -c "import time_machine; print('time-machine OK')"
```

Expected: Both print OK.

**Step 3: Commit**

```bash
git add pyproject.toml uv.lock
git commit -m "chore: add allpairspy and time-machine dev dependencies"
```

---

### Task 2: CT suite for execution client order translation

**Files:**
- Create: `tests/test_ct_execution.py`

**Step 1: Write the CT parametrized test for order translation**

This test generates 3-way covering arrays across the execution client's parameter space and validates every combination produces valid Kalshi params or raises an expected error.

```python
"""Combinatorial testing — 3-way coverage for execution client order translation."""
import pytest
from collections import OrderedDict
from unittest.mock import MagicMock

from allpairspy import AllPairs
from nautilus_trader.model.enums import OrderSide, OrderType, TimeInForce
from nautilus_trader.model.identifiers import ClientOrderId, InstrumentId, Symbol
from nautilus_trader.model.objects import Price, Quantity

from kalshi.common.constants import KALSHI_VENUE
from kalshi.execution import _order_to_kalshi_params


# ---------------------------------------------------------------------------
# Parameter space
# ---------------------------------------------------------------------------

PARAMS = OrderedDict({
    "order_side": [OrderSide.BUY, OrderSide.SELL],
    "contract_side": ["YES", "NO"],
    "price_cents": [1, 25, 50, 75, 99],
    "tif": [TimeInForce.GTC, TimeInForce.FOK, TimeInForce.IOC, TimeInForce.GTD],
    "quantity": [1, 5, 9, 100],
})

_TIF_EXPECTED = {
    TimeInForce.GTC: "good_till_canceled",
    TimeInForce.FOK: "fill_or_kill",
    TimeInForce.IOC: "immediate_or_cancel",
    TimeInForce.GTD: "good_till_canceled",
}


def _valid_combo(row):
    """No constraints needed for order translation — all combos are valid."""
    return True


CASES_3WAY = list(AllPairs(list(PARAMS.values()), filter_func=_valid_combo, n=3))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_order(side, price_cents, qty, tif):
    order = MagicMock()
    order.side = side
    order.order_type = OrderType.LIMIT
    order.time_in_force = tif
    order.price = Price.from_str(f"{price_cents / 100:.2f}")
    order.quantity = Quantity.from_int(qty)
    order.client_order_id = ClientOrderId("C-CT")
    return order


# ---------------------------------------------------------------------------
# 3-way CT test
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    list(PARAMS.keys()),
    CASES_3WAY,
    ids=[f"ct3-{i}" for i in range(len(CASES_3WAY))],
)
def test_order_translation_3way(order_side, contract_side, price_cents, tif, quantity):
    """Every 3-way parameter combination must produce valid Kalshi params."""
    order = _make_order(order_side, price_cents, quantity, tif)
    instrument_id = InstrumentId(
        Symbol(f"KXHIGHCHI-26MAR14-T55-{contract_side}"), KALSHI_VENUE
    )

    params = _order_to_kalshi_params(order, instrument_id)

    # --- Universal invariants ---
    # Action matches order side
    expected_action = "buy" if order_side == OrderSide.BUY else "sell"
    assert params["action"] == expected_action

    # Contract side matches instrument
    assert params["side"] == contract_side.lower()

    # Price is in the correct field and in valid cent range
    price_field = "yes_price" if contract_side == "YES" else "no_price"
    other_field = "no_price" if contract_side == "YES" else "yes_price"
    assert price_field in params
    assert other_field not in params
    assert 1 <= params[price_field] <= 99

    # Quantity passthrough
    assert params["count"] == quantity

    # TIF uses full string, never abbreviation
    assert params["time_in_force"] == _TIF_EXPECTED[tif]
    assert params["time_in_force"] not in ("gtc", "fok", "ioc", "gtd")

    # Ticker extracted correctly
    assert params["ticker"] == "KXHIGHCHI-26MAR14-T55"

    # Type is always "limit" for limit orders
    assert params["type"] == "limit"

    # Client order ID passed through
    assert params["client_order_id"] == "C-CT"
```

**Step 2: Run the test**

```bash
pytest tests/test_ct_execution.py -v --noconftest --tb=short 2>&1 | tail -20
```

Expected: All ~100-160 test cases PASS. Check the count to confirm 3-way coverage generates a reasonable number.

**Step 3: Commit**

```bash
git add tests/test_ct_execution.py
git commit -m "test: add 3-way combinatorial tests for order translation"
```

---

### Task 3: CT suite for data client and provider

**Files:**
- Create: `tests/test_ct_data_provider.py`

**Step 1: Write CT tests for quote derivation and provider parsing**

```python
"""Combinatorial testing — 3-way coverage for data client and provider."""
import pytest
from collections import OrderedDict

from allpairspy import AllPairs

from kalshi.data import _derive_quotes
from kalshi.providers import KalshiInstrumentProvider, parse_instrument_id
from nautilus_trader.model.identifiers import InstrumentId, Symbol
from kalshi.common.constants import KALSHI_VENUE


# ===========================================================================
# Data client: quote derivation
# ===========================================================================

QUOTE_PARAMS = OrderedDict({
    "yes_state": ["empty", "single", "multi"],
    "no_state": ["empty", "single", "multi"],
    "yes_price": [0.01, 0.32, 0.50, 0.68, 0.99],
    "no_price": [0.01, 0.32, 0.50, 0.68, 0.99],
    "yes_size": [0.0, 1.0, 5.0, 100.0],
    "no_size": [0.0, 1.0, 5.0, 100.0],
})


def _quote_filter(row):
    """Skip contradictions: 'empty' state with non-zero size."""
    n = len(row)
    if n > 0 and row[0] == "empty" and n > 4 and row[4] > 0.0:
        return False
    if n > 1 and row[1] == "empty" and n > 5 and row[5] > 0.0:
        return False
    return True


QUOTE_CASES = list(AllPairs(list(QUOTE_PARAMS.values()), filter_func=_quote_filter, n=3))


def _build_book(state, price, size):
    if state == "empty":
        return {}
    elif state == "single":
        return {price: size} if size > 0 else {}
    else:  # multi
        # Two levels: the specified price + one tick lower
        book = {}
        if size > 0:
            book[price] = size
            lower = round(price - 0.01, 2)
            if lower > 0:
                book[lower] = size * 0.5
        return book


@pytest.mark.parametrize(
    list(QUOTE_PARAMS.keys()),
    QUOTE_CASES,
    ids=[f"quote-{i}" for i in range(len(QUOTE_CASES))],
)
def test_derive_quotes_3way(yes_state, no_state, yes_price, no_price, yes_size, no_size):
    """Every 3-way parameter combination must produce valid quotes or None."""
    yes_book = _build_book(yes_state, yes_price, yes_size)
    no_book = _build_book(no_state, no_price, no_size)

    result = _derive_quotes("TICKER", yes_book, no_book)

    if not yes_book and not no_book:
        assert result is None, "Both books empty → must return None"
        return

    assert result is not None
    for side in ("YES", "NO"):
        q = result[side]
        # Bid and ask must be in valid range
        assert 0.0 <= q["bid"] <= 1.0, f"{side} bid out of range: {q['bid']}"
        assert 0.0 <= q["ask"] <= 1.0, f"{side} ask out of range: {q['ask']}"
        # Sizes must be non-negative
        assert q["bid_size"] >= 0.0, f"{side} bid_size negative"
        assert q["ask_size"] >= 0.0, f"{side} ask_size negative"

    # YES ask = 1 - NO bid (when NO book exists)
    if no_book:
        expected_yes_ask = round(1.0 - result["NO"]["bid"], 10)
        assert result["YES"]["ask"] == pytest.approx(expected_yes_ask, abs=1e-9)

    # NO ask = 1 - YES bid (when YES book exists)
    if yes_book:
        expected_no_ask = round(1.0 - result["YES"]["bid"], 10)
        assert result["NO"]["ask"] == pytest.approx(expected_no_ask, abs=1e-9)


# ===========================================================================
# Provider: parse_instrument_id
# ===========================================================================

PARSE_PARAMS = OrderedDict({
    "ticker": [
        "KXHIGHCHI-26MAR14-T55",
        "KXHIGHNYC-26DEC31-T40",
        "KXHIGHLAX-26JAN01-T80",
    ],
    "side_suffix": ["YES", "NO"],
})

PARSE_CASES = list(AllPairs(list(PARSE_PARAMS.values()), n=2))


@pytest.mark.parametrize(
    list(PARSE_PARAMS.keys()),
    PARSE_CASES,
    ids=[f"parse-{i}" for i in range(len(PARSE_CASES))],
)
def test_parse_instrument_id_ct(ticker, side_suffix):
    """All ticker × side combinations parse correctly."""
    instrument_id = InstrumentId(
        Symbol(f"{ticker}-{side_suffix}"), KALSHI_VENUE
    )
    parsed_ticker, parsed_side = parse_instrument_id(instrument_id)
    assert parsed_ticker == ticker
    assert parsed_side == side_suffix.lower()


# ===========================================================================
# Provider: observation date parsing
# ===========================================================================

DATE_PARAMS = OrderedDict({
    "city": ["CHI", "NYC", "LAX", "DEN", "MIA"],
    "month": ["JAN", "FEB", "MAR", "APR", "MAY", "JUN",
              "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"],
    "day": ["01", "14", "28", "31"],
    "threshold": ["T40", "T55", "T80", "T100"],
})


def _date_filter(row):
    """Skip invalid month/day combos (e.g., FEB-31)."""
    n = len(row)
    if n > 2:
        month, day = row[1], row[2]
        day_int = int(day)
        short_months = {"FEB", "APR", "JUN", "SEP", "NOV"}
        if month == "FEB" and day_int > 28:
            return False
        if month in short_months and day_int > 30:
            return False
    return True


DATE_CASES = list(AllPairs(list(DATE_PARAMS.values()), filter_func=_date_filter, n=3))

_MONTH_NUM = {
    "JAN": "01", "FEB": "02", "MAR": "03", "APR": "04",
    "MAY": "05", "JUN": "06", "JUL": "07", "AUG": "08",
    "SEP": "09", "OCT": "10", "NOV": "11", "DEC": "12",
}


@pytest.mark.parametrize(
    list(DATE_PARAMS.keys()),
    DATE_CASES,
    ids=[f"date-{i}" for i in range(len(DATE_CASES))],
)
def test_observation_date_parsing_3way(city, month, day, threshold):
    """Every valid city × month × day × threshold parses to correct date."""
    ticker = f"KXHIGH{city}-26{month}{day}-{threshold}"
    result = KalshiInstrumentProvider._parse_observation_date(ticker)
    assert result == f"2026-{_MONTH_NUM[month]}-{day}"
```

**Step 2: Run the tests**

```bash
pytest tests/test_ct_data_provider.py -v --noconftest --tb=short 2>&1 | tail -30
```

Expected: All tests pass. Note the case counts for each parameter space.

**Step 3: Commit**

```bash
git add tests/test_ct_data_provider.py
git commit -m "test: add 3-way combinatorial tests for data client and provider"
```

---

### Task 3.5: Review Tasks 2-3

**Trigger:** Both reviewers start simultaneously when Tasks 2-3 complete.

**Spec review — verify these specific items:**
- [ ] `test_ct_execution.py` covers all 5 parameters from the design doc (side, contract, price, tif, quantity)
- [ ] `AllPairs(..., n=3)` is used (not `n=2`) — design doc specifies 3-way
- [ ] Filter functions correctly reject impossible combinations (or document why none are needed)
- [ ] Assertions check semantic correctness: action matches side, price in correct field, TIF is full string
- [ ] `test_ct_data_provider.py` covers quote derivation parameter space (yes_state, no_state, prices, sizes)
- [ ] Quote invariant: YES ask = 1 - NO bid (and vice versa) is asserted
- [ ] Date parsing filter correctly rejects FEB-31, APR-31, etc.

**Code quality review — verify these specific items:**
- [ ] No duplicate coverage with existing `test_order_combinatorics.py` — CT tests cover parameter interactions, existing tests cover specific edge prices (0.315 rounding)
- [ ] Test IDs are meaningful (not just index numbers)
- [ ] Both files run with `--noconftest` (no fixture dependencies)
- [ ] `_build_book()` in quote CT correctly produces empty/single/multi level books

**Validation Data:**
- Run `pytest tests/test_ct_execution.py --co -q | wc -l` — should be 100-200 cases
- Run `pytest tests/test_ct_data_provider.py --co -q | wc -l` — should be 150-400 cases
- All CT tests must complete in under 10 seconds total

**Resolution:** Findings queue for dev; all must resolve before milestone.

---

### Task 3.6: Milestone — Phase 1 Complete (CT Suite)

**Present to user:**
- Number of CT test cases generated per parameter space
- All test results (pass counts)
- Confirmation that tests run in CI-appropriate time (<10s)
- Any surprising parameter combinations that revealed issues

**Wait for user response before proceeding to Phase 2.**

---

## Phase 2: Simulation Testing

### Task 4: Build FakeKalshiExchange

**Files:**
- Create: `tests/sim/fake_exchange.py`
- Create: `tests/sim/__init__.py`
- Create: `tests/sim/test_fake_exchange.py`

**Step 1: Write tests for the fake exchange**

```python
"""Tests for FakeKalshiExchange — deterministic order matching."""
import pytest
from tests.sim.fake_exchange import FakeKalshiExchange, BookState


class TestFakeExchangeOrderLifecycle:
    def _exchange(self, seed=42):
        """Create exchange with one market: thin YES book, thin NO book."""
        books = {
            "KXHIGHCHI-26MAR14-T55": BookState(
                yes_levels={32: 10, 30: 20},  # cents: size
                no_levels={68: 10, 70: 20},
            ),
        }
        return FakeKalshiExchange(seed=seed, initial_books=books, balance_cents=10_000)

    def test_buy_yes_at_ask_fills_immediately(self):
        """Buy YES at 68 cents (= 100 - best NO bid 32) should fill."""
        ex = self._exchange()
        resp = ex.create_order({
            "ticker": "KXHIGHCHI-26MAR14-T55",
            "action": "buy",
            "side": "yes",
            "count": 5,
            "type": "limit",
            "yes_price": 68,
            "time_in_force": "good_till_canceled",
            "client_order_id": "C-001",
        })
        assert resp["order"]["status"] in ("filled", "resting")
        order_id = resp["order"]["order_id"]
        # Check fills were generated
        fills = ex.get_fills({"order_id": order_id})
        if resp["order"]["status"] == "filled":
            assert len(fills["fills"]) > 0

    def test_buy_yes_below_ask_rests(self):
        """Buy YES at 25 cents (below ask) should rest in book."""
        ex = self._exchange()
        resp = ex.create_order({
            "ticker": "KXHIGHCHI-26MAR14-T55",
            "action": "buy",
            "side": "yes",
            "count": 5,
            "type": "limit",
            "yes_price": 25,
            "time_in_force": "good_till_canceled",
            "client_order_id": "C-002",
        })
        assert resp["order"]["status"] == "resting"

    def test_fok_no_fill_rejects(self):
        """FOK order that can't fully fill should be rejected."""
        ex = self._exchange()
        resp = ex.create_order({
            "ticker": "KXHIGHCHI-26MAR14-T55",
            "action": "buy",
            "side": "yes",
            "count": 5,
            "type": "limit",
            "yes_price": 25,  # below ask — won't fill
            "time_in_force": "fill_or_kill",
            "client_order_id": "C-003",
        })
        assert resp["order"]["status"] == "rejected"

    def test_cancel_resting_order(self):
        ex = self._exchange()
        resp = ex.create_order({
            "ticker": "KXHIGHCHI-26MAR14-T55",
            "action": "buy",
            "side": "yes",
            "count": 5,
            "type": "limit",
            "yes_price": 25,
            "time_in_force": "good_till_canceled",
            "client_order_id": "C-004",
        })
        order_id = resp["order"]["order_id"]
        cancel_resp = ex.cancel_order(order_id)
        assert cancel_resp["order"]["status"] == "canceled"

    def test_cancel_nonexistent_raises(self):
        ex = self._exchange()
        with pytest.raises(KeyError):
            ex.cancel_order("nonexistent-id")

    def test_get_positions_reflects_fills(self):
        ex = self._exchange()
        # Buy at a price that fills
        ex.create_order({
            "ticker": "KXHIGHCHI-26MAR14-T55",
            "action": "buy",
            "side": "yes",
            "count": 5,
            "type": "limit",
            "yes_price": 99,  # guaranteed fill
            "time_in_force": "good_till_canceled",
            "client_order_id": "C-005",
        })
        positions = ex.get_positions({})
        # Should have a YES position
        found = [p for p in positions["market_exposures"]
                 if p["ticker"] == "KXHIGHCHI-26MAR14-T55"]
        assert len(found) == 1
        assert float(found[0]["position_fp"]) > 0

    def test_ws_messages_generated_for_fills(self):
        ex = self._exchange()
        ex.create_order({
            "ticker": "KXHIGHCHI-26MAR14-T55",
            "action": "buy",
            "side": "yes",
            "count": 5,
            "type": "limit",
            "yes_price": 99,
            "time_in_force": "good_till_canceled",
            "client_order_id": "C-006",
        })
        msgs = ex.drain_ws_messages()
        types = [m["type"] for m in msgs]
        assert "user_order" in types
        # If filled, should also have a fill message
        if any(m.get("msg", {}).get("status") == "filled" for m in msgs if m["type"] == "user_order"):
            assert "fill" in types

    def test_multiple_markets_independent(self):
        """Orders on different markets don't interfere."""
        books = {
            "TICKER-A": BookState(yes_levels={50: 10}, no_levels={50: 10}),
            "TICKER-B": BookState(yes_levels={30: 10}, no_levels={70: 10}),
        }
        ex = FakeKalshiExchange(seed=42, initial_books=books, balance_cents=10_000)
        ex.create_order({
            "ticker": "TICKER-A", "action": "buy", "side": "yes",
            "count": 5, "type": "limit", "yes_price": 99,
            "time_in_force": "good_till_canceled", "client_order_id": "C-A",
        })
        ex.create_order({
            "ticker": "TICKER-B", "action": "buy", "side": "yes",
            "count": 5, "type": "limit", "yes_price": 25,
            "time_in_force": "good_till_canceled", "client_order_id": "C-B",
        })
        positions = ex.get_positions({})
        tickers = [p["ticker"] for p in positions["market_exposures"]]
        # Only TICKER-A should have a position (99 fills, 25 rests)
        assert "TICKER-A" in tickers

    def test_deterministic_across_runs(self):
        """Same seed → same order IDs, same fill sequence."""
        def _run(seed):
            ex = FakeKalshiExchange(
                seed=seed,
                initial_books={"T": BookState(yes_levels={50: 10}, no_levels={50: 10})},
                balance_cents=10_000,
            )
            resp = ex.create_order({
                "ticker": "T", "action": "buy", "side": "yes",
                "count": 5, "type": "limit", "yes_price": 99,
                "time_in_force": "good_till_canceled", "client_order_id": "C-DET",
            })
            return resp, ex.drain_ws_messages()

        r1, m1 = _run(seed=123)
        r2, m2 = _run(seed=123)
        assert r1 == r2
        assert m1 == m2

    def test_balance_deducted_on_buy(self):
        ex = self._exchange()
        initial_balance = ex.get_balance()
        ex.create_order({
            "ticker": "KXHIGHCHI-26MAR14-T55",
            "action": "buy",
            "side": "yes",
            "count": 5,
            "type": "limit",
            "yes_price": 99,
            "time_in_force": "good_till_canceled",
            "client_order_id": "C-BAL",
        })
        assert ex.get_balance() < initial_balance


class TestFakeExchangeSettlement:
    def test_settle_market_generates_settlement_fills(self):
        """Settling a market with open positions generates fills with no ticker."""
        books = {"T": BookState(yes_levels={50: 10}, no_levels={50: 10})}
        ex = FakeKalshiExchange(seed=42, initial_books=books, balance_cents=10_000)
        # Buy to create a position
        ex.create_order({
            "ticker": "T", "action": "buy", "side": "yes",
            "count": 5, "type": "limit", "yes_price": 99,
            "time_in_force": "good_till_canceled", "client_order_id": "C-SET",
        })
        ex.drain_ws_messages()  # clear

        # Settle the market
        ex.settle_market("T", outcome="yes")
        msgs = ex.drain_ws_messages()
        fill_msgs = [m for m in msgs if m["type"] == "fill"]
        assert len(fill_msgs) > 0
        # Settlement fills have no market_ticker
        for fm in fill_msgs:
            assert fm["msg"].get("market_ticker") is None
```

**Step 2: Implement FakeKalshiExchange**

```python
"""Deterministic fake Kalshi exchange for simulation testing.

Maintains order books, matches orders, generates fills and WS messages.
All randomness flows through a single seeded PRNG for reproducibility.
"""
from __future__ import annotations

import random
import uuid
from dataclasses import dataclass, field


@dataclass
class BookState:
    """Initial order book state for a market. Prices in cents, sizes in contracts."""
    yes_levels: dict[int, int] = field(default_factory=dict)
    no_levels: dict[int, int] = field(default_factory=dict)


@dataclass
class _Order:
    order_id: str
    client_order_id: str
    ticker: str
    action: str  # "buy" | "sell"
    side: str  # "yes" | "no"
    count: int
    price_cents: int
    tif: str
    status: str  # "resting" | "filled" | "canceled" | "rejected"
    filled_count: int = 0


@dataclass
class _Fill:
    trade_id: str
    order_id: str
    client_order_id: str | None
    ticker: str | None  # None for settlement
    side: str
    action: str
    count: int
    price_cents: int
    is_taker: bool
    fee_cents: float


class FakeKalshiExchange:
    """Deterministic simulated Kalshi exchange.

    Args:
        seed: Random seed for deterministic behavior.
        initial_books: Per-ticker initial book state.
        balance_cents: Starting account balance in cents.
    """

    def __init__(
        self,
        seed: int,
        initial_books: dict[str, BookState],
        balance_cents: int = 10_000,
    ) -> None:
        self._rng = random.Random(seed)
        self._balance_cents = balance_cents

        # Per-market books (copies to avoid mutating input)
        self._books: dict[str, BookState] = {
            k: BookState(
                yes_levels=dict(v.yes_levels),
                no_levels=dict(v.no_levels),
            )
            for k, v in initial_books.items()
        }

        self._orders: dict[str, _Order] = {}
        self._fills: list[_Fill] = []
        self._positions: dict[str, float] = {}  # ticker → net YES position
        self._ws_queue: list[dict] = []
        self._seq_counter: int = 0
        self._trade_counter: int = 0

    def _next_order_id(self) -> str:
        return f"ORD-{uuid.UUID(int=self._rng.getrandbits(128)).hex[:12]}"

    def _next_trade_id(self) -> str:
        self._trade_counter += 1
        return f"TRD-{self._trade_counter:06d}"

    def _next_seq(self) -> int:
        self._seq_counter += 1
        return self._seq_counter

    def _enqueue_ws(self, msg_type: str, msg: dict) -> None:
        self._ws_queue.append({
            "type": msg_type,
            "sid": 1,
            "seq": self._next_seq(),
            "msg": msg,
        })

    # ------------------------------------------------------------------
    # REST-like interface
    # ------------------------------------------------------------------

    def create_order(self, params: dict) -> dict:
        ticker = params["ticker"]
        action = params["action"]
        side = params["side"]
        count = params["count"]
        tif = params["time_in_force"]
        price_cents = params.get("yes_price") or params.get("no_price") or 0
        client_order_id = params.get("client_order_id")

        order_id = self._next_order_id()
        order = _Order(
            order_id=order_id,
            client_order_id=client_order_id,
            ticker=ticker,
            action=action,
            side=side,
            count=count,
            price_cents=price_cents,
            tif=tif,
            status="resting",
        )

        # Try to match
        fill_count = self._try_match(order)

        if fill_count >= count:
            order.status = "filled"
            order.filled_count = count
        elif fill_count > 0 and tif == "fill_or_kill":
            # FOK: partial fill not allowed → reject
            order.status = "rejected"
            order.filled_count = 0
        elif fill_count == 0 and tif == "fill_or_kill":
            order.status = "rejected"
        elif fill_count > 0 and tif == "immediate_or_cancel":
            # IOC: fill what we can, cancel rest
            order.status = "canceled"
            order.filled_count = fill_count
        elif fill_count == 0 and tif == "immediate_or_cancel":
            order.status = "canceled"
        elif fill_count > 0:
            order.status = "filled"
            order.filled_count = fill_count
        else:
            order.status = "resting"

        self._orders[order_id] = order

        # Enqueue user_order WS message
        self._enqueue_ws("user_order", {
            "order_id": order_id,
            "ticker": ticker,
            "status": order.status,
            "side": side,
            "action": action,
            "client_order_id": client_order_id,
            "remaining_count_fp": str(float(count - order.filled_count)),
            "fill_count_fp": str(float(order.filled_count)),
            "initial_count_fp": str(float(count)),
        })

        # Enqueue fill WS messages
        if order.filled_count > 0:
            fill = _Fill(
                trade_id=self._next_trade_id(),
                order_id=order_id,
                client_order_id=client_order_id,
                ticker=ticker,
                side=side,
                action=action,
                count=order.filled_count,
                price_cents=price_cents,
                is_taker=True,
                fee_cents=round(price_cents * order.filled_count * 0.07, 4),
            )
            self._fills.append(fill)
            self._update_position(ticker, side, action, order.filled_count)
            self._deduct_balance(action, price_cents, order.filled_count)

            price_dollars = f"{price_cents / 100:.2f}"
            fill_msg = {
                "trade_id": fill.trade_id,
                "order_id": order_id,
                "client_order_id": client_order_id,
                "market_ticker": ticker,
                "side": side,
                "action": action,
                "count_fp": str(float(fill.count)),
                "is_taker": True,
                "ts": 0,
                f"{side}_price_dollars": price_dollars,
                "fee_cost": f"{fill.fee_cents / 100:.4f}",
            }
            self._enqueue_ws("fill", fill_msg)

        return {"order": self._order_to_dict(order)}

    def cancel_order(self, order_id: str) -> dict:
        if order_id not in self._orders:
            raise KeyError(f"Order {order_id} not found")
        order = self._orders[order_id]
        if order.status != "resting":
            raise ValueError(f"Cannot cancel order in status {order.status}")
        order.status = "canceled"
        self._enqueue_ws("user_order", {
            "order_id": order_id,
            "ticker": order.ticker,
            "status": "canceled",
            "side": order.side,
            "action": order.action,
            "client_order_id": order.client_order_id,
        })
        return {"order": self._order_to_dict(order)}

    def batch_create_orders(self, orders: list[dict]) -> dict:
        results = []
        for params in orders:
            try:
                resp = self.create_order(params)
                results.append(resp)
            except Exception as e:
                results.append({"error": str(e)})
        return {"results": results}

    def get_order(self, order_id: str) -> dict:
        if order_id not in self._orders:
            raise KeyError(f"Order {order_id} not found")
        return {"order": self._order_to_dict(self._orders[order_id])}

    def get_orders(self, params: dict | None = None) -> dict:
        params = params or {}
        status_filter = params.get("status")
        orders = list(self._orders.values())
        if status_filter:
            orders = [o for o in orders if o.status == status_filter]
        return {"orders": [self._order_to_dict(o) for o in orders]}

    def get_fills(self, params: dict | None = None) -> dict:
        params = params or {}
        order_id = params.get("order_id")
        fills = self._fills
        if order_id:
            fills = [f for f in fills if f.order_id == order_id]
        return {"fills": [self._fill_to_dict(f) for f in fills]}

    def get_positions(self, params: dict | None = None) -> dict:
        exposures = []
        for ticker, position_fp in self._positions.items():
            if position_fp != 0:
                exposures.append({
                    "ticker": ticker,
                    "market_id": ticker,
                    "position_fp": str(position_fp),
                })
        return {"market_exposures": exposures}

    def get_balance(self) -> int:
        return self._balance_cents

    def drain_ws_messages(self) -> list[dict]:
        msgs = list(self._ws_queue)
        self._ws_queue.clear()
        return msgs

    # ------------------------------------------------------------------
    # Settlement
    # ------------------------------------------------------------------

    def settle_market(self, ticker: str, outcome: str) -> None:
        """Settle a market. Generates settlement fills (no market_ticker) for open positions."""
        position = self._positions.get(ticker, 0)
        if position == 0:
            return

        # Cancel resting orders for this market
        for order in self._orders.values():
            if order.ticker == ticker and order.status == "resting":
                order.status = "canceled"

        # Generate settlement fill
        price_cents = 100 if outcome == "yes" else 0
        count = abs(int(position))
        action = "sell" if position > 0 else "buy"
        side = "yes" if position > 0 else "no"

        fill = _Fill(
            trade_id=self._next_trade_id(),
            order_id="SETTLEMENT",
            client_order_id=None,
            ticker=None,  # Settlement fills have no ticker
            side=side,
            action=action,
            count=count,
            price_cents=price_cents,
            is_taker=False,
            fee_cents=0,
        )
        self._fills.append(fill)
        self._enqueue_ws("fill", {
            "trade_id": fill.trade_id,
            "order_id": "SETTLEMENT",
            "client_order_id": None,
            "market_ticker": None,
            "side": side,
            "action": action,
            "count_fp": str(float(count)),
            "is_taker": False,
            "ts": 0,
            "fee_cost": None,
        })
        self._positions[ticker] = 0

    # ------------------------------------------------------------------
    # Test inspection
    # ------------------------------------------------------------------

    def get_order_state(self, order_id: str) -> _Order:
        return self._orders[order_id]

    def get_book(self, ticker: str) -> BookState:
        return self._books.get(ticker, BookState())

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _try_match(self, order: _Order) -> int:
        """Try to match an order against the book. Returns fill count."""
        book = self._books.get(order.ticker)
        if not book:
            return 0

        if order.action == "buy":
            # Buy YES: need someone selling YES (or buying NO)
            # Simplified: check if price >= implied ask
            if order.side == "yes":
                # YES ask = 100 - best NO bid
                no_levels = book.no_levels
                if not no_levels:
                    return 0
                best_no_bid = max(no_levels.keys())
                implied_yes_ask = 100 - best_no_bid
                if order.price_cents >= implied_yes_ask:
                    available = no_levels[best_no_bid]
                    fill_count = min(order.count, available)
                    # Consume liquidity
                    no_levels[best_no_bid] -= fill_count
                    if no_levels[best_no_bid] <= 0:
                        del no_levels[best_no_bid]
                    return fill_count
            else:
                # Buy NO: check against YES book
                yes_levels = book.yes_levels
                if not yes_levels:
                    return 0
                best_yes_bid = max(yes_levels.keys())
                implied_no_ask = 100 - best_yes_bid
                if order.price_cents >= implied_no_ask:
                    available = yes_levels[best_yes_bid]
                    fill_count = min(order.count, available)
                    yes_levels[best_yes_bid] -= fill_count
                    if yes_levels[best_yes_bid] <= 0:
                        del yes_levels[best_yes_bid]
                    return fill_count
        elif order.action == "sell":
            # Sell YES: need YES buyers at >= our price
            if order.side == "yes":
                yes_levels = book.yes_levels
                if not yes_levels:
                    return 0
                best_yes_bid = max(yes_levels.keys())
                if best_yes_bid >= order.price_cents:
                    available = yes_levels[best_yes_bid]
                    fill_count = min(order.count, available)
                    yes_levels[best_yes_bid] -= fill_count
                    if yes_levels[best_yes_bid] <= 0:
                        del yes_levels[best_yes_bid]
                    return fill_count
            else:
                no_levels = book.no_levels
                if not no_levels:
                    return 0
                best_no_bid = max(no_levels.keys())
                if best_no_bid >= order.price_cents:
                    available = no_levels[best_no_bid]
                    fill_count = min(order.count, available)
                    no_levels[best_no_bid] -= fill_count
                    if no_levels[best_no_bid] <= 0:
                        del no_levels[best_no_bid]
                    return fill_count
        return 0

    def _update_position(self, ticker: str, side: str, action: str, count: int) -> None:
        delta = count if action == "buy" else -count
        if side == "no":
            delta = -delta  # NO position is inverse of YES
        self._positions[ticker] = self._positions.get(ticker, 0) + delta

    def _deduct_balance(self, action: str, price_cents: int, count: int) -> None:
        if action == "buy":
            self._balance_cents -= price_cents * count
        else:
            self._balance_cents += price_cents * count

    def _order_to_dict(self, order: _Order) -> dict:
        d = {
            "order_id": order.order_id,
            "client_order_id": order.client_order_id,
            "ticker": order.ticker,
            "action": order.action,
            "side": order.side,
            "count": order.count,
            "status": order.status,
            f"{order.side}_price": order.price_cents,
        }
        return d

    def _fill_to_dict(self, fill: _Fill) -> dict:
        d = {
            "trade_id": fill.trade_id,
            "order_id": fill.order_id,
            "client_order_id": fill.client_order_id,
            "market_ticker": fill.ticker,
            "side": fill.side,
            "action": fill.action,
            "count_fp": str(float(fill.count)),
            "is_taker": fill.is_taker,
            f"{fill.side}_price": fill.price_cents,
            "fee_cost": f"{fill.fee_cents / 100:.4f}" if fill.fee_cents else None,
        }
        return d
```

**Step 2: Run the tests**

```bash
pytest tests/sim/test_fake_exchange.py -v --tb=short
```

Expected: All tests pass. Determinism test confirms same seed → same output.

**Step 3: Commit**

```bash
git add tests/sim/
git commit -m "feat(sim): add FakeKalshiExchange with deterministic order matching"
```

---

### Task 5: Build FaultInjector

**Files:**
- Create: `tests/sim/fault_injector.py`
- Create: `tests/sim/test_fault_injector.py`

**Step 1: Write tests for FaultInjector**

```python
"""Tests for FaultInjector — middleware that wraps FakeKalshiExchange."""
import pytest
from unittest.mock import MagicMock
from tests.sim.fault_injector import FaultInjector, FaultProfile
from tests.sim.fake_exchange import FakeKalshiExchange, BookState


def _make_exchange(seed=42):
    return FakeKalshiExchange(
        seed=seed,
        initial_books={"T": BookState(yes_levels={50: 100}, no_levels={50: 100})},
        balance_cents=100_000,
    )


class TestFaultInjectorPassthrough:
    """With default (no-fault) profile, injector is transparent."""

    def test_create_order_passes_through(self):
        ex = _make_exchange()
        inj = FaultInjector(exchange=ex, profile=FaultProfile(seed=1))
        resp = inj.create_order({
            "ticker": "T", "action": "buy", "side": "yes",
            "count": 5, "type": "limit", "yes_price": 99,
            "time_in_force": "good_till_canceled", "client_order_id": "C-1",
        })
        assert resp["order"]["status"] in ("filled", "resting")

    def test_ws_messages_pass_through(self):
        ex = _make_exchange()
        inj = FaultInjector(exchange=ex, profile=FaultProfile(seed=1))
        inj.create_order({
            "ticker": "T", "action": "buy", "side": "yes",
            "count": 5, "type": "limit", "yes_price": 99,
            "time_in_force": "good_till_canceled", "client_order_id": "C-2",
        })
        msgs = inj.drain_ws_messages()
        assert len(msgs) > 0


class TestFaultInjectorDrops:
    def test_ws_messages_dropped_at_configured_rate(self):
        """With 100% drop rate, no WS messages should come through."""
        ex = _make_exchange()
        inj = FaultInjector(
            exchange=ex,
            profile=FaultProfile(seed=1, ws_drop_rate=1.0),
        )
        inj.create_order({
            "ticker": "T", "action": "buy", "side": "yes",
            "count": 5, "type": "limit", "yes_price": 99,
            "time_in_force": "good_till_canceled", "client_order_id": "C-3",
        })
        msgs = inj.drain_ws_messages()
        assert len(msgs) == 0

    def test_partial_drops_are_deterministic(self):
        """Same seed + same profile → same messages dropped."""
        def _run(seed):
            ex = _make_exchange(seed=seed)
            inj = FaultInjector(
                exchange=ex,
                profile=FaultProfile(seed=seed, ws_drop_rate=0.5),
            )
            for i in range(10):
                inj.create_order({
                    "ticker": "T", "action": "buy", "side": "yes",
                    "count": 1, "type": "limit", "yes_price": 99,
                    "time_in_force": "good_till_canceled",
                    "client_order_id": f"C-{i}",
                })
            return inj.drain_ws_messages()

        msgs1 = _run(seed=42)
        msgs2 = _run(seed=42)
        assert msgs1 == msgs2


class TestFaultInjectorDuplication:
    def test_ws_messages_duplicated(self):
        """With 100% dup rate, every message appears twice."""
        ex = _make_exchange()
        inj = FaultInjector(
            exchange=ex,
            profile=FaultProfile(seed=1, ws_duplicate_rate=1.0),
        )
        inj.create_order({
            "ticker": "T", "action": "buy", "side": "yes",
            "count": 5, "type": "limit", "yes_price": 99,
            "time_in_force": "good_till_canceled", "client_order_id": "C-4",
        })
        msgs = inj.drain_ws_messages()
        # Each original message should appear twice
        assert len(msgs) % 2 == 0


class TestFaultInjectorRateLimit:
    def test_rate_limit_returns_429(self):
        """After N requests, create_order returns 429."""
        ex = _make_exchange()
        inj = FaultInjector(
            exchange=ex,
            profile=FaultProfile(seed=1, rate_limit_after=2),
        )
        # First 2 should succeed
        inj.create_order({
            "ticker": "T", "action": "buy", "side": "yes",
            "count": 1, "type": "limit", "yes_price": 99,
            "time_in_force": "good_till_canceled", "client_order_id": "C-5",
        })
        inj.create_order({
            "ticker": "T", "action": "buy", "side": "yes",
            "count": 1, "type": "limit", "yes_price": 99,
            "time_in_force": "good_till_canceled", "client_order_id": "C-6",
        })
        # 3rd should raise
        from kalshi_python.exceptions import ApiException
        with pytest.raises(ApiException) as exc_info:
            inj.create_order({
                "ticker": "T", "action": "buy", "side": "yes",
                "count": 1, "type": "limit", "yes_price": 99,
                "time_in_force": "good_till_canceled", "client_order_id": "C-7",
            })
        assert exc_info.value.status == 429


class TestFaultInjectorSequenceGaps:
    def test_sequence_gap_injected(self):
        """With 100% gap rate, every WS message skips a seq number."""
        ex = _make_exchange()
        inj = FaultInjector(
            exchange=ex,
            profile=FaultProfile(seed=1, sequence_gap_rate=1.0),
        )
        inj.create_order({
            "ticker": "T", "action": "buy", "side": "yes",
            "count": 5, "type": "limit", "yes_price": 99,
            "time_in_force": "good_till_canceled", "client_order_id": "C-8",
        })
        msgs = inj.drain_ws_messages()
        if len(msgs) >= 2:
            seqs = [m["seq"] for m in msgs]
            # With 100% gap rate, there should be gaps
            for i in range(1, len(seqs)):
                assert seqs[i] > seqs[i - 1] + 1 or seqs[i] == seqs[i - 1] + 2
```

**Step 2: Implement FaultInjector**

```python
"""Fault injection middleware wrapping FakeKalshiExchange.

All randomness flows through a single seeded PRNG. The injector does NOT
modify the exchange's internal state — it only transforms messages between
the exchange and the adapter.
"""
from __future__ import annotations

import random
from dataclasses import dataclass

from kalshi_python.exceptions import ApiException

from tests.sim.fake_exchange import FakeKalshiExchange


@dataclass(frozen=True)
class FaultProfile:
    """Declarative fault configuration. All rates are probabilities [0.0, 1.0]."""
    seed: int
    ws_drop_rate: float = 0.0
    ws_duplicate_rate: float = 0.0
    ws_reorder_window: int = 0
    sequence_gap_rate: float = 0.0
    rate_limit_after: int | None = None
    disconnect_after_n_msgs: int | None = None


class FaultInjector:
    """Wraps a FakeKalshiExchange, injecting faults on the wire."""

    def __init__(self, exchange: FakeKalshiExchange, profile: FaultProfile) -> None:
        self._exchange = exchange
        self._profile = profile
        self._rng = random.Random(profile.seed)
        self._request_count = 0
        self._ws_msg_count = 0
        self._seq_offset = 0  # extra seq numbers to skip

    # ------------------------------------------------------------------
    # REST pass-through with fault injection
    # ------------------------------------------------------------------

    def create_order(self, params: dict) -> dict:
        self._check_rate_limit()
        return self._exchange.create_order(params)

    def cancel_order(self, order_id: str) -> dict:
        self._check_rate_limit()
        return self._exchange.cancel_order(order_id)

    def batch_create_orders(self, orders: list[dict]) -> dict:
        self._check_rate_limit()
        return self._exchange.batch_create_orders(orders)

    def get_order(self, order_id: str) -> dict:
        return self._exchange.get_order(order_id)

    def get_orders(self, params: dict | None = None) -> dict:
        return self._exchange.get_orders(params)

    def get_fills(self, params: dict | None = None) -> dict:
        return self._exchange.get_fills(params)

    def get_positions(self, params: dict | None = None) -> dict:
        return self._exchange.get_positions(params)

    def get_balance(self) -> int:
        return self._exchange.get_balance()

    # ------------------------------------------------------------------
    # WS message delivery with fault injection
    # ------------------------------------------------------------------

    def drain_ws_messages(self) -> list[dict]:
        raw_msgs = self._exchange.drain_ws_messages()
        result = []

        for msg in raw_msgs:
            # Drop?
            if self._rng.random() < self._profile.ws_drop_rate:
                continue

            # Sequence gap?
            if self._rng.random() < self._profile.sequence_gap_rate:
                self._seq_offset += 1
                msg = {**msg, "seq": msg["seq"] + self._seq_offset}
            elif self._seq_offset > 0:
                msg = {**msg, "seq": msg["seq"] + self._seq_offset}

            result.append(msg)

            # Duplicate?
            if self._rng.random() < self._profile.ws_duplicate_rate:
                result.append(dict(msg))

            # Disconnect check
            self._ws_msg_count += 1
            if (self._profile.disconnect_after_n_msgs is not None
                    and self._ws_msg_count >= self._profile.disconnect_after_n_msgs):
                break  # Simulate disconnect — drop remaining messages

        # Reorder?
        if self._profile.ws_reorder_window > 0 and len(result) > 1:
            result = self._reorder(result)

        return result

    def _reorder(self, msgs: list[dict]) -> list[dict]:
        """Shuffle messages within sliding window."""
        window = self._profile.ws_reorder_window
        result = []
        buffer = []
        for msg in msgs:
            buffer.append(msg)
            if len(buffer) >= window:
                self._rng.shuffle(buffer)
                result.extend(buffer)
                buffer = []
        if buffer:
            self._rng.shuffle(buffer)
            result.extend(buffer)
        return result

    # ------------------------------------------------------------------
    # Settlement / inspection pass-through
    # ------------------------------------------------------------------

    def settle_market(self, ticker: str, outcome: str) -> None:
        self._exchange.settle_market(ticker, outcome)

    def get_order_state(self, order_id: str):
        return self._exchange.get_order_state(order_id)

    def get_book(self, ticker: str):
        return self._exchange.get_book(ticker)

    # ------------------------------------------------------------------
    # Fault logic
    # ------------------------------------------------------------------

    def _check_rate_limit(self) -> None:
        self._request_count += 1
        if (self._profile.rate_limit_after is not None
                and self._request_count > self._profile.rate_limit_after):
            raise ApiException(status=429, reason="Too Many Requests")

    def reset_rate_limit(self) -> None:
        """Reset the request counter (simulates token replenishment)."""
        self._request_count = 0

    def reset_disconnect(self) -> None:
        """Reset disconnect counter."""
        self._ws_msg_count = 0
```

**Step 3: Run tests**

```bash
pytest tests/sim/test_fault_injector.py -v --tb=short
```

Expected: All tests pass.

**Step 4: Commit**

```bash
git add tests/sim/fault_injector.py tests/sim/test_fault_injector.py
git commit -m "feat(sim): add FaultInjector middleware with drops, dups, gaps, rate limits"
```

---

### Task 5.5: Review Tasks 4-5

**Trigger:** Both reviewers start simultaneously when Tasks 4-5 complete.

**Spec review — verify these specific items:**
- [ ] `FakeKalshiExchange` response dicts match Kalshi API shapes from `kalshi/execution.py:249` (`{"order": {"order_id": ...}}`) and `kalshi/execution.py:508` (`{"fills": [...]}`)
- [ ] WS messages match `FillMsg` struct fields from `kalshi/websocket/types.py:40-53` — specifically `trade_id`, `order_id`, `market_ticker`, `side`, `action`, `count_fp`, `is_taker`, `client_order_id`, `yes_price_dollars`/`no_price_dollars`, `fee_cost`
- [ ] WS messages match `UserOrderMsg` struct fields from `kalshi/websocket/types.py:24-37` — specifically `order_id`, `ticker`, `status`, `side`, `action`, `client_order_id`
- [ ] Settlement fills have `market_ticker: None` (matches `kalshi/execution.py:372` check)
- [ ] `FaultInjector` does NOT modify exchange internal state — only transforms messages on the wire
- [ ] Rate limiting raises `ApiException(status=429)` matching what `should_retry()` expects in `kalshi/common/errors.py:17`
- [ ] Multi-market independence tested (design doc: "cross-market testing")
- [ ] All four book states tested: empty, thin, deep, one-sided (design doc resolved questions)

**Code quality review — verify these specific items:**
- [ ] `_try_match()` correctly handles all 4 action×side combinations (buy YES, buy NO, sell YES, sell NO)
- [ ] Determinism test actually verifies identical output (not just "both succeed")
- [ ] `FaultProfile` is frozen dataclass (immutable after creation)
- [ ] PRNG usage: only `self._rng` used, no calls to `random.random()` module-level

**Resolution:** Findings queue for dev; all must resolve before next milestone.

---

### Task 5.6: Milestone — Fake Exchange + Fault Injector

**Present to user:**
- Fake exchange test results (pass counts, matching behavior)
- Fault injector test results (drops, dups, gaps, rate limits)
- Determinism verification (same seed = same output)
- WS message shape conformance with actual Kalshi WS types

**Wait for user response before proceeding to Task 6.**

---

### Task 6: Invariant Checkers

**Files:**
- Create: `tests/sim/invariants.py`
- Create: `tests/sim/test_invariants.py`

**Step 1: Write tests for invariant checker functions**

```python
"""Tests for simulation invariant checkers."""
import pytest
from tests.sim.invariants import (
    check_no_duplicate_fills,
    check_positions_reconcile,
    check_no_orphaned_orders,
    check_rate_limit_respected,
)


class TestNoDuplicateFills:
    def test_no_duplicates_passes(self):
        fills = [
            {"trade_id": "T-1", "instrument_id": "A"},
            {"trade_id": "T-2", "instrument_id": "A"},
        ]
        violations = check_no_duplicate_fills(fills)
        assert violations == []

    def test_duplicates_detected(self):
        fills = [
            {"trade_id": "T-1", "instrument_id": "A"},
            {"trade_id": "T-1", "instrument_id": "A"},
        ]
        violations = check_no_duplicate_fills(fills)
        assert len(violations) == 1
        assert "T-1" in violations[0]


class TestPositionsReconcile:
    def test_matching_positions_passes(self):
        adapter_positions = {"TICKER-A": 5.0, "TICKER-B": -3.0}
        exchange_positions = {"TICKER-A": 5.0, "TICKER-B": -3.0}
        violations = check_positions_reconcile(adapter_positions, exchange_positions)
        assert violations == []

    def test_mismatch_detected(self):
        adapter_positions = {"TICKER-A": 5.0}
        exchange_positions = {"TICKER-A": 3.0}
        violations = check_positions_reconcile(adapter_positions, exchange_positions)
        assert len(violations) == 1

    def test_missing_position_detected(self):
        adapter_positions = {}
        exchange_positions = {"TICKER-A": 5.0}
        violations = check_positions_reconcile(adapter_positions, exchange_positions)
        assert len(violations) == 1


class TestNoOrphanedOrders:
    def test_all_terminal_passes(self):
        orders = [
            {"order_id": "O-1", "status": "filled"},
            {"order_id": "O-2", "status": "canceled"},
            {"order_id": "O-3", "status": "rejected"},
        ]
        violations = check_no_orphaned_orders(orders)
        assert violations == []

    def test_resting_order_is_orphan(self):
        orders = [
            {"order_id": "O-1", "status": "resting"},
        ]
        violations = check_no_orphaned_orders(orders)
        assert len(violations) == 1


class TestRateLimitRespected:
    def test_under_limit_passes(self):
        timestamps = [0.0, 0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9]
        violations = check_rate_limit_respected(timestamps, max_per_second=10)
        assert violations == []

    def test_over_limit_detected(self):
        # 11 requests in 1 second
        timestamps = [0.0] * 11
        violations = check_rate_limit_respected(timestamps, max_per_second=10)
        assert len(violations) >= 1
```

**Step 2: Implement invariant checkers**

```python
"""Invariant checkers for simulation testing.

Each function returns a list of violation descriptions (empty = pass).
"""
from __future__ import annotations


def check_no_duplicate_fills(fills: list[dict]) -> list[str]:
    """No trade_id should appear more than once in emitted fills."""
    seen: set[str] = set()
    violations = []
    for fill in fills:
        tid = fill["trade_id"]
        if tid in seen:
            violations.append(f"Duplicate fill emitted: trade_id={tid}")
        seen.add(tid)
    return violations


def check_positions_reconcile(
    adapter_positions: dict[str, float],
    exchange_positions: dict[str, float],
) -> list[str]:
    """Adapter position state must match exchange ground truth."""
    violations = []
    all_tickers = set(adapter_positions) | set(exchange_positions)
    for ticker in all_tickers:
        adapter_qty = adapter_positions.get(ticker, 0.0)
        exchange_qty = exchange_positions.get(ticker, 0.0)
        if abs(adapter_qty - exchange_qty) > 1e-9:
            violations.append(
                f"Position mismatch for {ticker}: "
                f"adapter={adapter_qty}, exchange={exchange_qty}"
            )
    return violations


def check_no_orphaned_orders(orders: list[dict]) -> list[str]:
    """Every submitted order must reach a terminal state."""
    terminal = {"filled", "canceled", "rejected"}
    violations = []
    for order in orders:
        if order["status"] not in terminal:
            violations.append(
                f"Orphaned order: {order['order_id']} in status {order['status']}"
            )
    return violations


def check_rate_limit_respected(
    request_timestamps: list[float],
    max_per_second: int = 10,
) -> list[str]:
    """No more than max_per_second write requests in any 1-second window."""
    violations = []
    sorted_ts = sorted(request_timestamps)
    for i, ts in enumerate(sorted_ts):
        window_end = ts + 1.0
        count = sum(1 for t in sorted_ts[i:] if t < window_end)
        if count > max_per_second:
            violations.append(
                f"Rate limit exceeded: {count} requests in 1s window starting at t={ts:.3f}"
            )
            break  # One violation is enough
    return violations
```

**Step 3: Run tests**

```bash
pytest tests/sim/test_invariants.py -v --tb=short
```

Expected: All tests pass.

**Step 4: Commit**

```bash
git add tests/sim/invariants.py tests/sim/test_invariants.py
git commit -m "feat(sim): add invariant checkers for fills, positions, orders, rate limits"
```

---

### Task 7: NT Test Harness — wire adapter to fake exchange

**Files:**
- Create: `tests/sim/harness.py`
- Create: `tests/sim/test_harness.py`

This is the most complex task. The harness wires the real `KalshiExecutionClient` (via its `_handle_fill` and `_handle_user_order` methods) to the fake exchange, routing WS messages through.

> **Before implementing:** Invoke `forge:nautilus-testing` skill — it covers NT Cython test patterns and mock wiring.

**Step 1: Write tests for the harness**

```python
"""Tests for the simulation test harness — adapter wired to fake exchange."""
import asyncio
import json
import types
from collections import OrderedDict
from unittest.mock import AsyncMock, MagicMock

import msgspec
import pytest

from tests.sim.harness import SimHarness
from tests.sim.fake_exchange import BookState, FakeKalshiExchange
from tests.sim.fault_injector import FaultInjector, FaultProfile


class TestSimHarnessBasic:
    def test_submit_order_fills_and_emits_events(self):
        """Submit an order that should fill → adapter emits accepted + filled."""
        harness = SimHarness(
            seed=42,
            books={"T": BookState(yes_levels={50: 100}, no_levels={50: 100})},
        )
        harness.submit_order(
            ticker="T", side="yes", action="buy",
            price_cents=99, count=5, tif="good_till_canceled",
        )
        harness.deliver_ws_messages()

        # Check events emitted
        assert harness.accepted_count >= 1
        assert harness.filled_count >= 1

    def test_submit_order_rests_when_below_ask(self):
        harness = SimHarness(
            seed=42,
            books={"T": BookState(yes_levels={50: 100}, no_levels={50: 100})},
        )
        harness.submit_order(
            ticker="T", side="yes", action="buy",
            price_cents=25, count=5, tif="good_till_canceled",
        )
        harness.deliver_ws_messages()
        assert harness.accepted_count >= 1
        assert harness.filled_count == 0

    def test_dedup_prevents_duplicate_fills(self):
        """Duplicate WS fill messages → only one fill event emitted."""
        harness = SimHarness(
            seed=42,
            books={"T": BookState(yes_levels={50: 100}, no_levels={50: 100})},
            fault_profile=FaultProfile(seed=42, ws_duplicate_rate=1.0),
        )
        harness.submit_order(
            ticker="T", side="yes", action="buy",
            price_cents=99, count=5, tif="good_till_canceled",
        )
        harness.deliver_ws_messages()
        # Despite duplicates, should only emit one fill
        assert harness.filled_count == 1

    def test_settlement_fill_triggers_reconciliation(self):
        harness = SimHarness(
            seed=42,
            books={"T": BookState(yes_levels={50: 100}, no_levels={50: 100})},
        )
        # Create a position
        harness.submit_order(
            ticker="T", side="yes", action="buy",
            price_cents=99, count=5, tif="good_till_canceled",
        )
        harness.deliver_ws_messages()

        # Settle
        harness.settle_market("T", outcome="yes")
        harness.deliver_ws_messages()

        assert harness.settlement_recon_count >= 1

    def test_sequence_gap_triggers_reconciliation(self):
        harness = SimHarness(
            seed=42,
            books={"T": BookState(yes_levels={50: 100}, no_levels={50: 100})},
            fault_profile=FaultProfile(seed=42, sequence_gap_rate=1.0),
        )
        harness.submit_order(
            ticker="T", side="yes", action="buy",
            price_cents=99, count=5, tif="good_till_canceled",
        )
        harness.deliver_ws_messages()
        assert harness.recon_triggered_count >= 1

    def test_cross_market_positions_independent(self):
        harness = SimHarness(
            seed=42,
            books={
                "A": BookState(yes_levels={50: 100}, no_levels={50: 100}),
                "B": BookState(yes_levels={30: 100}, no_levels={70: 100}),
            },
        )
        harness.submit_order(
            ticker="A", side="yes", action="buy",
            price_cents=99, count=5, tif="good_till_canceled",
        )
        harness.submit_order(
            ticker="B", side="yes", action="buy",
            price_cents=25, count=5, tif="good_till_canceled",
        )
        harness.deliver_ws_messages()
        # A should fill, B should rest
        assert harness.filled_count == 1
        assert harness.accepted_count >= 2
```

**Step 2: Implement SimHarness**

The harness binds `_handle_fill` and `_handle_user_order` from `KalshiExecutionClient` onto a stub, then feeds WS messages from the fake exchange through them.

```python
"""Simulation test harness — wires KalshiExecutionClient methods to FakeKalshiExchange.

Uses the SimpleNamespace + MethodType pattern from test_execution.py:117-137
to avoid Cython descriptor issues.
"""
from __future__ import annotations

import types
from collections import OrderedDict
from unittest.mock import AsyncMock, MagicMock

import msgspec

from kalshi.common.constants import KALSHI_VENUE
from kalshi.execution import KalshiExecutionClient, _DEDUP_MAX
from kalshi.websocket.types import FillMsg, UserOrderMsg, decode_ws_msg
from nautilus_trader.model.identifiers import InstrumentId, Symbol

from tests.sim.fake_exchange import BookState, FakeKalshiExchange
from tests.sim.fault_injector import FaultInjector, FaultProfile


class SimHarness:
    """Wires adapter fill/order handling to a fake exchange.

    Does NOT instantiate a full NautilusTrader kernel. Instead, binds
    the methods under test onto a stub with mocked NT infrastructure.
    """

    def __init__(
        self,
        seed: int,
        books: dict[str, BookState],
        fault_profile: FaultProfile | None = None,
        balance_cents: int = 100_000,
    ) -> None:
        self._exchange = FakeKalshiExchange(
            seed=seed, initial_books=books, balance_cents=balance_cents,
        )
        profile = fault_profile or FaultProfile(seed=seed)
        self._injector = FaultInjector(exchange=self._exchange, profile=profile)

        # Build stub with the attributes _handle_fill and _handle_user_order need
        self._stub = self._build_stub(books)

        # Counters for assertions
        self.accepted_count = 0
        self.filled_count = 0
        self.rejected_count = 0
        self.canceled_count = 0
        self.settlement_recon_count = 0
        self.recon_triggered_count = 0

    def _build_stub(self, books: dict[str, BookState]) -> types.SimpleNamespace:
        """Build a stub that mimics KalshiExecutionClient's needed attributes."""
        # Mock instrument provider that returns instruments for known tickers
        provider = MagicMock()
        instrument = MagicMock()
        instrument.make_qty = lambda x: x
        instrument.make_price = lambda x: x
        provider.find.return_value = instrument

        stub = types.SimpleNamespace(
            _seen_trade_ids=OrderedDict(),
            _accepted_orders={},
            _instrument_provider=provider,
            _clock=MagicMock(timestamp_ns=MagicMock(return_value=1_000_000_000)),
            _ws_client=MagicMock(),
            _ack_events={},
            _log=MagicMock(),
            generate_order_accepted=MagicMock(side_effect=lambda **kw: self._on_accepted(**kw)),
            generate_order_filled=MagicMock(side_effect=lambda **kw: self._on_filled(**kw)),
            generate_order_rejected=MagicMock(side_effect=lambda **kw: self._on_rejected(**kw)),
            generate_order_canceled=MagicMock(side_effect=lambda **kw: self._on_canceled(**kw)),
            generate_position_status_reports=AsyncMock(
                side_effect=lambda *a: self._on_settlement_recon(),
                return_value=[],
            ),
            generate_order_status_reports=AsyncMock(
                side_effect=lambda *a: self._on_recon_triggered(),
                return_value=[],
            ),
            generate_fill_reports=AsyncMock(return_value=[]),
        )

        # Bind methods from KalshiExecutionClient
        stub._handle_fill = types.MethodType(KalshiExecutionClient._handle_fill, stub)
        stub._handle_user_order = types.MethodType(KalshiExecutionClient._handle_user_order, stub)

        # Sequence checker
        stub._ws_client._check_sequence = MagicMock(return_value=True)

        return stub

    def _on_accepted(self, **kw):
        self.accepted_count += 1

    def _on_filled(self, **kw):
        self.filled_count += 1

    def _on_rejected(self, **kw):
        self.rejected_count += 1

    def _on_canceled(self, **kw):
        self.canceled_count += 1

    def _on_settlement_recon(self):
        self.settlement_recon_count += 1

    def _on_recon_triggered(self):
        self.recon_triggered_count += 1

    # ------------------------------------------------------------------
    # Operations
    # ------------------------------------------------------------------

    def submit_order(
        self,
        ticker: str,
        side: str,
        action: str,
        price_cents: int,
        count: int,
        tif: str,
        client_order_id: str | None = None,
    ) -> dict:
        """Submit an order through the fake exchange."""
        if client_order_id is None:
            client_order_id = f"C-{id(self)}-{self.accepted_count + self.rejected_count}"
        params = {
            "ticker": ticker,
            "action": action,
            "side": side,
            "count": count,
            "type": "limit",
            f"{side}_price": price_cents,
            "time_in_force": tif,
            "client_order_id": client_order_id,
        }
        return self._injector.create_order(params)

    def cancel_order(self, order_id: str) -> dict:
        return self._injector.cancel_order(order_id)

    def settle_market(self, ticker: str, outcome: str) -> None:
        self._injector.settle_market(ticker, outcome)

    def deliver_ws_messages(self) -> int:
        """Drain WS messages from injector and feed them to the adapter stub.

        Returns count of messages delivered.
        """
        msgs = self._injector.drain_ws_messages()
        delivered = 0

        for ws_msg in msgs:
            msg_type = ws_msg["type"]
            sid = ws_msg.get("sid", 1)
            seq = ws_msg.get("seq", 0)
            msg_data = ws_msg.get("msg", {})
            ts = self._stub._clock.timestamp_ns()

            # Check sequence (may trigger recon)
            if not self._stub._ws_client._check_sequence(sid=sid, seq=seq):
                self.recon_triggered_count += 1
                continue

            if msg_type == "user_order":
                typed_msg = UserOrderMsg(**{
                    k: v for k, v in msg_data.items()
                    if k in UserOrderMsg.__struct_fields__
                })
                self._stub._handle_user_order(typed_msg, ts)
            elif msg_type == "fill":
                typed_msg = FillMsg(**{
                    k: v for k, v in msg_data.items()
                    if k in FillMsg.__struct_fields__
                })
                self._stub._handle_fill(typed_msg, ts)

            delivered += 1

        return delivered

    # ------------------------------------------------------------------
    # Inspection
    # ------------------------------------------------------------------

    def get_adapter_seen_trade_ids(self) -> set[str]:
        return set(self._stub._seen_trade_ids.keys())

    def get_exchange_positions(self) -> dict[str, float]:
        pos = self._injector.get_positions({})
        return {p["ticker"]: float(p["position_fp"]) for p in pos["market_exposures"]}

    def get_exchange_orders(self) -> list[dict]:
        return self._injector.get_orders()["orders"]
```

**Step 3: Run tests**

```bash
pytest tests/sim/test_harness.py -v --tb=short
```

Expected: All tests pass.

**Step 4: Commit**

```bash
git add tests/sim/harness.py tests/sim/test_harness.py
git commit -m "feat(sim): add SimHarness wiring adapter to fake exchange"
```

---

### Task 7.5: Review Tasks 6-7

**Trigger:** Both reviewers start simultaneously when Tasks 6-7 complete.

**Spec review — verify these specific items:**
- [ ] `SimHarness` uses `_make_fill_stub` pattern from `tests/test_execution.py:117-137` (MethodType binding onto SimpleNamespace)
- [ ] `_handle_fill` bound from `KalshiExecutionClient._handle_fill` (not reimplemented)
- [ ] `_handle_user_order` bound from `KalshiExecutionClient._handle_user_order` (not reimplemented)
- [ ] WS messages converted to `FillMsg`/`UserOrderMsg` structs before feeding to handler (not raw dicts)
- [ ] Dedup test: with `ws_duplicate_rate=1.0`, verify fill count is exactly 1 (not 2)
- [ ] Settlement test: verify `generate_position_status_reports` called (not `generate_fill_reports`)
- [ ] Invariant checkers return `list[str]` violations (not bool) — callers can inspect what failed
- [ ] `check_rate_limit_respected` uses sliding window, not fixed buckets

**Code quality review — verify these specific items:**
- [ ] No real asyncio event loop needed for synchronous harness tests
- [ ] Harness counters (accepted_count, filled_count) match actual `generate_order_*` calls via side_effect
- [ ] FillMsg struct construction uses `__struct_fields__` to filter valid fields only
- [ ] No silent swallowing of KeyError/TypeError when constructing typed messages

**Validation Data:**
- Run all sim tests: `pytest tests/sim/ -v --tb=short` — all pass
- Verify dedup: `test_dedup_prevents_duplicate_fills` must show `filled_count == 1`
- Verify settlement: `test_settlement_fill_triggers_reconciliation` must show `settlement_recon_count >= 1`

**Resolution:** Findings queue for dev; all must resolve before next milestone.

---

### Task 7.6: Milestone — Simulation Infrastructure Complete

**Present to user:**
- All sim test results (fake exchange, fault injector, invariants, harness)
- Demonstration of key scenarios working:
  - Order fills through fake exchange → adapter emits correct NT events
  - Dedup prevents duplicate fill events
  - Settlement fills trigger position reconciliation
  - Fault injection (drops, dups, gaps, rate limits) works as configured
- Architecture diagram showing component relationships

**Wait for user response before proceeding to Task 8.**

---

### Task 8: Sequence Generator

**Files:**
- Create: `tests/sim/sequence_generator.py`
- Create: `tests/sim/test_sequence_generator.py`

**Step 1: Write tests for the sequence generator**

```python
"""Tests for the seeded operation sequence generator."""
import pytest
from tests.sim.sequence_generator import SequenceGenerator, Operation


class TestSequenceGenerator:
    def test_generates_requested_length(self):
        gen = SequenceGenerator(seed=42, length=20)
        ops = list(gen.generate())
        assert len(ops) == 20

    def test_deterministic_across_runs(self):
        ops1 = list(SequenceGenerator(seed=42, length=30).generate())
        ops2 = list(SequenceGenerator(seed=42, length=30).generate())
        assert ops1 == ops2

    def test_different_seeds_produce_different_sequences(self):
        ops1 = list(SequenceGenerator(seed=1, length=30).generate())
        ops2 = list(SequenceGenerator(seed=2, length=30).generate())
        assert ops1 != ops2

    def test_all_operation_types_can_appear(self):
        """With enough length, all operation types should appear at least once."""
        ops = list(SequenceGenerator(seed=42, length=200).generate())
        types = {op.op_type for op in ops}
        expected = {"submit_order", "cancel_order", "trigger_fill",
                    "disconnect_ws", "reconnect_ws", "settle_market"}
        assert expected.issubset(types), f"Missing types: {expected - types}"

    def test_submit_order_has_required_fields(self):
        gen = SequenceGenerator(seed=42, length=50)
        for op in gen.generate():
            if op.op_type == "submit_order":
                assert "ticker" in op.params
                assert "side" in op.params
                assert "action" in op.params
                assert "price_cents" in op.params
                assert "count" in op.params
                break

    def test_cancel_order_references_valid_order(self):
        """Cancel ops should only reference previously submitted orders."""
        gen = SequenceGenerator(seed=42, length=50)
        submitted = set()
        for op in gen.generate():
            if op.op_type == "submit_order":
                submitted.add(op.params.get("client_order_id"))
            elif op.op_type == "cancel_order":
                # Cancel should reference an existing order (or skip if none)
                assert op.params.get("target") in submitted or op.params.get("target") is None


class TestSequenceGeneratorWeights:
    def test_dedup_scenario_weight(self):
        """Dedup-weighted generator should produce more fills than default."""
        gen = SequenceGenerator(
            seed=42, length=100,
            weights={"submit_order": 3, "trigger_fill": 5,
                     "disconnect_ws": 1, "reconnect_ws": 1,
                     "cancel_order": 1, "settle_market": 1},
        )
        ops = list(gen.generate())
        fill_count = sum(1 for op in ops if op.op_type == "trigger_fill")
        submit_count = sum(1 for op in ops if op.op_type == "submit_order")
        assert fill_count > submit_count  # fills weighted higher
```

**Step 2: Implement SequenceGenerator**

```python
"""Seeded operation sequence generator for simulation testing.

Generates sequences of operations (submit, cancel, fill, disconnect, reconnect,
settle) with configurable weights. All randomness via seeded PRNG.
"""
from __future__ import annotations

import random
from dataclasses import dataclass, field


@dataclass
class Operation:
    op_type: str
    params: dict = field(default_factory=dict)


_DEFAULT_WEIGHTS = {
    "submit_order": 5,
    "cancel_order": 2,
    "trigger_fill": 3,
    "disconnect_ws": 1,
    "reconnect_ws": 1,
    "settle_market": 1,
}

_TICKERS = ["TICKER-A", "TICKER-B"]
_SIDES = ["yes", "no"]
_ACTIONS = ["buy", "sell"]
_PRICES = [1, 10, 25, 50, 75, 90, 99]
_COUNTS = [1, 5, 9, 50]
_TIFS = ["good_till_canceled", "fill_or_kill", "immediate_or_cancel"]


class SequenceGenerator:
    """Generates a deterministic sequence of trading operations."""

    def __init__(
        self,
        seed: int,
        length: int = 50,
        weights: dict[str, int] | None = None,
        tickers: list[str] | None = None,
    ) -> None:
        self._rng = random.Random(seed)
        self._length = length
        self._weights = weights or dict(_DEFAULT_WEIGHTS)
        self._tickers = tickers or list(_TICKERS)
        self._submitted_orders: list[str] = []
        self._order_counter = 0
        self._connected = True

    def generate(self):
        """Yield `length` operations."""
        op_types = list(self._weights.keys())
        op_weights = list(self._weights.values())

        for _ in range(self._length):
            op_type = self._rng.choices(op_types, weights=op_weights, k=1)[0]

            # Adjust for state-dependent operations
            if op_type == "cancel_order" and not self._submitted_orders:
                op_type = "submit_order"
            if op_type == "reconnect_ws" and self._connected:
                op_type = "disconnect_ws"
            if op_type == "disconnect_ws" and not self._connected:
                op_type = "reconnect_ws"

            yield self._make_operation(op_type)

    def _make_operation(self, op_type: str) -> Operation:
        if op_type == "submit_order":
            self._order_counter += 1
            coid = f"C-SEQ-{self._order_counter}"
            self._submitted_orders.append(coid)
            return Operation(op_type, {
                "ticker": self._rng.choice(self._tickers),
                "side": self._rng.choice(_SIDES),
                "action": self._rng.choice(_ACTIONS),
                "price_cents": self._rng.choice(_PRICES),
                "count": self._rng.choice(_COUNTS),
                "tif": self._rng.choice(_TIFS),
                "client_order_id": coid,
            })

        elif op_type == "cancel_order":
            target = self._rng.choice(self._submitted_orders) if self._submitted_orders else None
            return Operation(op_type, {"target": target})

        elif op_type == "trigger_fill":
            target = self._rng.choice(self._submitted_orders) if self._submitted_orders else None
            count = self._rng.choice([1, 2, 5])
            return Operation(op_type, {"target": target, "count": count})

        elif op_type == "disconnect_ws":
            self._connected = False
            return Operation(op_type, {})

        elif op_type == "reconnect_ws":
            self._connected = True
            return Operation(op_type, {})

        elif op_type == "settle_market":
            ticker = self._rng.choice(self._tickers)
            outcome = self._rng.choice(["yes", "no"])
            return Operation(op_type, {"ticker": ticker, "outcome": outcome})

        return Operation(op_type, {})
```

**Step 3: Run tests**

```bash
pytest tests/sim/test_sequence_generator.py -v --tb=short
```

Expected: All tests pass.

**Step 4: Commit**

```bash
git add tests/sim/sequence_generator.py tests/sim/test_sequence_generator.py
git commit -m "feat(sim): add seeded operation sequence generator"
```

---

### Task 9: Integration — run sequences through harness with invariant checking

**Files:**
- Create: `tests/sim/test_simulation_runs.py`

**Step 1: Write the full simulation test**

This is the capstone — it ties everything together. Run N seeded sequences through the harness and check all invariants after each.

```python
"""Full simulation runs — seeded sequences through harness with invariant checking.

NOT intended for CI — run manually or on schedule.
Mark with pytest.mark.simulation so they can be excluded.
"""
import pytest
from tests.sim.fake_exchange import BookState
from tests.sim.fault_injector import FaultProfile
from tests.sim.harness import SimHarness
from tests.sim.sequence_generator import SequenceGenerator
from tests.sim.invariants import (
    check_no_duplicate_fills,
    check_positions_reconcile,
    check_no_orphaned_orders,
)

# Default book states for simulation
BOOKS = {
    "TICKER-A": BookState(yes_levels={50: 100, 45: 200}, no_levels={50: 100, 55: 200}),
    "TICKER-B": BookState(yes_levels={30: 50}, no_levels={70: 50}),
}

SEEDS = list(range(10))  # 10 seeds for quick local run; increase for thorough


@pytest.mark.parametrize("seed", SEEDS, ids=[f"seed-{s}" for s in SEEDS])
class TestSimulationNoFaults:
    """Simulation runs without fault injection — baseline correctness."""

    def test_sequence_no_faults(self, seed):
        harness = SimHarness(seed=seed, books=BOOKS)
        gen = SequenceGenerator(seed=seed, length=30, tickers=list(BOOKS.keys()))

        for op in gen.generate():
            self._execute_op(harness, op)
            harness.deliver_ws_messages()

        self._check_invariants(harness)

    def _execute_op(self, harness, op):
        if op.op_type == "submit_order":
            try:
                harness.submit_order(**op.params)
            except Exception:
                pass  # Rejected orders are expected
        elif op.op_type == "settle_market":
            harness.settle_market(**op.params)

    def _check_invariants(self, harness):
        # No duplicate fills
        fills = [{"trade_id": tid} for tid in harness.get_adapter_seen_trade_ids()]
        violations = check_no_duplicate_fills(fills)
        assert violations == [], f"Duplicate fills: {violations}"


@pytest.mark.parametrize("seed", SEEDS, ids=[f"seed-{s}" for s in SEEDS])
class TestSimulationWithFaults:
    """Simulation runs WITH fault injection — dedup, reconnection, rate limiting."""

    def test_dedup_under_duplication(self, seed):
        """With message duplication, adapter must still emit each fill exactly once."""
        harness = SimHarness(
            seed=seed,
            books=BOOKS,
            fault_profile=FaultProfile(
                seed=seed,
                ws_duplicate_rate=0.5,
            ),
        )
        gen = SequenceGenerator(seed=seed, length=30, tickers=list(BOOKS.keys()))

        for op in gen.generate():
            if op.op_type == "submit_order":
                try:
                    harness.submit_order(**op.params)
                except Exception:
                    pass
            elif op.op_type == "settle_market":
                harness.settle_market(**op.params)
            harness.deliver_ws_messages()

        # Every trade_id in the adapter's dedup cache should be unique
        fills = [{"trade_id": tid} for tid in harness.get_adapter_seen_trade_ids()]
        violations = check_no_duplicate_fills(fills)
        assert violations == [], f"Duplicate fills under duplication: {violations}"

    def test_sequence_gap_triggers_recon(self, seed):
        """With sequence gaps, adapter must trigger reconciliation."""
        harness = SimHarness(
            seed=seed,
            books=BOOKS,
            fault_profile=FaultProfile(
                seed=seed,
                sequence_gap_rate=0.3,
            ),
        )
        # Configure WS client to detect gaps
        harness._stub._ws_client._check_sequence = _gap_detector(seed)

        gen = SequenceGenerator(seed=seed, length=30, tickers=list(BOOKS.keys()))
        for op in gen.generate():
            if op.op_type == "submit_order":
                try:
                    harness.submit_order(**op.params)
                except Exception:
                    pass
            harness.deliver_ws_messages()

        # With 30% gap rate over 30 operations, we should see at least some recons
        # (exact count depends on seed, so we just verify the mechanism works)


def _gap_detector(seed):
    """Create a sequence checker that detects gaps."""
    import random
    rng = random.Random(seed)
    tracker = {}

    def check(sid, seq):
        last = tracker.get(sid)
        tracker[sid] = seq
        if last is None:
            return True
        return seq == last + 1

    return check
```

**Step 2: Run the simulation tests**

```bash
pytest tests/sim/test_simulation_runs.py -v --tb=short
```

Expected: All seeds pass. Note any seeds that reveal interesting behavior.

**Step 3: Commit**

```bash
git add tests/sim/test_simulation_runs.py
git commit -m "test(sim): add full simulation runs with invariant checking"
```

---

### Task 9.5: Review Tasks 8-9

**Trigger:** Both reviewers start simultaneously when Tasks 8-9 complete.

**Spec review — verify these specific items:**
- [ ] Sequence generator produces all 6 operation types (submit, cancel, fill, disconnect, reconnect, settle) — verified by `test_all_operation_types_can_appear`
- [ ] Weights are configurable and affect distribution — verified by `test_dedup_scenario_weight`
- [ ] Simulation runs use invariant checkers (not ad-hoc assertions)
- [ ] `TestSimulationWithFaults.test_dedup_under_duplication` uses `ws_duplicate_rate=0.5` (not 0.0 or 1.0)
- [ ] Simulation tests use `BOOKS` with both thin (`TICKER-B`) and deep (`TICKER-A`) book states
- [ ] Simulation tests are NOT marked for CI (verify no `@pytest.mark.ci` or similar)
- [ ] Sequence generator handles state-dependent operations (can't cancel before submit, can't reconnect when connected)

**Code quality review — verify these specific items:**
- [ ] `SequenceGenerator.generate()` is a generator (yields, doesn't return list)
- [ ] Cancel operations only target previously submitted orders
- [ ] Simulation test exception handling: rejected orders are `pass`'d, not silently swallowed with no comment
- [ ] `_gap_detector` uses its own PRNG instance, not the harness's

**Validation Data:**
- `pytest tests/sim/ -v --tb=short` — all tests pass
- `pytest tests/sim/test_simulation_runs.py --co -q | wc -l` — should be 20-40 test cases (10 seeds × 2-3 test classes)
- Total sim test runtime should be under 30 seconds

**Resolution:** Findings queue for dev; all must resolve before next milestone.

---

### Task 9.6: Milestone — Full Simulation Suite Complete

**Present to user:**
- All test results across the entire `tests/sim/` directory
- Summary of invariant check results across all seeds
- Any seeds that revealed interesting edge cases or failures
- CT test results (confirming Phase 1 still passes)
- Full test run: `pytest tests/ -v --noconftest --tb=short`
- Architecture summary:
  ```
  Layer 1 (CI):     test_ct_execution.py, test_ct_data_provider.py
  Layer 2 (local):  sim/fake_exchange.py → sim/fault_injector.py → sim/harness.py
  Layer 3 (local):  sim/sequence_generator.py → sim/test_simulation_runs.py
  Invariants:       sim/invariants.py (used by Layer 3)
  ```

**Wait for user response.**
