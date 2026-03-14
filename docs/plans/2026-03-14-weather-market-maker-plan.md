# Weather Market Maker Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use forge:executing-plans to implement this plan task-by-task.

**Goal:** Build a two-layer KXHIGH trading system — forecast filter narrows to high-conviction markets, then passive market making with layered quotes extracts value.

**Architecture:** SignalActor consumes win probabilities from an external signal server via WebSocket, publishes custom `SignalScore` / `ForecastDrift` data events on the NT MessageBus. WeatherMakerStrategy subscribes to those events plus Kalshi QuoteTicks, runs filter → quote ladder → risk management → exit pipeline. BacktestEngine replays historical signals + market data for parameter tuning.

**Tech Stack:** NautilusTrader (BacktestEngine, Actor, Strategy, `@customdataclass`), websockets, httpx (signal server HTTP), msgspec, existing Kalshi adapter.

**Required Skills:**
- `writing-tests`: Invoke before every implementation task — TDD throughout
- `escalate-not-accommodate`: Invoke during error handling implementation — never silently drop financial events
- `kalshi-weather-markets`: Invoke before Task 4 (quote manager) — KXHIGH microstructure specifics
- `geek-finance`: Invoke before Task 6 (risk manager) — position sizing, exposure management
- `kalshi-tails`: Invoke before Task 4 (filter layer) — multi-model consensus, hours-based margins

## Enhancement Summary

**Deepened on:** 2026-03-14
**Agents used:** 13 (architecture-strategist, security-sentinel, performance-oracle, error-propagation-reviewer, python-reviewer, code-simplicity-reviewer, pattern-recognition-specialist, spec-flow-analyzer, kalshi-tails methodology, kalshi-weather-markets domain, nautilus-testing patterns, framework-docs-researcher, best-practices-researcher)

### Blocking Issues (system won't work without fixes)

1. **Config syntax crash** — `@dataclass(frozen=True)` on msgspec.Struct subclasses (ActorConfig, StrategyConfig) crashes at import. Must use `class Config(StrategyConfig, frozen=True):` keyword form. See `kalshi/config.py` for the correct pattern.

2. **No QuoteTick subscription** — `on_start()` subscribes to SignalScore and ForecastDrift but never calls `subscribe_quote_ticks()`. The strategy will never receive tick data and never place orders. Must add: when `_evaluate_contract()` adds a ticker to `_quoted_tickers`, resolve the instrument from cache and call `self.subscribe_quote_ticks(instrument_id)`. Use `cache.add_instrument()` before subscribing (StreamingFeatherWriter requirement).

3. **ExposureTracker is append-only** — `_exposure.add()` is called at order submission but `remove()` is never called on cancel, fill, or exit. After one reprice cycle, phantom exposure accumulates and `check_risk_caps()` returns 0 for everything. **Resolution: delete ExposureTracker entirely. Derive exposure from `self.cache.positions()` and `self.cache.orders_open()` on each risk check.** This is always correct, eliminates drift by construction, and saves ~60 LOC. The NT cache is the single source of truth for position state.

### Critical Issues (financial risk)

4. **No circuit breaker / kill switch** — No drawdown limit, no daily loss cap, no halt file. Add to config: `max_drawdown_pct: float = 0.15`, `halt_file_path: str = "/tmp/kalshi-halt"`. Check at top of `on_quote_tick()` and `_evaluate_contract()`. On trigger: cancel all orders, set `_halted = True`, log CRITICAL. Do NOT close positions (hold to settlement per kalshi-tails principle).

5. **No model agreement check** — `should_quote()` checks `n_models >= min_models` but ignores spread between models. Add `max_model_spread: float = 0.15` to config. Compute `spread = max(emos_no, ngboost_no, drn_no) - min(emos_no, ngboost_no, drn_no)` (excluding zero values). Reject if spread exceeds threshold. The per-model scores exist in SignalScore but are never used by the filter.

6. **Fire-and-forget WS task** — `self._loop.create_task(self._run_ws())` without `done_callback` means signal pipeline death is silent. Add `self._ws_task.add_done_callback(self._on_ws_task_done)` following the existing pattern at `kalshi/websocket/client.py:121-122`. The callback should log CRITICAL and set a flag the strategy can check.

7. **No re-bootstrap after WS reconnect** — `_bootstrap()` runs once before the WS loop. After reconnect, scores are stale until the next server push. Move `_bootstrap()` inside the reconnect loop, after subscribing.

8. **FOK exit = stuck position on rejection** — If the FOK sell is killed (no liquidity), resting orders are already canceled, position is open, no retry mechanism. Use IOC instead of FOK (partial exit better than no exit), add exit-in-progress flag, retry on next tick if position still open.

9. **Today vs tomorrow contracts** — KXHIGH has two distinct regimes. Tomorrow contracts have 21c mean opening moves, need delayed entry. Add `status: str = ""` and derive cohort from ticker date. Add `tomorrow_min_age_minutes: int = 30` to config. Gate new ladders on tomorrow contracts until price discovery settles.

10. **No time-of-day gating** — Strategy quotes during volatile 10:00-10:30 ET open when spreads are widest. Add `entry_phase_start_et: str = "10:30"` and `entry_phase_end_et: str = "15:00"` to config. Before `entry_phase_start_et`, honor exit logic only.

11. **OmsType mismatch** — Backtest uses `NETTING`, live adapter uses `HEDGING` (YES/NO are separate instruments). Change backtest to `OmsType.HEDGING` to match live, or backtest results are invalid.

### High Priority Issues

12. **Backtest OOM** — `load_catalog_data()` loads entire 23GB catalog. Add `start`/`end` datetime parameters, pass through to `catalog.quote_ticks()` for predicate pushdown. Without this, backtesting is impossible.

13. **Rate limit over-counting** — Cancel operations charge 1.0 token in the execution client but Kalshi's actual cost is 0.2 (per `WRITE_COST_CANCEL` in constants.py). Pass `cost=WRITE_COST_CANCEL` to `acquire()` in `_cancel_order`. This wastes 67% of rate limit capacity.

14. **No input validation** — `parse_score_msg()` trusts all signal server data. Validate: `0 <= p_win <= 1`, `1 <= n_models <= 3`, `0 <= yes_bid <= 99`. Return `None` for invalid messages, log WARNING. Financial system must reject malformed inputs.

15. **No heartbeat/ping** — SignalActor never sends pings to signal server. Add timer in `on_start()` sending `{"type": "ping"}` every 30s. Track pong responses; if 2 missed, close and reconnect.

16. **Contract status missing** — Design doc requires status="open" check but SignalScore has no `status` field. Add `status: str = ""` to SignalScore, capture in `parse_score_msg()`, check in `should_quote()`.

17. **`_handle_ws_message` has no try/except** — Malformed JSON kills the entire WS task permanently. Add try/except around message parsing body (log + continue, not swallow), following `kalshi/data.py` pattern.

18. **Drift auto-clear defeats purpose** — New score immediately clears drift, but drift alerts indicate forecast instability. Remove auto-clear. Let drift persist until session end or explicit signal server "drift_cleared" message.

19. **Fragile ticker parsing** — `on_quote_tick()` uses `rsplit("-", 1)` instead of the existing `parse_instrument_id()` from `kalshi/providers.py`. Reuse the existing utility (DRY, handles format changes).

### Simplification Opportunities (YAGNI)

20. **Drift response modes** — Start with "pause" only. Remove "tighten" and "ignore" modes (speculative, untested). Eliminates `drift_response` and `drift_threshold_delta` config fields, ~25 LOC.

21. **LadderLevel dataclass** — Replace with `tuple[int, int]`. A frozen dataclass wrapping two ints is unnecessary. `compute_ladder()` returns `list[tuple[int, int]]`, caller destructures.

22. **Integration test deferral** — Replace `MockSignalServer` + full integration test with synthetic-data strategy test (feed `SignalScore` + mock QuoteTick directly). Defer full integration to after demo deployment. Saves ~100 LOC of test infrastructure.

23. **Use `self._config` not `self._cfg`** — Match existing adapter convention (`data.py:92`, `execution.py:165`).

24. **`_latest_scores` in SignalActor** — Populated but never read. Remove or document purpose.

25. **`on_dispose` override** — Default no-op exists in Cython base class. Remove `pass` override.

### Additional Config Parameters (from domain skills)

```python
class WeatherMakerConfig(StrategyConfig, frozen=True):
    # Filter
    confidence_threshold: float = 0.95
    min_models: int = 2
    max_model_spread: float = 0.15       # NEW: max spread between per-model probabilities

    # Quote ladder
    ladder_depth: int = 3
    ladder_spacing: int = 1
    level_quantity: int = 10
    reprice_threshold: int = 1
    max_entry_cents: int = 96            # NEW: hard cap on anchor price (from weather markets skill)

    # Risk
    market_cap_pct: float = 0.20
    city_cap_pct: float = 0.33

    # Exit
    exit_price_cents: int = 97

    # Time-of-day gating
    entry_phase_start_et: str = "10:30"  # NEW: earliest ET to place ladders
    entry_phase_end_et: str = "15:00"    # NEW: latest ET to place ladders
    tomorrow_min_age_minutes: int = 30   # NEW: delay for tomorrow contract entry

    # Circuit breaker
    max_drawdown_pct: float = 0.15       # NEW: halt if balance drops this fraction
    halt_file_path: str = "/tmp/kalshi-halt"  # NEW: manual kill switch

    # Strategy identity
    strategy_id: str = "WeatherMaker-001"
```

### Key Pattern Corrections

| Plan Says | Should Be | Reason |
|-----------|-----------|--------|
| `@dataclass(frozen=True) class Config(StrategyConfig):` | `class Config(StrategyConfig, frozen=True):` | msgspec.Struct keyword form |
| `self._cfg = config` | `self._config = config` | Match adapter convention |
| `websockets.WebSocketClientProtocol` | `websockets.asyncio.client.ClientConnection` | Deprecated in websockets v16 |
| `json.loads(raw)` in SignalActor | msgspec typed decoder | 5-10x faster, consistent with adapter |
| `TimeInForce.FOK` for exit | `TimeInForce.IOC` | Partial exit > no exit on thin books |
| `self.cancel_all_orders(self.id)` | Verify NT API — may need `self.cancel_all_orders()` | API may not take strategy_id |
| `list(balances.values())[0].total` | `account.balance(USD).total` | Explicit currency lookup |
| `OmsType.NETTING` in backtest | `OmsType.HEDGING` | Match live adapter |
| Test file `tests/test_integration.py` | `tests/test_maker_integration.py` | File already exists |

## Context for Executor

### Key Files
- `kalshi/config.py:1-58` — Config pattern to follow for new configs (frozen dataclass, env-aware URLs)
- `kalshi/factories.py:1-75` — Factory singleton pattern (`_SHARED_PROVIDER`), must reset between tests
- `kalshi/data.py:1-220` — KalshiDataClient lifecycle (connect, subscribe, WS handling) — follow for SignalActor
- `kalshi/execution.py:111-133` — TokenBucket rate limiter (strategy must respect this)
- `kalshi/execution.py:233-289` — Order submission pattern (_submit_order flow)
- `kalshi/providers.py:101-160` — `_build_instrument()` creates BinaryOption, `parse_instrument_id()` splits ticker/side
- `kalshi/websocket/client.py:1-143` — WS client pattern (connect, subscribe, reconnect)
- `kalshi/websocket/types.py:1-82` — msgspec Struct pattern for WS messages
- `kalshi/common/constants.py:1-22` — KALSHI_VENUE, URLs, rate limit costs
- `collector.py:34-172` — KalshiDiscoveryStrategy lifecycle (on_start, timers, background thread for blocking calls, cache.add_instrument before subscribing)
- `tests/mock_exchange/strategy.py:1-172` — ConfidenceTestStrategy order placement patterns (market, limit, cancel-and-replace)
- `tests/mock_exchange/server.py:42-476` — MockKalshiServer dual-server design (REST + WS)
- `tests/test_live_confidence.py:133-236` — TradingNode boot against mock server, scenario execution
- `archive/strategy.py:1-150` — KalshiSpreadCaptureStrategy spread capture patterns (position checks, order tracking)
- `experiments/exp05_backtest_phased.py:1-109` — BacktestEngine setup pattern, result extraction

### Research Findings

**NT Custom Data Types (`@customdataclass`):**
```python
from nautilus_trader.core.data import Data
from nautilus_trader.model.custom import customdataclass

@customdataclass
class MyData(Data):
    field: str = ""
    value: float = 0.0
    # ts_event and ts_init auto-generated as constructor params
```
- Auto-generates: `__init__`, `to_dict`/`from_dict`, `to_bytes`/`from_bytes`, `to_arrow`/`from_arrow`, `_schema`
- Auto-registers with `register_serializable_type` and `register_arrow` at import time
- Supported field types: `str`, `float`, `int`, `bool`, `bytes`, `InstrumentId`
- `ParquetDataCatalog.write_data([...])` works out of the box after `@customdataclass`
- `catalog.custom_data(cls=MyData)` returns `CustomData` wrappers — access original via `.data`

**Actor Publish Pattern:**
```python
from nautilus_trader.model.data import DataType
self.publish_data(DataType(SignalScore), score)  # in Actor
```

**Strategy Subscribe Pattern:**
```python
def on_start(self):
    self.subscribe_data(DataType(SignalScore))  # subscribes to all SignalScore events
def on_data(self, data):
    if isinstance(data, SignalScore):  # callback for custom data
        ...
```

**BacktestEngine Custom Data:**
```python
engine.add_data(signal_scores_list, client_id=ClientId("SIGNAL"), sort=False)
engine.add_data(quote_ticks_list, sort=False)
engine.sort_data()
```

**Kalshi Contract Model:**
- YES and NO are separate instruments with independent order books
- Prices in cents (1-99). Adapter converts to 0.01-0.99 internally
- `instrument.make_qty(n)` → `Quantity`, `instrument.make_price(dollars)` → `Price`
- Rate limit: 10 writes/sec (TokenBucket in execution client)
- Cancel cost: 0.2 tokens (vs 1.0 for create)
- No order modification — must cancel-and-replace
- `cache.add_instrument(inst)` required before any data subscription (StreamingFeatherWriter silently drops otherwise)

**Signal Server API (key endpoints):**
- `GET /v1/trading/scores` — batch scoring, returns array of `{ticker, city, threshold, direction, no_p_win, yes_p_win, no_margin, n_models, emos_no, ngboost_no, drn_no, yes_bid, yes_ask, status}`
- `GET /v1/trading/scores/backfill?lead_idx=1&start_date=2026-01-01` — historical scores for backtesting
- `ws://localhost:8000/v1/stream` — subscribe to `["scores", "alerts"]` channels
- Score WS messages contain same fields as REST `/v1/trading/scores`
- Alert messages: `{type: "alert", alert_type: "forecast_drift", city, date, message, details}`
- Ping/pong: send `{"type": "ping"}`, receive `{"type": "pong"}`

### Relevant Patterns
- `collector.py:34-44` — Actor on_start pattern (capture event loop, set timers)
- `tests/mock_exchange/strategy.py:97-123` — Order factory usage (market + limit orders)
- `archive/strategy.py:70-100` — Position checking via cache, cancel-and-replace flow
- `kalshi/data.py:107-115` — DataClient _connect pattern (initialize provider, publish instruments)

## Execution Architecture

**Team:** 2 devs, 1 spec reviewer, 1 quality reviewer
**Task dependencies:**
  - Tasks 1-3 are sequential (data types → actor → actor tests depend on each other)
  - Tasks 4-5 (filter layer) depend on Task 1 (data types)
  - Tasks 6-7 (quote manager) depend on Tasks 4-5 (filter layer)
  - Tasks 8-9 (risk + exit) depend on Tasks 6-7 (quote manager)
  - Tasks 10-11 (backtest loader) are independent of Tasks 4-9 (can run in parallel after Task 1)
  - Tasks 12-13 (backtest harness) depend on Tasks 8-9 + Tasks 10-11
  - Tasks 14-15 (integration test) depend on all prior tasks
**Phases:**
  - Phase 1: Tasks 1-3 — Custom data types + SignalActor
  - Phase 2: Tasks 4-9 — Strategy (filter, quote manager, risk, exit)
  - Phase 3: Tasks 10-13 — Backtesting infrastructure
  - Phase 4: Tasks 14-15 — Integration testing
**Milestones:**
  - After Phase 1 (Task 3) — SignalActor publishes data, verified with unit tests
  - After Phase 2 (Task 9) — Full strategy logic, verified with mock data
  - After Phase 3 (Task 13) — Backtest runs end-to-end
  - After Phase 4 (Task 15) — Full integration verified

---

## Phase 1: Custom Data Types + SignalActor

### Task 1: Implement custom data types (SignalScore, ForecastDrift)

**Files:**
- Create: `kalshi/signals.py`
- Test: `tests/test_signals.py`

**Step 1: Write failing tests for SignalScore and ForecastDrift serialization**

```python
# tests/test_signals.py
"""Tests for custom signal data types."""
import pytest
from nautilus_trader.core.data import Data
from nautilus_trader.model.data import DataType

from kalshi.signals import ForecastDrift, SignalScore


class TestSignalScore:
    def test_is_data_subclass(self):
        score = SignalScore(
            ticker="KXHIGHNY-26MAR15-T54",
            city="new_york",
            threshold=54.0,
            direction="above",
            no_p_win=0.983,
            yes_p_win=0.017,
            no_margin=7.2,
            n_models=3,
            emos_no=0.952,
            ngboost_no=0.991,
            drn_no=0.988,
            yes_bid=85,
            yes_ask=90,
            ts_event=1_000_000_000,
            ts_init=1_000_000_000,
        )
        assert isinstance(score, Data)

    def test_roundtrip_dict(self):
        score = SignalScore(
            ticker="KXHIGHNY-26MAR15-T54",
            city="new_york",
            threshold=54.0,
            direction="above",
            no_p_win=0.983,
            yes_p_win=0.017,
            no_margin=7.2,
            n_models=3,
            emos_no=0.952,
            ngboost_no=0.991,
            drn_no=0.988,
            yes_bid=85,
            yes_ask=90,
            ts_event=1_000_000_000,
            ts_init=1_000_000_000,
        )
        d = score.to_dict()
        restored = SignalScore.from_dict(d)
        assert restored.ticker == "KXHIGHNY-26MAR15-T54"
        assert restored.city == "new_york"
        assert restored.no_p_win == pytest.approx(0.983)
        assert restored.n_models == 3
        assert restored.yes_bid == 85
        assert restored.ts_event == 1_000_000_000

    def test_roundtrip_bytes(self):
        score = SignalScore(
            ticker="KXHIGHNY-26MAR15-T54",
            city="new_york",
            threshold=54.0,
            direction="above",
            no_p_win=0.983,
            yes_p_win=0.017,
            no_margin=7.2,
            n_models=3,
            emos_no=0.952,
            ngboost_no=0.991,
            drn_no=0.988,
            yes_bid=85,
            yes_ask=90,
            ts_event=1_000_000_000,
            ts_init=1_000_000_000,
        )
        raw = score.to_bytes()
        restored = SignalScore.from_bytes(raw)
        assert restored.ticker == "KXHIGHNY-26MAR15-T54"
        assert restored.no_p_win == pytest.approx(0.983)

    def test_data_type_creation(self):
        dt = DataType(SignalScore)
        assert dt.type == SignalScore

    def test_winning_side_no(self):
        """When no_p_win > yes_p_win, NO is the winning side."""
        score = SignalScore(
            ticker="KXHIGHNY-26MAR15-T54",
            city="new_york",
            threshold=54.0,
            direction="above",
            no_p_win=0.983,
            yes_p_win=0.017,
            no_margin=7.2,
            n_models=3,
            emos_no=0.952,
            ngboost_no=0.991,
            drn_no=0.988,
            yes_bid=85,
            yes_ask=90,
            ts_event=0,
            ts_init=0,
        )
        assert score.no_p_win > score.yes_p_win


class TestForecastDrift:
    def test_is_data_subclass(self):
        drift = ForecastDrift(
            city="new_york",
            date="2026-03-15",
            message="ECMWF shifted +2.3F in 30min",
            ts_event=1_000_000_000,
            ts_init=1_000_000_000,
        )
        assert isinstance(drift, Data)

    def test_roundtrip_dict(self):
        drift = ForecastDrift(
            city="new_york",
            date="2026-03-15",
            message="ECMWF shifted +2.3F in 30min",
            ts_event=1_000_000_000,
            ts_init=1_000_000_000,
        )
        d = drift.to_dict()
        restored = ForecastDrift.from_dict(d)
        assert restored.city == "new_york"
        assert restored.date == "2026-03-15"
        assert restored.message == "ECMWF shifted +2.3F in 30min"
```

**Step 2: Run tests to verify they fail**

Run: `uv run python -m pytest tests/test_signals.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'kalshi.signals'`

**Step 3: Implement SignalScore and ForecastDrift**

```python
# kalshi/signals.py
"""Custom NT data types for signal server integration."""
from nautilus_trader.core.data import Data
from nautilus_trader.model.custom import customdataclass


@customdataclass
class SignalScore(Data):
    """Score for a single KXHIGH contract from the signal server ensemble."""

    ticker: str = ""
    city: str = ""
    threshold: float = 0.0
    direction: str = ""       # "above" | "below"
    no_p_win: float = 0.0     # ensemble NO probability
    yes_p_win: float = 0.0    # ensemble YES probability
    no_margin: float = 0.0    # forecast distance from threshold (positive = safe for NO)
    n_models: int = 0         # 1-3, quality indicator
    emos_no: float = 0.0      # EMOS model NO probability
    ngboost_no: float = 0.0   # NGBoost model NO probability
    drn_no: float = 0.0       # DRN model NO probability
    yes_bid: int = 0          # current market YES bid in cents
    yes_ask: int = 0          # current market YES ask in cents


@customdataclass
class ForecastDrift(Data):
    """Alert when forecast shifts significantly for a city."""

    city: str = ""
    date: str = ""
    message: str = ""
```

**Step 4: Run tests to verify they pass**

Run: `uv run python -m pytest tests/test_signals.py -v`
Expected: All 6 tests PASS

**Step 5: Commit**

```bash
git add kalshi/signals.py tests/test_signals.py
git commit -m "feat: add SignalScore and ForecastDrift custom data types"
```

---

### Task 2: Implement SignalActor

**Files:**
- Create: `kalshi/signal_actor.py`
- Test: `tests/test_signal_actor.py`

**Step 1: Write failing tests for SignalActor**

The SignalActor needs to:
1. Connect to signal server WS on start, subscribe to `["scores", "alerts"]`
2. Bootstrap initial state from REST `GET /v1/trading/scores`
3. Parse incoming WS messages into SignalScore / ForecastDrift events
4. Publish events on the NT MessageBus via `self.publish_data()`

Test the parsing and publish logic in isolation (mock the WS/HTTP connections):

```python
# tests/test_signal_actor.py
"""Tests for SignalActor message parsing and event publishing."""
import json

import pytest

from kalshi.signal_actor import SignalActorConfig, parse_score_msg, parse_alert_msg
from kalshi.signals import ForecastDrift, SignalScore


class TestParseScoreMsg:
    """Test parsing signal server score messages into SignalScore."""

    def test_parse_valid_score(self):
        msg = {
            "ticker": "KXHIGHNY-26MAR15-T54",
            "city": "new_york",
            "threshold": 54.0,
            "direction": "above",
            "no_p_win": 0.9834,
            "yes_p_win": 0.0166,
            "no_margin": 7.2,
            "n_models": 3,
            "emos_no": 0.952,
            "ngboost_no": 0.991,
            "drn_no": 0.988,
            "yes_bid": 85,
            "yes_ask": 90,
            "status": "open",
        }
        ts = 1_000_000_000
        score = parse_score_msg(msg, ts)
        assert isinstance(score, SignalScore)
        assert score.ticker == "KXHIGHNY-26MAR15-T54"
        assert score.city == "new_york"
        assert score.no_p_win == pytest.approx(0.9834)
        assert score.n_models == 3
        assert score.yes_bid == 85

    def test_parse_score_missing_optional_models(self):
        """When n_models < 3, missing model scores default to 0.0."""
        msg = {
            "ticker": "KXHIGHNY-26MAR15-T54",
            "city": "new_york",
            "threshold": 54.0,
            "direction": "above",
            "no_p_win": 0.95,
            "yes_p_win": 0.05,
            "no_margin": 5.0,
            "n_models": 1,
            "emos_no": 0.95,
            "yes_bid": 80,
            "yes_ask": 85,
            "status": "open",
        }
        ts = 1_000_000_000
        score = parse_score_msg(msg, ts)
        assert score.n_models == 1
        assert score.emos_no == pytest.approx(0.95)
        assert score.ngboost_no == 0.0
        assert score.drn_no == 0.0


class TestParseAlertMsg:
    """Test parsing signal server alert messages into ForecastDrift."""

    def test_parse_forecast_drift(self):
        msg = {
            "type": "alert",
            "alert_type": "forecast_drift",
            "city": "new_york",
            "date": "2026-03-15",
            "message": "ECMWF shifted +2.3F in 30min",
            "details": {},
            "timestamp": "2026-03-14T12:40:00Z",
        }
        ts = 1_000_000_000
        drift = parse_alert_msg(msg, ts)
        assert isinstance(drift, ForecastDrift)
        assert drift.city == "new_york"
        assert drift.date == "2026-03-15"
        assert drift.message == "ECMWF shifted +2.3F in 30min"

    def test_non_drift_alert_returns_none(self):
        """Non-forecast_drift alerts are ignored."""
        msg = {
            "type": "alert",
            "alert_type": "other_alert",
            "city": "new_york",
            "date": "2026-03-15",
            "message": "something else",
            "details": {},
            "timestamp": "2026-03-14T12:40:00Z",
        }
        ts = 1_000_000_000
        result = parse_alert_msg(msg, ts)
        assert result is None


class TestSignalActorConfig:
    def test_defaults(self):
        cfg = SignalActorConfig(
            signal_ws_url="ws://localhost:8000/v1/stream",
            signal_http_url="http://localhost:8000",
        )
        assert cfg.signal_ws_url == "ws://localhost:8000/v1/stream"
        assert cfg.signal_http_url == "http://localhost:8000"
```

**Step 2: Run tests to verify they fail**

Run: `uv run python -m pytest tests/test_signal_actor.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'kalshi.signal_actor'`

**Step 3: Implement SignalActor**

```python
# kalshi/signal_actor.py
"""SignalActor — consumes signal server WebSocket, publishes NT data events."""
from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass

import httpx
import websockets

from nautilus_trader.common.actor import Actor
from nautilus_trader.config import ActorConfig
from nautilus_trader.model.data import DataType

from kalshi.signals import ForecastDrift, SignalScore

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class SignalActorConfig(ActorConfig):
    """Configuration for the SignalActor."""

    signal_ws_url: str = "ws://localhost:8000/v1/stream"
    signal_http_url: str = "http://localhost:8000"
    component_id: str = "SignalActor-001"


def parse_score_msg(msg: dict, ts_ns: int) -> SignalScore:
    """Parse a signal server score dict into a SignalScore data event."""
    return SignalScore(
        ticker=msg["ticker"],
        city=msg["city"],
        threshold=float(msg["threshold"]),
        direction=msg["direction"],
        no_p_win=float(msg["no_p_win"]),
        yes_p_win=float(msg["yes_p_win"]),
        no_margin=float(msg["no_margin"]),
        n_models=int(msg["n_models"]),
        emos_no=float(msg.get("emos_no", 0.0)),
        ngboost_no=float(msg.get("ngboost_no", 0.0)),
        drn_no=float(msg.get("drn_no", 0.0)),
        yes_bid=int(msg.get("yes_bid", 0)),
        yes_ask=int(msg.get("yes_ask", 0)),
        ts_event=ts_ns,
        ts_init=ts_ns,
    )


def parse_alert_msg(msg: dict, ts_ns: int) -> ForecastDrift | None:
    """Parse a signal server alert into a ForecastDrift, or None if not a drift alert."""
    if msg.get("alert_type") != "forecast_drift":
        return None
    return ForecastDrift(
        city=msg["city"],
        date=msg["date"],
        message=msg["message"],
        ts_event=ts_ns,
        ts_init=ts_ns,
    )


class SignalActor(Actor):
    """Consumes signal server WebSocket and publishes SignalScore/ForecastDrift on the MessageBus."""

    def __init__(self, config: SignalActorConfig) -> None:
        super().__init__(config)
        self._ws_url = config.signal_ws_url
        self._http_url = config.signal_http_url
        self._ws: websockets.WebSocketClientProtocol | None = None
        self._ws_task: asyncio.Task | None = None
        self._score_data_type = DataType(SignalScore)
        self._drift_data_type = DataType(ForecastDrift)
        self._latest_scores: dict[str, SignalScore] = {}

    def on_start(self) -> None:
        self._loop = asyncio.get_running_loop()
        self._ws_task = self._loop.create_task(self._run_ws())

    def on_stop(self) -> None:
        if self._ws_task and not self._ws_task.done():
            self._ws_task.cancel()
        if self._ws:
            self._loop.create_task(self._ws.close())

    async def _bootstrap(self) -> None:
        """Fetch initial scores from REST endpoint."""
        url = f"{self._http_url}/v1/trading/scores"
        async with httpx.AsyncClient() as client:
            resp = await client.get(url, timeout=30.0)
            resp.raise_for_status()
            scores = resp.json()

        ts = self.clock.timestamp_ns()
        for item in scores:
            score = parse_score_msg(item, ts)
            self._latest_scores[score.ticker] = score
            self.publish_data(self._score_data_type, score)

        self.log.info(f"Bootstrapped {len(scores)} scores from REST")

    async def _run_ws(self) -> None:
        """Connect to signal server WebSocket and process messages."""
        await self._bootstrap()

        async for ws in websockets.connect(self._ws_url):
            self._ws = ws
            try:
                subscribe_msg = json.dumps({
                    "type": "subscribe",
                    "channels": ["scores", "alerts"],
                    "cities": [],
                })
                await ws.send(subscribe_msg)
                self.log.info("Subscribed to signal server [scores, alerts]")

                async for raw in ws:
                    self._handle_ws_message(raw)

            except websockets.ConnectionClosed:
                self.log.warning("Signal server WS disconnected, reconnecting...")
                continue
            except Exception as e:
                self.log.error(f"Signal server WS error: {e}")
                raise

    def _handle_ws_message(self, raw: str | bytes) -> None:
        """Route incoming WS message to appropriate handler."""
        msg = json.loads(raw)
        msg_type = msg.get("type", "")
        ts = self.clock.timestamp_ns()

        if msg_type == "scores_update":
            contracts = msg.get("contracts", msg.get("scores", []))
            for item in contracts:
                score = parse_score_msg(item, ts)
                self._latest_scores[score.ticker] = score
                self.publish_data(self._score_data_type, score)

        elif msg_type == "alert":
            drift = parse_alert_msg(msg, ts)
            if drift is not None:
                self.publish_data(self._drift_data_type, drift)

        elif msg_type == "pong":
            pass  # heartbeat response, ignore

        else:
            self.log.debug(f"Unhandled signal server message type: {msg_type}")

    def on_dispose(self) -> None:
        pass
```

**Step 4: Run tests to verify they pass**

Run: `uv run python -m pytest tests/test_signal_actor.py -v`
Expected: All 5 tests PASS

**Step 5: Commit**

```bash
git add kalshi/signal_actor.py tests/test_signal_actor.py
git commit -m "feat: add SignalActor for signal server integration"
```

---

### Task 3: Review Tasks 1-2

**Trigger:** Both reviewers start simultaneously when Tasks 1-2 complete.

**Spec review — verify these specific items:**
- [ ] `SignalScore` fields match the signal server `/v1/trading/scores` response schema exactly — compare field names and types against the API reference in the design doc
- [ ] `ForecastDrift` captures `city`, `date`, `message` from the signal server alert schema
- [ ] `@customdataclass` decorator is applied (not manual Data subclass) — verify `to_dict`, `from_dict`, `to_bytes`, `from_bytes` all auto-generated
- [ ] `parse_score_msg` handles missing optional model fields (`ngboost_no`, `drn_no`) with `.get(key, 0.0)` — signal server sends partial scores when `n_models < 3`
- [ ] `SignalActor.on_start()` captures the event loop and creates the WS task — follow the pattern in `collector.py:34-44`
- [ ] `SignalActor._bootstrap()` calls REST `/v1/trading/scores` and publishes one `SignalScore` per contract — verify the URL path matches the API reference
- [ ] `SignalActor._handle_ws_message()` routes `scores_update` and `alert` types — confirm the WS message `type` field values match signal server documentation
- [ ] No extra features beyond parsing and publishing (no filtering, no trading logic in the Actor)

**Code quality review — verify these specific items:**
- [ ] Error handling in `_run_ws()` uses `websockets.connect()` async iterator for auto-reconnect — does NOT silently swallow connection errors
- [ ] `_bootstrap()` calls `resp.raise_for_status()` — HTTP errors propagate, not silently ignored
- [ ] Test assertions check specific field values (ticker, city, no_p_win), not just `isinstance` checks
- [ ] `SignalActorConfig` inherits from `ActorConfig` (not raw dataclass) — required for NT component registration
- [ ] No import of `kalshi.signals` types at module level causes registration conflicts — `@customdataclass` registers at import time, verify no double-registration issues

**Validation Data:**
- Compare `SignalScore` fields against the signal server API reference: `ticker, city, threshold, direction, no_p_win, yes_p_win, no_margin, n_models, emos_no, ngboost_no, drn_no, yes_bid, yes_ask`
- Compare `ForecastDrift` fields against alert schema: `city, date, message`
- Verify `parse_score_msg` output matches input dict values exactly (use pytest.approx for floats)

**Resolution:** Findings queue for dev; all must resolve before next milestone.

---

### Task 3.5: Milestone — Phase 1 Complete

**Present to user:**
- SignalScore and ForecastDrift custom data types implemented with `@customdataclass`
- SignalActor connects to signal server WS, bootstraps from REST, publishes events on MessageBus
- Review findings and resolutions
- Test results

**Wait for user response before proceeding to Phase 2.**

---

## Phase 2: Strategy — Filter, Quote Manager, Risk, Exit

### Task 4: Implement WeatherMakerStrategy config and filter layer

**Files:**
- Create: `kalshi/strategy.py`
- Test: `tests/test_strategy.py`

**Step 1: Write failing tests for the filter layer**

The filter decides which contracts are quotable. Test it in isolation with synthetic SignalScore data.

```python
# tests/test_strategy.py
"""Tests for WeatherMakerStrategy filter layer."""
import pytest

from kalshi.signals import ForecastDrift, SignalScore
from kalshi.strategy import WeatherMakerConfig, should_quote


def _make_score(
    ticker: str = "KXHIGHNY-26MAR15-T54",
    city: str = "new_york",
    no_p_win: float = 0.98,
    yes_p_win: float = 0.02,
    n_models: int = 3,
    **kwargs,
) -> SignalScore:
    defaults = dict(
        threshold=54.0,
        direction="above",
        no_margin=7.0,
        emos_no=0.95,
        ngboost_no=0.99,
        drn_no=0.98,
        yes_bid=85,
        yes_ask=90,
        ts_event=0,
        ts_init=0,
    )
    defaults.update(kwargs)
    return SignalScore(
        ticker=ticker,
        city=city,
        no_p_win=no_p_win,
        yes_p_win=yes_p_win,
        n_models=n_models,
        **defaults,
    )


class TestFilterLayer:
    def test_high_confidence_no_passes(self):
        cfg = WeatherMakerConfig(confidence_threshold=0.95, min_models=2)
        score = _make_score(no_p_win=0.98, n_models=3)
        side, passes = should_quote(cfg, score, drift_cities=set())
        assert passes is True
        assert side == "no"

    def test_high_confidence_yes_passes(self):
        cfg = WeatherMakerConfig(confidence_threshold=0.95, min_models=2)
        score = _make_score(no_p_win=0.02, yes_p_win=0.98, n_models=3)
        side, passes = should_quote(cfg, score, drift_cities=set())
        assert passes is True
        assert side == "yes"

    def test_below_threshold_fails(self):
        cfg = WeatherMakerConfig(confidence_threshold=0.95, min_models=2)
        score = _make_score(no_p_win=0.90, yes_p_win=0.10, n_models=3)
        _, passes = should_quote(cfg, score, drift_cities=set())
        assert passes is False

    def test_insufficient_models_fails(self):
        cfg = WeatherMakerConfig(confidence_threshold=0.95, min_models=2)
        score = _make_score(no_p_win=0.98, n_models=1)
        _, passes = should_quote(cfg, score, drift_cities=set())
        assert passes is False

    def test_drift_pause_excludes_city(self):
        cfg = WeatherMakerConfig(
            confidence_threshold=0.95, min_models=2, drift_response="pause"
        )
        score = _make_score(no_p_win=0.98, n_models=3, city="new_york")
        _, passes = should_quote(cfg, score, drift_cities={"new_york"})
        assert passes is False

    def test_drift_tighten_raises_threshold(self):
        cfg = WeatherMakerConfig(
            confidence_threshold=0.95,
            min_models=2,
            drift_response="tighten",
            drift_threshold_delta=0.02,
        )
        # 0.96 is above 0.95 but below 0.95 + 0.02 = 0.97
        score = _make_score(no_p_win=0.96, n_models=3, city="new_york")
        _, passes = should_quote(cfg, score, drift_cities={"new_york"})
        assert passes is False

        # 0.98 is above 0.97
        score2 = _make_score(no_p_win=0.98, n_models=3, city="new_york")
        _, passes2 = should_quote(cfg, score2, drift_cities={"new_york"})
        assert passes2 is True

    def test_drift_ignore_has_no_effect(self):
        cfg = WeatherMakerConfig(
            confidence_threshold=0.95, min_models=2, drift_response="ignore"
        )
        score = _make_score(no_p_win=0.98, n_models=3, city="new_york")
        _, passes = should_quote(cfg, score, drift_cities={"new_york"})
        assert passes is True

    def test_neither_side_above_threshold(self):
        """Both sides below threshold — ambiguous market, don't quote."""
        cfg = WeatherMakerConfig(confidence_threshold=0.95, min_models=2)
        score = _make_score(no_p_win=0.55, yes_p_win=0.45, n_models=3)
        _, passes = should_quote(cfg, score, drift_cities=set())
        assert passes is False
```

**Step 2: Run tests to verify they fail**

Run: `uv run python -m pytest tests/test_strategy.py -v`
Expected: FAIL — `ModuleNotFoundError`

**Step 3: Implement config and filter layer**

```python
# kalshi/strategy.py
"""WeatherMakerStrategy — forecast-filtered passive market maker for KXHIGH contracts."""
from __future__ import annotations

from dataclasses import dataclass, field

from nautilus_trader.config import StrategyConfig

from kalshi.signals import SignalScore


@dataclass(frozen=True)
class WeatherMakerConfig(StrategyConfig):
    """Configuration for the WeatherMakerStrategy."""

    # Filter
    confidence_threshold: float = 0.95
    min_models: int = 2
    drift_response: str = "pause"           # "pause" | "tighten" | "ignore"
    drift_threshold_delta: float = 0.02

    # Quote ladder
    ladder_depth: int = 3
    ladder_spacing: int = 1                 # cents between levels
    level_quantity: int = 10                # contracts per level
    reprice_threshold: int = 1              # cents — min bid movement to trigger reprice

    # Risk
    market_cap_pct: float = 0.20            # max fraction of account per contract
    city_cap_pct: float = 0.33              # max fraction of account per city

    # Exit
    exit_price_cents: int = 97              # close position when bid reaches this

    # Strategy identity
    strategy_id: str = "WeatherMaker-001"


def should_quote(
    config: WeatherMakerConfig,
    score: SignalScore,
    drift_cities: set[str],
) -> tuple[str, bool]:
    """Decide if a contract should be quoted and on which side.

    Returns (side, passes) where side is "yes" or "no" and passes is True if
    all filter conditions are met.
    """
    # Determine winning side
    if score.no_p_win >= score.yes_p_win:
        side = "no"
        p_win = score.no_p_win
    else:
        side = "yes"
        p_win = score.yes_p_win

    # Check model count
    if score.n_models < config.min_models:
        return side, False

    # Determine effective threshold (may be raised by drift)
    threshold = config.confidence_threshold
    if score.city in drift_cities:
        if config.drift_response == "pause":
            return side, False
        elif config.drift_response == "tighten":
            threshold += config.drift_threshold_delta
        # "ignore" — threshold unchanged

    # Check confidence
    if p_win < threshold:
        return side, False

    return side, True
```

**Step 4: Run tests to verify they pass**

Run: `uv run python -m pytest tests/test_strategy.py -v`
Expected: All 8 tests PASS

**Step 5: Commit**

```bash
git add kalshi/strategy.py tests/test_strategy.py
git commit -m "feat: add WeatherMakerConfig and filter layer (should_quote)"
```

---

### Task 5: Review Task 4

**Trigger:** Both reviewers start simultaneously when Task 4 completes.

**Spec review — verify these specific items:**
- [ ] `should_quote()` checks `no_p_win >= threshold` OR `yes_p_win >= threshold` — both sides covered
- [ ] When both sides are below threshold, returns `passes=False` (test: `test_neither_side_above_threshold`)
- [ ] `drift_response="pause"` excludes the entire city (returns False immediately)
- [ ] `drift_response="tighten"` adds `drift_threshold_delta` to the confidence threshold
- [ ] `drift_response="ignore"` leaves threshold unchanged even when city is in drift_cities
- [ ] `min_models` check rejects scores with insufficient ensemble coverage
- [ ] `WeatherMakerConfig` defaults match the design doc values (0.95, 2, "pause", 0.02, 3, 1, 10, 1, 0.20, 0.33, 97)

**Code quality review — verify these specific items:**
- [ ] `should_quote()` is a pure function (no side effects, no state) — testable without NT infrastructure
- [ ] `WeatherMakerConfig` inherits from `StrategyConfig` (required for NT strategy registration)
- [ ] No import of Strategy class yet — filter logic is standalone, strategy shell comes in next task
- [ ] Tests cover all 3 drift_response modes with specific threshold arithmetic

**Resolution:** Findings queue for dev; all must resolve before next milestone.

---

### Task 6: Implement quote manager

**Files:**
- Modify: `kalshi/strategy.py`
- Modify: `tests/test_strategy.py`

**Step 1: Write failing tests for quote ladder computation**

Test the ladder computation as a pure function — given an anchor bid and config, compute the ladder levels.

```python
# Add to tests/test_strategy.py

from kalshi.strategy import compute_ladder, LadderLevel


class TestComputeLadder:
    def test_basic_ladder(self):
        """3 levels, spacing 1c, anchor at 92c."""
        levels = compute_ladder(anchor_bid_cents=92, depth=3, spacing=1, qty=10)
        assert len(levels) == 3
        assert levels[0] == LadderLevel(price_cents=92, quantity=10)
        assert levels[1] == LadderLevel(price_cents=91, quantity=10)
        assert levels[2] == LadderLevel(price_cents=90, quantity=10)

    def test_anchor_at_minimum(self):
        """Anchor at 1c — only one level possible."""
        levels = compute_ladder(anchor_bid_cents=1, depth=3, spacing=1, qty=10)
        assert len(levels) == 1
        assert levels[0] == LadderLevel(price_cents=1, quantity=10)

    def test_anchor_near_minimum(self):
        """Anchor at 3c with depth 5 — truncates to valid levels only."""
        levels = compute_ladder(anchor_bid_cents=3, depth=5, spacing=1, qty=10)
        assert len(levels) == 3
        assert levels[-1] == LadderLevel(price_cents=1, quantity=10)

    def test_spacing_2(self):
        levels = compute_ladder(anchor_bid_cents=95, depth=3, spacing=2, qty=5)
        assert levels[0].price_cents == 95
        assert levels[1].price_cents == 93
        assert levels[2].price_cents == 91

    def test_zero_anchor_returns_empty(self):
        levels = compute_ladder(anchor_bid_cents=0, depth=3, spacing=1, qty=10)
        assert levels == []

    def test_anchor_at_99(self):
        """Anchor at max valid price."""
        levels = compute_ladder(anchor_bid_cents=99, depth=3, spacing=1, qty=10)
        assert levels[0].price_cents == 99
        assert len(levels) == 3
```

**Step 2: Run tests to verify they fail**

Run: `uv run python -m pytest tests/test_strategy.py::TestComputeLadder -v`
Expected: FAIL — `ImportError: cannot import name 'compute_ladder'`

**Step 3: Implement compute_ladder**

Add to `kalshi/strategy.py`:

```python
from dataclasses import dataclass

@dataclass(frozen=True)
class LadderLevel:
    """A single price level in the quote ladder."""
    price_cents: int
    quantity: int


def compute_ladder(
    anchor_bid_cents: int,
    depth: int,
    spacing: int,
    qty: int,
) -> list[LadderLevel]:
    """Compute a descending quote ladder from the anchor bid.

    Returns up to `depth` levels, each spaced `spacing` cents apart,
    starting at the anchor. Levels below 1c are excluded.
    """
    levels = []
    for i in range(depth):
        price = anchor_bid_cents - (i * spacing)
        if price < 1:
            break
        levels.append(LadderLevel(price_cents=price, quantity=qty))
    return levels
```

**Step 4: Run tests to verify they pass**

Run: `uv run python -m pytest tests/test_strategy.py::TestComputeLadder -v`
Expected: All 6 tests PASS

**Step 5: Commit**

```bash
git add kalshi/strategy.py tests/test_strategy.py
git commit -m "feat: add compute_ladder for quote level calculation"
```

---

### Task 7: Implement risk manager

**Files:**
- Modify: `kalshi/strategy.py`
- Modify: `tests/test_strategy.py`

**Step 1: Write failing tests for risk cap calculations**

```python
# Add to tests/test_strategy.py

from kalshi.strategy import check_risk_caps, ExposureTracker


class TestExposureTracker:
    def test_empty_tracker(self):
        tracker = ExposureTracker()
        assert tracker.market_exposure("KXHIGHNY-26MAR15-T54") == 0
        assert tracker.city_exposure("new_york") == 0

    def test_add_exposure(self):
        tracker = ExposureTracker()
        tracker.add("KXHIGHNY-26MAR15-T54", "new_york", quantity=10, price_cents=92)
        assert tracker.market_exposure("KXHIGHNY-26MAR15-T54") == 920  # 10 * 92 cents
        assert tracker.city_exposure("new_york") == 920

    def test_city_aggregates_markets(self):
        tracker = ExposureTracker()
        tracker.add("KXHIGHNY-26MAR15-T54", "new_york", quantity=10, price_cents=90)
        tracker.add("KXHIGHNY-26MAR15-T60", "new_york", quantity=5, price_cents=95)
        assert tracker.city_exposure("new_york") == 10 * 90 + 5 * 95

    def test_remove_exposure(self):
        tracker = ExposureTracker()
        tracker.add("KXHIGHNY-26MAR15-T54", "new_york", quantity=10, price_cents=90)
        tracker.remove("KXHIGHNY-26MAR15-T54", "new_york", quantity=5, price_cents=90)
        assert tracker.market_exposure("KXHIGHNY-26MAR15-T54") == 450


class TestCheckRiskCaps:
    def test_within_caps(self):
        tracker = ExposureTracker()
        allowed = check_risk_caps(
            tracker=tracker,
            ticker="KXHIGHNY-26MAR15-T54",
            city="new_york",
            quantity=10,
            price_cents=90,
            account_balance_cents=100_000,
            market_cap_pct=0.20,
            city_cap_pct=0.33,
        )
        assert allowed == 10  # full quantity allowed

    def test_market_cap_reduces_quantity(self):
        tracker = ExposureTracker()
        # Already have 15,000 cents exposed in this market
        tracker.add("KXHIGHNY-26MAR15-T54", "new_york", quantity=166, price_cents=90)
        # Account is 100,000 cents. Market cap = 20,000 cents. Remaining ~5,060 cents.
        # At 90c per contract, that's ~56 contracts
        allowed = check_risk_caps(
            tracker=tracker,
            ticker="KXHIGHNY-26MAR15-T54",
            city="new_york",
            quantity=100,
            price_cents=90,
            account_balance_cents=100_000,
            market_cap_pct=0.20,
            city_cap_pct=0.33,
        )
        assert allowed < 100
        assert allowed >= 0

    def test_city_cap_reduces_quantity(self):
        tracker = ExposureTracker()
        # Fill up city exposure across multiple markets
        tracker.add("KXHIGHNY-26MAR15-T54", "new_york", quantity=100, price_cents=90)
        tracker.add("KXHIGHNY-26MAR15-T60", "new_york", quantity=100, price_cents=90)
        # City exposure = 18,000 cents. City cap at 33% of 100,000 = 33,000.
        # Remaining = 15,000 cents. At 90c = 166 contracts.
        allowed = check_risk_caps(
            tracker=tracker,
            ticker="KXHIGHNY-26MAR15-T65",
            city="new_york",
            quantity=200,
            price_cents=90,
            account_balance_cents=100_000,
            market_cap_pct=0.20,
            city_cap_pct=0.33,
        )
        assert allowed < 200
        assert allowed > 0

    def test_zero_balance_allows_nothing(self):
        tracker = ExposureTracker()
        allowed = check_risk_caps(
            tracker=tracker,
            ticker="KXHIGHNY-26MAR15-T54",
            city="new_york",
            quantity=10,
            price_cents=90,
            account_balance_cents=0,
            market_cap_pct=0.20,
            city_cap_pct=0.33,
        )
        assert allowed == 0
```

**Step 2: Run tests to verify they fail**

Run: `uv run python -m pytest tests/test_strategy.py::TestExposureTracker tests/test_strategy.py::TestCheckRiskCaps -v`
Expected: FAIL — `ImportError`

**Step 3: Implement ExposureTracker and check_risk_caps**

Add to `kalshi/strategy.py`:

```python
from collections import defaultdict


class ExposureTracker:
    """Tracks exposure in cents per market and per city."""

    def __init__(self) -> None:
        self._market: dict[str, int] = defaultdict(int)  # ticker -> cents
        self._city: dict[str, int] = defaultdict(int)    # city -> cents
        self._ticker_to_city: dict[str, str] = {}

    def add(self, ticker: str, city: str, quantity: int, price_cents: int) -> None:
        cost = quantity * price_cents
        self._market[ticker] += cost
        self._city[city] += cost
        self._ticker_to_city[ticker] = city

    def remove(self, ticker: str, city: str, quantity: int, price_cents: int) -> None:
        cost = quantity * price_cents
        self._market[ticker] = max(0, self._market[ticker] - cost)
        self._city[city] = max(0, self._city[city] - cost)

    def market_exposure(self, ticker: str) -> int:
        return self._market.get(ticker, 0)

    def city_exposure(self, city: str) -> int:
        return self._city.get(city, 0)


def check_risk_caps(
    tracker: ExposureTracker,
    ticker: str,
    city: str,
    quantity: int,
    price_cents: int,
    account_balance_cents: int,
    market_cap_pct: float,
    city_cap_pct: float,
) -> int:
    """Return the maximum allowed quantity given risk caps.

    Returns a value between 0 and `quantity` (inclusive).
    """
    if account_balance_cents <= 0 or price_cents <= 0:
        return 0

    market_cap = int(account_balance_cents * market_cap_pct)
    city_cap = int(account_balance_cents * city_cap_pct)

    market_remaining = max(0, market_cap - tracker.market_exposure(ticker))
    city_remaining = max(0, city_cap - tracker.city_exposure(city))

    max_by_market = market_remaining // price_cents
    max_by_city = city_remaining // price_cents

    return min(quantity, max_by_market, max_by_city)
```

**Step 4: Run tests to verify they pass**

Run: `uv run python -m pytest tests/test_strategy.py::TestExposureTracker tests/test_strategy.py::TestCheckRiskCaps -v`
Expected: All 8 tests PASS

**Step 5: Commit**

```bash
git add kalshi/strategy.py tests/test_strategy.py
git commit -m "feat: add ExposureTracker and risk cap checking"
```

---

### Task 8: Implement WeatherMakerStrategy shell (lifecycle + wiring)

**Files:**
- Modify: `kalshi/strategy.py`
- Test: `tests/test_strategy_lifecycle.py`

**Step 1: Write failing tests for strategy lifecycle and event routing**

Test that the strategy correctly subscribes to data and routes events. Use NT's `TestableStrategy` patterns from the existing test code — mock the NT Cython attributes.

```python
# tests/test_strategy_lifecycle.py
"""Tests for WeatherMakerStrategy lifecycle and event wiring."""
from types import MethodType, SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from kalshi.signals import ForecastDrift, SignalScore
from kalshi.strategy import WeatherMakerConfig, WeatherMakerStrategy


def _make_strategy(config: WeatherMakerConfig | None = None) -> WeatherMakerStrategy:
    """Create a testable WeatherMakerStrategy with mocked NT attributes."""
    cfg = config or WeatherMakerConfig()
    strategy = object.__new__(WeatherMakerStrategy)

    # Mock NT Cython read-only descriptors
    strategy._config = cfg
    strategy.log = MagicMock()
    strategy.clock = MagicMock()
    strategy.clock.timestamp_ns.return_value = 1_000_000_000
    strategy.cache = MagicMock()
    strategy.portfolio = MagicMock()

    # Mock NT methods
    strategy.subscribe_data = MagicMock()
    strategy.subscribe_quote_ticks = MagicMock()
    strategy.publish_data = MagicMock()
    strategy.submit_order = MagicMock()
    strategy.cancel_order = MagicMock()
    strategy.cancel_all_orders = MagicMock()
    strategy.order_factory = MagicMock()

    # Call __init__ body (not __init__ itself — Cython parent constructor issues)
    WeatherMakerStrategy._init_state(strategy, cfg)

    return strategy


class TestStrategyState:
    def test_initial_state_empty(self):
        strategy = _make_strategy()
        assert strategy._scores == {}
        assert strategy._drift_cities == set()
        assert strategy._quoted_tickers == set()

    def test_on_signal_score_updates_scores(self):
        strategy = _make_strategy()
        score = SignalScore(
            ticker="KXHIGHNY-26MAR15-T54",
            city="new_york",
            threshold=54.0,
            direction="above",
            no_p_win=0.98,
            yes_p_win=0.02,
            no_margin=7.0,
            n_models=3,
            emos_no=0.95,
            ngboost_no=0.99,
            drn_no=0.98,
            yes_bid=85,
            yes_ask=90,
            ts_event=0,
            ts_init=0,
        )
        strategy._handle_signal_score(score)
        assert "KXHIGHNY-26MAR15-T54" in strategy._scores
        assert strategy._scores["KXHIGHNY-26MAR15-T54"] is score

    def test_on_forecast_drift_adds_city(self):
        strategy = _make_strategy()
        drift = ForecastDrift(
            city="new_york",
            date="2026-03-15",
            message="ECMWF shifted +2.3F",
            ts_event=0,
            ts_init=0,
        )
        strategy._handle_forecast_drift(drift)
        assert "new_york" in strategy._drift_cities

    def test_score_update_clears_drift(self):
        """A new score for a city in drift should clear the drift state."""
        strategy = _make_strategy()
        strategy._drift_cities.add("new_york")
        score = SignalScore(
            ticker="KXHIGHNY-26MAR15-T54",
            city="new_york",
            threshold=54.0,
            direction="above",
            no_p_win=0.98,
            yes_p_win=0.02,
            no_margin=7.0,
            n_models=3,
            emos_no=0.95,
            ngboost_no=0.99,
            drn_no=0.98,
            yes_bid=85,
            yes_ask=90,
            ts_event=0,
            ts_init=0,
        )
        strategy._handle_signal_score(score)
        assert "new_york" not in strategy._drift_cities
```

**Step 2: Run tests to verify they fail**

Run: `uv run python -m pytest tests/test_strategy_lifecycle.py -v`
Expected: FAIL — `ImportError: cannot import name 'WeatherMakerStrategy'`

**Step 3: Implement WeatherMakerStrategy shell**

Add to `kalshi/strategy.py`:

```python
from nautilus_trader.common.enums import LogColor
from nautilus_trader.core.data import Data
from nautilus_trader.model.data import DataType
from nautilus_trader.model.enums import OrderSide, TimeInForce
from nautilus_trader.model.identifiers import ClientOrderId, InstrumentId
from nautilus_trader.model.objects import Price, Quantity
from nautilus_trader.trading.strategy import Strategy


class WeatherMakerStrategy(Strategy):
    """Forecast-filtered passive market maker for KXHIGH contracts."""

    def __init__(self, config: WeatherMakerConfig) -> None:
        super().__init__(config)
        self._init_state(config)

    def _init_state(self, config: WeatherMakerConfig) -> None:
        """Initialize strategy state (called from __init__ and test helpers)."""
        self._cfg = config
        self._scores: dict[str, SignalScore] = {}
        self._drift_cities: set[str] = set()
        self._quoted_tickers: set[str] = set()
        self._resting_orders: dict[str, list[ClientOrderId]] = {}  # ticker -> order ids
        self._exposure = ExposureTracker()
        self._last_anchor: dict[str, int] = {}  # ticker -> last anchor bid cents

    def on_start(self) -> None:
        self.subscribe_data(DataType(SignalScore))
        self.subscribe_data(DataType(ForecastDrift))
        self.log.info("WeatherMakerStrategy started — subscribed to signals")

    def on_data(self, data: Data) -> None:
        if isinstance(data, SignalScore):
            self._handle_signal_score(data)
        elif isinstance(data, ForecastDrift):
            self._handle_forecast_drift(data)

    def _handle_signal_score(self, score: SignalScore) -> None:
        """Process a new score: update state, clear drift, re-evaluate filter."""
        self._scores[score.ticker] = score

        # New score clears drift for this city
        if score.city in self._drift_cities:
            self._drift_cities.discard(score.city)

        self._evaluate_contract(score.ticker)

    def _handle_forecast_drift(self, drift: ForecastDrift) -> None:
        """Process a drift alert: pause/tighten quoting for the city."""
        self._drift_cities.add(drift.city)
        self.log.warning(f"Forecast drift for {drift.city}: {drift.message}")

        # Re-evaluate all contracts for this city
        for ticker, score in self._scores.items():
            if score.city == drift.city:
                self._evaluate_contract(ticker)

    def _evaluate_contract(self, ticker: str) -> None:
        """Re-evaluate whether to quote a contract based on current state."""
        score = self._scores.get(ticker)
        if score is None:
            return

        side, passes = should_quote(self._cfg, score, self._drift_cities)

        if passes and ticker not in self._quoted_tickers:
            self._quoted_tickers.add(ticker)
            self.log.info(f"Filter PASS: {ticker} ({side} side, p_win={score.no_p_win if side == 'no' else score.yes_p_win:.3f})")
            # Quote management triggered by on_quote_tick
        elif not passes and ticker in self._quoted_tickers:
            self._quoted_tickers.discard(ticker)
            self._cancel_all_for_ticker(ticker)
            self.log.info(f"Filter EXIT: {ticker}")

    def _cancel_all_for_ticker(self, ticker: str) -> None:
        """Cancel all resting orders for a specific ticker."""
        order_ids = self._resting_orders.pop(ticker, [])
        for oid in order_ids:
            order = self.cache.order(oid)
            if order and order.is_open:
                self.cancel_order(order)

    def on_quote_tick(self, tick) -> None:
        """Handle incoming quote tick — reprice ladder if needed."""
        ticker_with_side = tick.instrument_id.symbol.value  # e.g. "KXHIGHNY-26MAR15-T54-NO"
        # Extract base ticker (without -YES/-NO suffix)
        parts = ticker_with_side.rsplit("-", 1)
        if len(parts) != 2 or parts[1] not in ("YES", "NO"):
            return
        base_ticker = parts[0]

        if base_ticker not in self._quoted_tickers:
            return

        score = self._scores.get(base_ticker)
        if score is None:
            return

        side, passes = should_quote(self._cfg, score, self._drift_cities)
        if not passes:
            return

        # Only act on ticks for our trading side
        tick_side = parts[1].lower()
        if tick_side != side:
            return

        # Get current bid in cents
        bid_cents = round(float(tick.bid_price) * 100)
        if bid_cents <= 0:
            return

        # Check exit condition
        if bid_cents >= self._cfg.exit_price_cents:
            self._exit_position(base_ticker, tick.instrument_id, bid_cents)
            return

        # Check if reprice needed
        last_anchor = self._last_anchor.get(base_ticker, 0)
        if abs(bid_cents - last_anchor) < self._cfg.reprice_threshold and last_anchor > 0:
            return  # Within dead zone, no reprice

        self._reprice_ladder(base_ticker, tick.instrument_id, side, bid_cents, score)

    def _reprice_ladder(
        self,
        ticker: str,
        instrument_id: InstrumentId,
        side: str,
        bid_cents: int,
        score: SignalScore,
    ) -> None:
        """Cancel existing orders and place new ladder at current bid."""
        # Cancel existing
        self._cancel_all_for_ticker(ticker)

        # Get account balance for risk caps
        # Note: in backtesting, portfolio.account returns the simulated account
        account = self.portfolio.account(instrument_id.venue)
        if account is None:
            self.log.warning(f"No account for {instrument_id.venue}")
            return
        balances = account.balances()
        if not balances:
            return
        balance_cents = int(list(balances.values())[0].total.as_double() * 100)

        # Compute ladder
        levels = compute_ladder(
            anchor_bid_cents=bid_cents,
            depth=self._cfg.ladder_depth,
            spacing=self._cfg.ladder_spacing,
            qty=self._cfg.level_quantity,
        )

        # Place orders for each level, respecting risk caps
        instrument = self.cache.instrument(instrument_id)
        if instrument is None:
            self.log.warning(f"Instrument not in cache: {instrument_id}")
            return

        new_order_ids = []
        for level in levels:
            allowed_qty = check_risk_caps(
                tracker=self._exposure,
                ticker=ticker,
                city=score.city,
                quantity=level.quantity,
                price_cents=level.price_cents,
                account_balance_cents=balance_cents,
                market_cap_pct=self._cfg.market_cap_pct,
                city_cap_pct=self._cfg.city_cap_pct,
            )
            if allowed_qty <= 0:
                break

            order = self.order_factory.limit(
                instrument_id=instrument_id,
                order_side=OrderSide.BUY,
                quantity=instrument.make_qty(allowed_qty),
                price=instrument.make_price(level.price_cents / 100.0),
                time_in_force=TimeInForce.GTC,
            )
            self.submit_order(order)
            new_order_ids.append(order.client_order_id)
            self._exposure.add(ticker, score.city, allowed_qty, level.price_cents)

        self._resting_orders[ticker] = new_order_ids
        self._last_anchor[ticker] = bid_cents

    def _exit_position(self, ticker: str, instrument_id: InstrumentId, bid_cents: int) -> None:
        """Exit position when price hits exit threshold."""
        # Cancel resting orders
        self._cancel_all_for_ticker(ticker)

        # Check if we have a position to exit
        positions = self.cache.positions(venue=instrument_id.venue)
        for pos in positions:
            if pos.instrument_id == instrument_id and not pos.is_closed:
                qty = int(pos.quantity.as_double())
                if qty > 0:
                    instrument = self.cache.instrument(instrument_id)
                    order = self.order_factory.market(
                        instrument_id=instrument_id,
                        order_side=OrderSide.SELL,
                        quantity=instrument.make_qty(qty),
                        time_in_force=TimeInForce.FOK,
                    )
                    self.submit_order(order)
                    self.log.info(f"EXIT: {ticker} at {bid_cents}c, selling {qty} contracts")
                break

    def on_order_filled(self, event) -> None:
        """Track fills for exposure and replenishment."""
        self.log.info(
            f"FILL: {event.instrument_id} {event.order_side} "
            f"{event.last_qty}@{event.last_px}"
        )

    def on_order_canceled(self, event) -> None:
        """Remove canceled order from tracking."""
        pass  # _resting_orders is replaced entirely on reprice

    def on_order_rejected(self, event) -> None:
        """Log rejected orders — this should not happen in normal operation."""
        self.log.error(f"ORDER REJECTED: {event.instrument_id} — {event.reason}")

    def on_stop(self) -> None:
        self.cancel_all_orders(self.id)
        self.log.info("WeatherMakerStrategy stopped — all orders canceled")
```

**Step 4: Run tests to verify they pass**

Run: `uv run python -m pytest tests/test_strategy_lifecycle.py -v`
Expected: All 4 tests PASS

**Step 5: Commit**

```bash
git add kalshi/strategy.py tests/test_strategy_lifecycle.py
git commit -m "feat: add WeatherMakerStrategy shell with lifecycle, filter, quote, risk, exit"
```

---

### Task 9: Review Tasks 6-8

**Trigger:** Both reviewers start simultaneously when Tasks 6-8 complete.

**Spec review — verify these specific items:**
- [ ] `compute_ladder()` produces `ladder_depth` levels descending from anchor, spaced `ladder_spacing` cents apart — verify with anchor=92, depth=3, spacing=1 → [92, 91, 90]
- [ ] Ladder levels below 1 cent are excluded (prices in [1, 99] range for Kalshi)
- [ ] `ExposureTracker` tracks per-market and per-city exposure in cents — verify aggregation across multiple markets in same city
- [ ] `check_risk_caps()` returns `min(requested, market_remaining // price, city_remaining // price)` — both caps applied
- [ ] `_reprice_ladder()` cancels all existing orders before placing new ones — avoids double exposure
- [ ] `_reprice_ladder()` skips repricing when bid movement < `reprice_threshold` (dead zone) — avoids rate limit churn
- [ ] `_exit_position()` fires when bid reaches `exit_price_cents` — sells position with FOK market order
- [ ] `on_quote_tick()` correctly extracts base ticker from instrument ID (strips `-YES`/`-NO` suffix)
- [ ] `on_quote_tick()` only acts on ticks for the forecast-confirmed side (ignores opposite side ticks)
- [ ] `_handle_forecast_drift()` re-evaluates all contracts for the affected city
- [ ] `_handle_signal_score()` clears drift state for the city when new score arrives
- [ ] `on_stop()` cancels all resting orders

**Code quality review — verify these specific items:**
- [ ] `compute_ladder()` and `check_risk_caps()` are pure functions — testable without NT
- [ ] `ExposureTracker` uses `defaultdict(int)` — no KeyError risk on first access
- [ ] Order placement uses `instrument.make_qty()` and `instrument.make_price()` — follows NT patterns from `archive/strategy.py:70-100`
- [ ] Rate limit: strategy does cancel-and-replace, not modify — Kalshi doesn't support modification
- [ ] Balance retrieval from `portfolio.account(venue).balances()` — check this is the correct NT API path
- [ ] No silent error swallowing — `on_order_rejected` logs at ERROR level, not WARNING

**Validation Data:**
- `compute_ladder(92, 3, 1, 10)` → `[(92, 10), (91, 10), (90, 10)]`
- `compute_ladder(1, 3, 1, 10)` → `[(1, 10)]` (truncated)
- `check_risk_caps(empty_tracker, qty=10, price=90, balance=100000, market=0.20, city=0.33)` → 10
- `check_risk_caps(tracker_at_market_cap, ...)` → reduced quantity

**Resolution:** Findings queue for dev; all must resolve before next milestone.

---

### Task 9.5: Milestone — Phase 2 Complete

**Present to user:**
- Filter layer (should_quote) with configurable threshold, model count, drift response
- Quote manager (compute_ladder) with configurable depth, spacing, repricing
- Risk manager (ExposureTracker, check_risk_caps) with per-market and per-city caps
- Exit manager (price-based exit with configurable threshold)
- Full WeatherMakerStrategy shell with lifecycle wiring
- Review findings and resolutions
- All test results

**Wait for user response before proceeding to Phase 3.**

---

## Phase 3: Backtesting Infrastructure

### Task 10: Implement backtest data loader

**Files:**
- Create: `kalshi/backtest_loader.py`
- Test: `tests/test_backtest_loader.py`

**Step 1: Write failing tests for loading backfill data into SignalScore events**

```python
# tests/test_backtest_loader.py
"""Tests for backtest data loading — signal server backfill → SignalScore list."""
import json

import pytest

from kalshi.backtest_loader import parse_backfill_response
from kalshi.signals import SignalScore


class TestParseBackfillResponse:
    def test_parses_single_score(self):
        data = [
            {
                "ticker": "KXHIGHNY-26MAR15-T54",
                "city": "new_york",
                "threshold": 54.0,
                "direction": "above",
                "no_p_win": 0.98,
                "yes_p_win": 0.02,
                "no_margin": 7.0,
                "n_models": 3,
                "emos_no": 0.95,
                "ngboost_no": 0.99,
                "drn_no": 0.98,
                "yes_bid": 85,
                "yes_ask": 90,
                "status": "open",
                "timestamp": "2026-03-14T12:00:00Z",
            }
        ]
        scores = parse_backfill_response(data)
        assert len(scores) == 1
        assert isinstance(scores[0], SignalScore)
        assert scores[0].ticker == "KXHIGHNY-26MAR15-T54"
        assert scores[0].ts_event > 0  # parsed from timestamp

    def test_sorted_by_timestamp(self):
        data = [
            {
                "ticker": "T1",
                "city": "new_york",
                "threshold": 54.0,
                "direction": "above",
                "no_p_win": 0.98,
                "yes_p_win": 0.02,
                "no_margin": 7.0,
                "n_models": 3,
                "emos_no": 0.95,
                "ngboost_no": 0.99,
                "drn_no": 0.98,
                "yes_bid": 85,
                "yes_ask": 90,
                "status": "open",
                "timestamp": "2026-03-14T14:00:00Z",
            },
            {
                "ticker": "T2",
                "city": "chicago",
                "threshold": 60.0,
                "direction": "above",
                "no_p_win": 0.95,
                "yes_p_win": 0.05,
                "no_margin": 5.0,
                "n_models": 3,
                "emos_no": 0.93,
                "ngboost_no": 0.96,
                "drn_no": 0.95,
                "yes_bid": 80,
                "yes_ask": 85,
                "status": "open",
                "timestamp": "2026-03-14T12:00:00Z",
            },
        ]
        scores = parse_backfill_response(data)
        assert scores[0].ticker == "T2"  # earlier timestamp first
        assert scores[1].ticker == "T1"

    def test_empty_response(self):
        scores = parse_backfill_response([])
        assert scores == []
```

**Step 2: Run tests to verify they fail**

Run: `uv run python -m pytest tests/test_backtest_loader.py -v`
Expected: FAIL — `ModuleNotFoundError`

**Step 3: Implement backtest_loader**

```python
# kalshi/backtest_loader.py
"""Load signal server backfill data into SignalScore events for backtesting."""
from __future__ import annotations

from datetime import datetime, timezone

import httpx

from kalshi.signal_actor import parse_score_msg
from kalshi.signals import SignalScore


def _iso_to_ns(ts_str: str) -> int:
    """Convert ISO 8601 timestamp string to nanoseconds since epoch."""
    dt = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
    return int(dt.timestamp() * 1_000_000_000)


def parse_backfill_response(data: list[dict]) -> list[SignalScore]:
    """Parse backfill response into sorted list of SignalScore events."""
    scores = []
    for item in data:
        ts_ns = _iso_to_ns(item["timestamp"])
        score = parse_score_msg(item, ts_ns)
        scores.append(score)
    scores.sort(key=lambda s: s.ts_event)
    return scores


async def fetch_backfill(
    http_url: str,
    lead_idx: int = 1,
    start_date: str = "2026-01-01",
) -> list[SignalScore]:
    """Fetch historical scores from the signal server backfill endpoint."""
    url = f"{http_url}/v1/trading/scores/backfill"
    params = {"lead_idx": lead_idx, "start_date": start_date}
    async with httpx.AsyncClient() as client:
        resp = await client.get(url, params=params, timeout=120.0)
        resp.raise_for_status()
        data = resp.json()
    return parse_backfill_response(data)
```

**Step 4: Run tests to verify they pass**

Run: `uv run python -m pytest tests/test_backtest_loader.py -v`
Expected: All 3 tests PASS

**Step 5: Commit**

```bash
git add kalshi/backtest_loader.py tests/test_backtest_loader.py
git commit -m "feat: add backtest data loader for signal server backfill"
```

---

### Task 11: Review Task 10

**Trigger:** Both reviewers start simultaneously when Task 10 completes.

**Spec review — verify these specific items:**
- [ ] `parse_backfill_response()` reuses `parse_score_msg()` from SignalActor — no duplicate parsing logic
- [ ] Timestamps parsed from ISO 8601 to nanoseconds — verify `_iso_to_ns("2026-03-14T12:00:00Z")` produces correct value
- [ ] Results sorted by `ts_event` ascending — BacktestEngine expects chronological order
- [ ] `fetch_backfill()` calls `GET /v1/trading/scores/backfill?lead_idx=1&start_date=2026-01-01` — matches the API endpoint the signal server provides
- [ ] `resp.raise_for_status()` — HTTP errors propagate (escalate, don't accommodate)

**Code quality review — verify these specific items:**
- [ ] `_iso_to_ns()` handles both `"Z"` and `"+00:00"` timezone suffixes
- [ ] `fetch_backfill()` uses `timeout=120.0` — backfill may be large, don't timeout prematurely
- [ ] No unnecessary dependencies — uses `httpx` (already in project for signal actor) and stdlib `datetime`

**Resolution:** Findings queue for dev; all must resolve before next milestone.

---

### Task 12: Implement backtest harness

**Files:**
- Create: `kalshi/backtest.py`
- Test: `tests/test_backtest.py`

**Step 1: Write failing test for backtest engine configuration**

```python
# tests/test_backtest.py
"""Tests for backtest harness setup."""
import pytest

from kalshi.backtest import build_backtest_engine


class TestBuildBacktestEngine:
    def test_creates_engine_with_venue(self):
        engine = build_backtest_engine(starting_balance_usd=10_000)
        # Venue should be configured
        assert engine is not None

    def test_accepts_custom_balance(self):
        engine = build_backtest_engine(starting_balance_usd=50_000)
        assert engine is not None
```

**Step 2: Run tests to verify they fail**

Run: `uv run python -m pytest tests/test_backtest.py -v`
Expected: FAIL — `ModuleNotFoundError`

**Step 3: Implement backtest harness**

```python
# kalshi/backtest.py
"""Backtest harness for WeatherMakerStrategy."""
from __future__ import annotations

from decimal import Decimal
from pathlib import Path

from nautilus_trader.backtest.engine import BacktestEngine, BacktestEngineConfig
from nautilus_trader.model.currencies import USD
from nautilus_trader.model.data import DataType
from nautilus_trader.model.enums import AccountType, OmsType
from nautilus_trader.model.identifiers import ClientId, TraderId, Venue
from nautilus_trader.model.objects import Money
from nautilus_trader.persistence.catalog import ParquetDataCatalog

from kalshi.common.constants import KALSHI_VENUE
from kalshi.signals import SignalScore


def build_backtest_engine(
    starting_balance_usd: int = 10_000,
    trader_id: str = "BACKTESTER-001",
) -> BacktestEngine:
    """Build a BacktestEngine configured for Kalshi KXHIGH backtesting.

    Returns the engine ready for add_instrument(), add_data(), add_strategy().
    """
    config = BacktestEngineConfig(
        trader_id=TraderId(trader_id),
    )
    engine = BacktestEngine(config=config)

    engine.add_venue(
        venue=KALSHI_VENUE,
        oms_type=OmsType.NETTING,
        account_type=AccountType.CASH,
        base_currency=USD,
        starting_balances=[Money(starting_balance_usd, USD)],
    )

    return engine


def load_catalog_data(
    engine: BacktestEngine,
    catalog_path: str | Path,
    instrument_ids: list[str] | None = None,
) -> None:
    """Load QuoteTick data from ParquetDataCatalog into the engine."""
    catalog = ParquetDataCatalog(str(catalog_path))

    instruments = catalog.instruments()
    if instrument_ids:
        instruments = [i for i in instruments if str(i.id) in instrument_ids]

    for inst in instruments:
        engine.add_instrument(inst)

    quotes = catalog.quote_ticks(
        instrument_ids=[i.id for i in instruments] if instrument_ids else None,
    )
    if quotes:
        engine.add_data(quotes, sort=False)


def load_signal_data(
    engine: BacktestEngine,
    scores: list[SignalScore],
) -> None:
    """Load historical SignalScore data into the engine."""
    if scores:
        engine.add_data(scores, client_id=ClientId("SIGNAL"), sort=False)
```

**Step 4: Run tests to verify they pass**

Run: `uv run python -m pytest tests/test_backtest.py -v`
Expected: All 2 tests PASS

**Step 5: Commit**

```bash
git add kalshi/backtest.py tests/test_backtest.py
git commit -m "feat: add backtest harness for Kalshi KXHIGH backtesting"
```

---

### Task 13: Review Tasks 12 + Milestone — Phase 3 Complete

**Trigger:** Both reviewers start simultaneously when Task 12 completes.

**Spec review — verify these specific items:**
- [ ] `build_backtest_engine()` uses `KALSHI_VENUE` from `kalshi/common/constants.py` — same venue as live adapter
- [ ] `OmsType.NETTING` and `AccountType.CASH` — matches design doc (prediction markets are cash-settled, positions net)
- [ ] `load_signal_data()` passes `client_id=ClientId("SIGNAL")` — required for custom data without instrument_id (verified in research)
- [ ] Both `load_catalog_data` and `load_signal_data` pass `sort=False` — sort once with `engine.sort_data()` at the end for efficiency
- [ ] `load_catalog_data()` adds instruments before data — `engine.add_instrument()` must precede `engine.add_data()` for those instruments

**Code quality review — verify these specific items:**
- [ ] No hardcoded file paths — catalog path is a parameter
- [ ] Currency is USD (the base currency), not CONTRACT — verify this matches the adapter's internal accounting
- [ ] `load_catalog_data()` handles the case where `catalog.quote_ticks()` returns empty/None

**Milestone — present to user:**
- Backtest data loader fetches historical scores from signal server
- Backtest harness configures BacktestEngine with Kalshi venue
- Catalog and signal data loading utilities ready
- Review findings and resolutions
- How to run an end-to-end backtest (engine.sort_data() → engine.add_strategy() → engine.run())

**Wait for user response before proceeding to Phase 4.**

---

## Phase 4: Integration Testing

### Task 14: Implement integration test — mock signal server + mock exchange

**Files:**
- Create: `tests/test_integration.py`
- Modify: `tests/mock_exchange/server.py` (may need minor additions for signal server mock)

**Step 1: Write integration test**

Create a mock signal server (simple WebSocket + REST) alongside the existing mock Kalshi exchange. Boot a TradingNode with SignalActor + WeatherMakerStrategy, verify end-to-end flow: signal → filter → quote → fill.

```python
# tests/test_integration.py
"""Integration test: SignalActor + WeatherMakerStrategy + MockKalshiServer."""
import asyncio
import json
import threading
import time
from unittest.mock import MagicMock

import pytest
import websockets

from kalshi.signal_actor import SignalActorConfig
from kalshi.signals import SignalScore
from kalshi.strategy import WeatherMakerConfig, WeatherMakerStrategy


class MockSignalServer:
    """Minimal mock of the signal server for integration testing."""

    def __init__(self) -> None:
        self.port: int | None = None
        self._ws_clients: list = []
        self._scores: list[dict] = []

    async def _handle_ws(self, ws) -> None:
        self._ws_clients.append(ws)
        try:
            async for msg in ws:
                data = json.loads(msg)
                if data.get("type") == "subscribe":
                    # Send initial scores
                    await ws.send(json.dumps({
                        "type": "scores_update",
                        "scores": self._scores,
                    }))
                elif data.get("type") == "ping":
                    await ws.send(json.dumps({"type": "pong"}))
        except websockets.ConnectionClosed:
            pass
        finally:
            self._ws_clients.remove(ws)

    async def _handle_http(self, reader, writer) -> None:
        data = await reader.read(4096)
        request = data.decode()
        if "/v1/trading/scores" in request:
            body = json.dumps(self._scores).encode()
            response = (
                f"HTTP/1.1 200 OK\r\n"
                f"Content-Type: application/json\r\n"
                f"Content-Length: {len(body)}\r\n"
                f"\r\n"
            ).encode() + body
            writer.write(response)
            await writer.drain()
        writer.close()

    def set_scores(self, scores: list[dict]) -> None:
        self._scores = scores

    async def broadcast_scores(self, scores: list[dict]) -> None:
        self._scores = scores
        msg = json.dumps({"type": "scores_update", "scores": scores})
        for ws in self._ws_clients:
            await ws.send(msg)

    async def start(self) -> None:
        self._ws_server = await websockets.serve(self._handle_ws, "127.0.0.1", 0)
        self._http_server = await asyncio.start_server(self._handle_http, "127.0.0.1", 0)
        self.ws_port = self._ws_server.sockets[0].getsockname()[1]
        self.http_port = self._http_server.sockets[0].getsockname()[1]

    async def stop(self) -> None:
        self._ws_server.close()
        await self._ws_server.wait_closed()
        self._http_server.close()
        await self._http_server.wait_closed()


# Integration tests go here — test the full flow:
# 1. Start MockSignalServer + MockKalshiServer
# 2. Set high-confidence scores for a test contract
# 3. Boot TradingNode with SignalActor + WeatherMakerStrategy
# 4. Verify: strategy receives scores, filter passes, ladder placed
# 5. Update book prices, verify reprice
# 6. Push bid above exit threshold, verify exit
#
# This test follows the same pattern as test_live_confidence.py
# but adds the signal server component.
```

Note: The full integration test implementation will follow the exact patterns in `tests/test_live_confidence.py:133-236`. The executor should:
1. Start both mock servers in background threads
2. Create configs pointing to localhost ports
3. Boot TradingNode with both data client factories + SignalActor + WeatherMakerStrategy
4. Run scenarios and assert outcomes

**Step 2: Run test to verify it fails**

Run: `uv run python -m pytest tests/test_integration.py -v`
Expected: FAIL (test not yet fully implemented)

**Step 3: Implement the full integration test following test_live_confidence.py patterns**

The executor should fill in the test body following the TradingNode boot pattern from `tests/test_live_confidence.py:178-191` and scenario runner from `tests/test_live_confidence.py:199-215`.

Key assertions:
- SignalActor publishes SignalScore events (verify via strategy state)
- Filter passes for high-confidence contracts
- Ladder orders are placed (check `cache.orders()`)
- Orders are repriced when book moves
- Position exits when bid hits exit threshold

**Step 4: Run test to verify it passes**

Run: `uv run python -m pytest tests/test_integration.py -v`
Expected: PASS

**Step 5: Commit**

```bash
git add tests/test_integration.py
git commit -m "test: add integration test for full signal-to-trade pipeline"
```

---

### Task 15: Review Task 14 + Final Milestone

**Trigger:** Both reviewers start simultaneously when Task 14 completes.

**Spec review — verify these specific items:**
- [ ] Integration test boots both MockSignalServer and MockKalshiServer — two independent mock services
- [ ] SignalActor connects to mock signal server WS and receives scores
- [ ] Strategy receives SignalScore via MessageBus, filter passes for high-confidence score
- [ ] Ladder orders appear in `cache.orders()` — verify correct prices (anchor at bid, descending)
- [ ] Reprice occurs when book updates move the bid by more than reprice_threshold
- [ ] Exit fires when bid reaches exit_price_cents — market SELL order appears
- [ ] Factory singleton reset: `kalshi.factories._SHARED_PROVIDER = None` before test (line reference: `tests/test_live_confidence.py:152`)
- [ ] Test cleanup: servers stopped, TradingNode stopped

**Code quality review — verify these specific items:**
- [ ] MockSignalServer follows MockKalshiServer patterns — asyncio.start_server for HTTP, websockets.serve for WS
- [ ] No flaky timing — use events/conditions for synchronization, not sleep
- [ ] Test assertions check specific values (order prices, quantities), not just counts
- [ ] Proper isolation — test can run standalone without affecting other tests

**Final Milestone — present to user:**
- Complete weather market maker system implemented and tested
- Components: SignalScore/ForecastDrift data types, SignalActor, WeatherMakerStrategy (filter + quote + risk + exit), backtest harness
- Integration test verifying full signal-to-trade pipeline
- All review findings resolved
- Next steps: run backtests with real data, tune parameters

**Wait for user response.**
