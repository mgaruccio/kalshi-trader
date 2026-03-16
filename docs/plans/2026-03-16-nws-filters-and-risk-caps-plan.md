# NWS Filters & Low-Price Risk Caps — Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use forge:executing-plans to implement this plan task-by-task.

**Goal:** Add NWS forecast-based hard filters and low-price cap reduction to the WeatherMaker strategy, with all parameters configurable for backtest sweeps.

**Architecture:** Extend the existing `should_quote()` → `check_risk_caps()` filter chain. Add `nws_max` field to `SignalScore`, new config fields to `WeatherMakerConfig`, NWS margin/divergence checks to `should_quote()`, and cap multiplier logic to `_reprice_ladder()` before it calls `check_risk_caps()`.

**Tech Stack:** Python, NautilusTrader custom data types, pytest

**Required Skills:**
- `forge:writing-tests`: Invoke before each implementation task — TDD for all changes
- `forge:escalate-not-accommodate`: Apply throughout — NWS margin checks must reject, not silently downgrade

## Context for Executor

### Key Files
- `kalshi/signals.py:7-23` — `SignalScore` dataclass, add `nws_max` field here
- `kalshi/signal_actor.py:30-70` — `parse_score_msg()`, add `nws_max` parsing here
- `kalshi/strategy.py:21-47` — `WeatherMakerConfig`, add new config fields here
- `kalshi/strategy.py:50-97` — `should_quote()`, add NWS hard filters here
- `kalshi/strategy.py:100-128` — `check_risk_caps()`, no changes needed (caps modified upstream)
- `kalshi/strategy.py:346-417` — `_reprice_ladder()`, apply cap multiplier before calling `check_risk_caps()` at line 387
- `tests/test_strategy.py:1-317` — existing tests for `should_quote`, `check_risk_caps`, `compute_ladder`
- `scripts/sweep_params.py:27-33` — sweep grid, add new params here

### Research Findings
- `SignalScore` uses `@customdataclass` (NT Cython-compatible). All fields must have defaults. Convention: `0.0` for absent float fields (see `emos_no`, `ngboost_no`, `drn_no` at line 18-20).
- `parse_score_msg()` uses `msg.get("field", 0.0)` for optional fields — same pattern for `nws_max`.
- `n_models` stays 1-3. NWS is a temperature forecast, not a probability model. Validation at `signal_actor.py:46` unchanged.
- `should_quote()` returns `(side, bool)`. NWS checks go after model spread check (line 91) and before max entry check (line 94).
- `_reprice_ladder()` passes `self._config.market_cap_pct` and `self._config.city_cap_pct` directly to `check_risk_caps()` at line 393-394. Cap multiplier applies to these values before the call.
- Sweep grid at `sweep_params.py:29` uses `(label, field_name, values)` tuples. New config fields are automatically available.
- The `direction` field on `SignalScore` is `"above"` or `"below"`. For NWS margin: if direction is `"above"`, NWS margin = `nws_max - threshold` (positive = safe for NO). If `"below"`, NWS margin = `threshold - nws_max`.
- Signal server sends `nws_max: null` (JSON null) when unavailable. Python `msg.get("nws_max")` returns `None`. Convert to `0.0` for the dataclass.

### Relevant Patterns
- `kalshi/strategy.py:87-91` — model spread filter pattern (check non-zero values, compute metric, compare to config threshold)
- `kalshi/signal_actor.py:62-64` — optional field parsing pattern (`msg.get("field", 0.0)`)
- `tests/test_strategy.py:8-37` — `_make_score()` helper with `**kwargs` override pattern

## Execution Architecture

**Team:** 2 devs, 1 spec reviewer, 1 quality reviewer
**Task dependencies:**
  - Tasks 1-3 are sequential (data layer → filter logic → cap logic)
  - Task 7 (sweep grid) is independent of Tasks 4-6

**Phases:**
  - Phase 1: Tasks 1-3 (SignalScore + parsing + config fields)
  - Phase 2: Tasks 4-6 (NWS hard filters in should_quote)
  - Phase 3: Tasks 7-9 (cap multiplier in _reprice_ladder)
  - Phase 4: Task 10 (sweep grid)

**Milestones:**
  - After Phase 1 (Task 3) — data layer complete, backward compatible
  - After Phase 2 (Task 6) — NWS filters functional
  - After Phase 3 (Task 9) — cap reduction functional
  - After Phase 4 (Task 10) — ready for sweeps

---

### Task 1: Add `nws_max` to SignalScore and parse it

**Files:**
- Modify: `kalshi/signals.py:7-23`
- Modify: `kalshi/signal_actor.py:53-70`
- Test: `tests/test_strategy.py`

**Step 1: Write failing tests**

In `tests/test_strategy.py`, update `_make_score()` helper to accept `nws_max` and add tests:

```python
# In _make_score(), add to defaults dict:
#   nws_max=0.0,

def test_signal_score_has_nws_max():
    score = _make_score(nws_max=70.0)
    assert score.nws_max == 70.0

def test_signal_score_nws_max_defaults_to_zero():
    score = _make_score()
    assert score.nws_max == 0.0
```

Also add a test for `parse_score_msg` in the appropriate test file:

```python
# In tests/test_strategy.py or a new section:
from kalshi.signal_actor import parse_score_msg

def test_parse_score_msg_with_nws_max():
    msg = {
        "ticker": "KXHIGHTDC-26MAR16-T69",
        "city": "washington_dc",
        "threshold": 69.0,
        "direction": "above",
        "no_p_win": 0.98,
        "yes_p_win": 0.02,
        "no_margin": 5.0,
        "n_models": 3,
        "yes_bid": 85,
        "nws_max": 70.0,
    }
    score = parse_score_msg(msg, ts_ns=0)
    assert score.nws_max == 70.0

def test_parse_score_msg_nws_max_null():
    msg = {
        "ticker": "KXHIGHTDC-26MAR16-T69",
        "city": "washington_dc",
        "threshold": 69.0,
        "direction": "above",
        "no_p_win": 0.98,
        "yes_p_win": 0.02,
        "no_margin": 5.0,
        "n_models": 3,
        "yes_bid": 85,
        "nws_max": None,
    }
    score = parse_score_msg(msg, ts_ns=0)
    assert score.nws_max == 0.0

def test_parse_score_msg_nws_max_missing():
    msg = {
        "ticker": "KXHIGHTDC-26MAR16-T69",
        "city": "washington_dc",
        "threshold": 69.0,
        "direction": "above",
        "no_p_win": 0.98,
        "yes_p_win": 0.02,
        "no_margin": 5.0,
        "n_models": 3,
        "yes_bid": 85,
    }
    score = parse_score_msg(msg, ts_ns=0)
    assert score.nws_max == 0.0
```

**Step 2: Run tests — expect FAIL**

Run: `uv run python -m pytest tests/test_strategy.py -v -k "nws_max"`
Expected: FAIL — `nws_max` not a field on SignalScore

**Step 3: Implement**

In `kalshi/signals.py`, add after `status` field (line 23):
```python
    nws_max: float = 0.0          # NWS hourly forecast max temp (°F), 0.0 if unavailable
```

In `kalshi/signal_actor.py`, add after `status` line (line 67):
```python
        nws_max=float(msg.get("nws_max") or 0.0),
```

Note: `msg.get("nws_max") or 0.0` handles both missing key AND explicit `None` (JSON null).

**Step 4: Run tests — expect PASS**

Run: `uv run python -m pytest tests/test_strategy.py -v -k "nws_max"`
Expected: PASS

**Step 5: Run full test suite to verify backward compatibility**

Run: `uv run python -m pytest --ignore=tests/test_live_confidence.py --ignore=tests/test_live_signals.py -v`
Expected: All tests PASS — existing tests don't set `nws_max`, default 0.0 preserves behavior.

**Step 6: Commit**

```bash
git add kalshi/signals.py kalshi/signal_actor.py tests/test_strategy.py
git commit -m "feat: add nws_max field to SignalScore and parse from signal server"
```

---

### Task 2: Add NWS and low-price config fields to WeatherMakerConfig

**Files:**
- Modify: `kalshi/strategy.py:21-47`
- Test: `tests/test_strategy.py`

**Step 1: Write failing test**

```python
def test_nws_config_defaults():
    cfg = WeatherMakerConfig()
    assert cfg.min_nws_margin == 2.0
    assert cfg.max_nws_model_divergence == 5.0
    assert cfg.nws_missing_cap_multiplier == 0.5
    assert cfg.low_price_threshold_cents == 75
    assert cfg.low_price_cap_multiplier == 0.5
```

**Step 2: Run test — expect FAIL**

Run: `uv run python -m pytest tests/test_strategy.py::TestWeatherMakerConfig::test_nws_config_defaults -v`

**Step 3: Implement**

In `kalshi/strategy.py`, add to `WeatherMakerConfig` after `max_entry_cents` (line 34) and before the Risk section (line 36):

```python
    # NWS filters
    min_nws_margin: float = 2.0              # min °F between nws_max and threshold
    max_nws_model_divergence: float = 5.0    # max °F disagreement between nws_max and consensus
    nws_missing_cap_multiplier: float = 0.5  # scale caps when nws_max unavailable

    # Low-price cap reduction
    low_price_threshold_cents: int = 75      # contracts below this get reduced caps
    low_price_cap_multiplier: float = 0.5    # cap multiplier for low-price contracts
```

**Step 4: Run tests — expect PASS**

Run: `uv run python -m pytest tests/test_strategy.py::TestWeatherMakerConfig -v`

**Step 5: Commit**

```bash
git add kalshi/strategy.py tests/test_strategy.py
git commit -m "feat: add NWS filter and low-price cap config fields"
```

---

### Task 3: Review Tasks 1-2

**Trigger:** Both reviewers start simultaneously when Tasks 1-2 complete.

**Spec review — verify these specific items:**
- [ ] `SignalScore.nws_max` defaults to `0.0` — verify in `kalshi/signals.py`
- [ ] `parse_score_msg()` handles all three cases: `nws_max` present (float), `nws_max: null` (None), `nws_max` missing (key absent) — verify in `kalshi/signal_actor.py` and test assertions
- [ ] New config fields have correct types and defaults — verify values match design doc exactly
- [ ] `n_models` validation at `signal_actor.py:46` is unchanged (still `1 <= n_models <= 3`)
- [ ] No existing tests broken — full suite passes

**Code quality review — verify these specific items:**
- [ ] `float(msg.get("nws_max") or 0.0)` correctly handles `None` from JSON null — trace through: `msg.get("nws_max")` returns `None`, `None or 0.0` evaluates to `0.0`, `float(0.0)` is `0.0`
- [ ] Config field names match exactly what `sweep_params.py` grid will use (field names, not labels)
- [ ] Test assertions verify specific values, not just `is not None`

---

### Task 4: Milestone — Data layer complete

**Present to user:**
- `SignalScore` now has `nws_max` field, backward compatible (defaults to 0.0)
- Signal actor parses `nws_max` from wire, handles null/missing
- Config has 5 new fields ready for filter logic
- All existing tests pass

**Wait for user response before proceeding.**

---

### Task 5: Add NWS hard filters to `should_quote()`

**Files:**
- Modify: `kalshi/strategy.py:50-97` — `should_quote()` function
- Test: `tests/test_strategy.py`

**Step 1: Write failing tests**

```python
class TestNWSFilters:
    """NWS forecast-based hard filters in should_quote()."""

    def test_nws_margin_too_tight_rejects(self):
        """NWS forecast within min_nws_margin of threshold — reject."""
        cfg = WeatherMakerConfig(min_nws_margin=2.0)
        # threshold=54, nws_max=55 → margin=1.0 < 2.0
        score = _make_score(nws_max=55.0, threshold=54.0, direction="above")
        _, passes = should_quote(cfg, score, drift_cities=set())
        assert passes is False

    def test_nws_margin_sufficient_passes(self):
        """NWS forecast well above threshold — passes."""
        cfg = WeatherMakerConfig(min_nws_margin=2.0)
        # threshold=54, nws_max=58 → margin=4.0 >= 2.0
        score = _make_score(nws_max=58.0, threshold=54.0, direction="above")
        _, passes = should_quote(cfg, score, drift_cities=set())
        assert passes is True

    def test_nws_margin_below_direction(self):
        """For below-NO contracts, margin is threshold - nws_max."""
        cfg = WeatherMakerConfig(min_nws_margin=2.0)
        # direction="below", threshold=69, nws_max=70 → margin = 69 - 70 = -1 → abs=1 < 2
        score = _make_score(
            nws_max=70.0, threshold=69.0, direction="below",
            no_p_win=0.98, yes_p_win=0.02,
        )
        _, passes = should_quote(cfg, score, drift_cities=set())
        assert passes is False

    def test_nws_margin_below_direction_safe(self):
        """Below-NO with safe margin passes."""
        cfg = WeatherMakerConfig(min_nws_margin=2.0)
        # direction="below", threshold=69, nws_max=65 → margin = 69 - 65 = 4 >= 2
        score = _make_score(
            nws_max=65.0, threshold=69.0, direction="below",
            no_p_win=0.98, yes_p_win=0.02,
        )
        _, passes = should_quote(cfg, score, drift_cities=set())
        assert passes is True

    def test_nws_model_divergence_rejects(self):
        """NWS wildly disagrees with model consensus — reject."""
        cfg = WeatherMakerConfig(max_nws_model_divergence=5.0)
        # no_margin=7.0 implies consensus ~61 for threshold=54
        # nws_max=55 → divergence = |55 - 61| = 6 > 5
        score = _make_score(
            nws_max=55.0, threshold=54.0, direction="above",
            no_margin=7.0,
        )
        _, passes = should_quote(cfg, score, drift_cities=set())
        assert passes is False

    def test_nws_model_divergence_within_limit_passes(self):
        """NWS agrees with models within tolerance — passes."""
        cfg = WeatherMakerConfig(max_nws_model_divergence=5.0)
        # no_margin=7.0 → consensus ~61, nws_max=59 → divergence=2 < 5
        score = _make_score(
            nws_max=59.0, threshold=54.0, direction="above",
            no_margin=7.0,
        )
        _, passes = should_quote(cfg, score, drift_cities=set())
        assert passes is True

    def test_nws_zero_skips_nws_filters(self):
        """nws_max=0.0 (unavailable) — NWS hard filters skipped, trade allowed."""
        cfg = WeatherMakerConfig(min_nws_margin=2.0, max_nws_model_divergence=5.0)
        score = _make_score(nws_max=0.0)
        _, passes = should_quote(cfg, score, drift_cities=set())
        assert passes is True

    def test_nws_margin_exactly_at_threshold_passes(self):
        """Edge case: margin exactly equals min_nws_margin — passes (not strictly less)."""
        cfg = WeatherMakerConfig(min_nws_margin=2.0)
        # threshold=54, nws_max=56 → margin=2.0 == 2.0
        score = _make_score(nws_max=56.0, threshold=54.0, direction="above")
        _, passes = should_quote(cfg, score, drift_cities=set())
        assert passes is True
```

**Step 2: Run tests — expect FAIL**

Run: `uv run python -m pytest tests/test_strategy.py::TestNWSFilters -v`

**Step 3: Implement**

In `kalshi/strategy.py`, in `should_quote()`, add after the model spread check (line 91) and before the max entry check (line 93):

```python
    # NWS forecast checks (skip if nws_max unavailable)
    if score.nws_max > 0.0:
        # Compute NWS margin (direction-aware)
        if score.direction == "above":
            nws_margin = score.nws_max - score.threshold
        else:  # "below"
            nws_margin = score.threshold - score.nws_max

        # Reject if NWS margin is too tight
        if abs(nws_margin) < config.min_nws_margin:
            return side, False

        # Reject if NWS diverges too much from model consensus
        # Derive consensus from no_margin: consensus = threshold + no_margin (above)
        #                                  consensus = threshold - no_margin (below)
        if score.direction == "above":
            consensus = score.threshold + score.no_margin
        else:
            consensus = score.threshold - score.no_margin
        if abs(score.nws_max - consensus) > config.max_nws_model_divergence:
            return side, False
```

**Step 4: Run tests — expect PASS**

Run: `uv run python -m pytest tests/test_strategy.py::TestNWSFilters -v`

**Step 5: Run full test suite**

Run: `uv run python -m pytest --ignore=tests/test_live_confidence.py --ignore=tests/test_live_signals.py -v`
Expected: All PASS — existing tests use `nws_max=0.0` (default), NWS checks skipped.

**Step 6: Commit**

```bash
git add kalshi/strategy.py tests/test_strategy.py
git commit -m "feat: add NWS margin and divergence hard filters to should_quote"
```

---

### Task 6: Review Task 5

**Trigger:** Both reviewers start when Task 5 completes.

**Spec review — verify these specific items:**
- [ ] NWS margin computation is direction-aware: `above` → `nws_max - threshold`, `below` → `threshold - nws_max`
- [ ] Consensus derivation from `no_margin` matches signal server semantics (check design doc payload example: DC threshold=69, no_margin=-5.4, consensus should be ~63.6 for above)
- [ ] `nws_max=0.0` (unavailable) skips ALL NWS hard filters — verify the `if score.nws_max > 0.0` guard
- [ ] Margin check uses `abs(nws_margin) < min_nws_margin` (strictly less than — exactly at threshold passes)
- [ ] Divergence check uses `abs(nws_max - consensus) > max_nws_model_divergence` (strictly greater than)
- [ ] Filter order: NWS checks run AFTER model spread, BEFORE max entry — verify line positions

**Code quality review — verify these specific items:**
- [ ] No duplicate direction-checking logic — the `if/else` for direction appears once per filter
- [ ] Tests cover: above-tight, above-safe, below-tight, below-safe, divergence-reject, divergence-pass, unavailable-skip, exact-boundary
- [ ] Existing `TestFilterLayer` tests all still pass unchanged

**Validation Data:**
- DC example from design: threshold=69, nws_max=70, direction="below" → margin = 69-70 = -1, abs=1 < 2.0 → REJECT. Verify test `test_nws_margin_below_direction` matches.

---

### Task 7: Milestone — NWS filters functional

**Present to user:**
- `should_quote()` now rejects contracts with tight NWS margins or large NWS/model divergence
- Backward compatible: `nws_max=0.0` skips all NWS checks
- DC scenario from the blind spot would now be caught: margin=1°F < min_nws_margin=2°F → rejected

**Wait for user response before proceeding.**

---

### Task 8: Add cap multiplier logic to `_reprice_ladder()`

**Files:**
- Modify: `kalshi/strategy.py:346-417` — `_reprice_ladder()` method
- Test: `tests/test_strategy.py`

**Step 1: Write failing tests**

Add a new helper function and test class. Since `_reprice_ladder` is a strategy method requiring NT infrastructure, test the multiplier computation as a standalone function:

```python
from kalshi.strategy import compute_cap_multiplier

class TestCapMultiplier:
    def test_full_caps_with_nws_and_high_price(self):
        """NWS available, price above threshold — full caps."""
        m = compute_cap_multiplier(
            nws_max=60.0,
            anchor_cents=85,
            low_price_threshold_cents=75,
            low_price_cap_multiplier=0.5,
            nws_missing_cap_multiplier=0.5,
        )
        assert m == 1.0

    def test_low_price_reduces_caps(self):
        """Price below threshold — caps halved."""
        m = compute_cap_multiplier(
            nws_max=60.0,
            anchor_cents=70,
            low_price_threshold_cents=75,
            low_price_cap_multiplier=0.5,
            nws_missing_cap_multiplier=0.5,
        )
        assert m == 0.5

    def test_nws_missing_reduces_caps(self):
        """NWS unavailable — caps halved."""
        m = compute_cap_multiplier(
            nws_max=0.0,
            anchor_cents=85,
            low_price_threshold_cents=75,
            low_price_cap_multiplier=0.5,
            nws_missing_cap_multiplier=0.5,
        )
        assert m == 0.5

    def test_both_penalties_stack(self):
        """Low price AND missing NWS — caps quartered."""
        m = compute_cap_multiplier(
            nws_max=0.0,
            anchor_cents=70,
            low_price_threshold_cents=75,
            low_price_cap_multiplier=0.5,
            nws_missing_cap_multiplier=0.5,
        )
        assert m == 0.25

    def test_price_exactly_at_threshold_no_penalty(self):
        """Price exactly at threshold — no low-price penalty."""
        m = compute_cap_multiplier(
            nws_max=60.0,
            anchor_cents=75,
            low_price_threshold_cents=75,
            low_price_cap_multiplier=0.5,
            nws_missing_cap_multiplier=0.5,
        )
        assert m == 1.0

    def test_custom_multipliers(self):
        """Configurable multiplier values."""
        m = compute_cap_multiplier(
            nws_max=0.0,
            anchor_cents=50,
            low_price_threshold_cents=80,
            low_price_cap_multiplier=0.3,
            nws_missing_cap_multiplier=0.7,
        )
        assert m == pytest.approx(0.21)
```

**Step 2: Run tests — expect FAIL**

Run: `uv run python -m pytest tests/test_strategy.py::TestCapMultiplier -v`

**Step 3: Implement `compute_cap_multiplier()`**

In `kalshi/strategy.py`, add after `check_risk_caps()` (after line 128):

```python
def compute_cap_multiplier(
    nws_max: float,
    anchor_cents: int,
    low_price_threshold_cents: int,
    low_price_cap_multiplier: float,
    nws_missing_cap_multiplier: float,
) -> float:
    """Compute position cap multiplier based on NWS availability and price.

    Multipliers stack: a cheap contract with missing NWS data gets both penalties.
    Returns a value between 0.0 and 1.0 (inclusive).
    """
    multiplier = 1.0
    if nws_max == 0.0:
        multiplier *= nws_missing_cap_multiplier
    if anchor_cents < low_price_threshold_cents:
        multiplier *= low_price_cap_multiplier
    return multiplier
```

**Step 4: Run tests — expect PASS**

Run: `uv run python -m pytest tests/test_strategy.py::TestCapMultiplier -v`

**Step 5: Wire into `_reprice_ladder()`**

In `kalshi/strategy.py`, in `_reprice_ladder()`, add the multiplier computation before the ladder loop. Between lines 376-378 (after `city_exposure` is computed, before `compute_ladder`), add:

```python
        # Compute cap multiplier from NWS availability and price
        cap_mult = compute_cap_multiplier(
            nws_max=score.nws_max,
            anchor_cents=bid_cents,
            low_price_threshold_cents=self._config.low_price_threshold_cents,
            low_price_cap_multiplier=self._config.low_price_cap_multiplier,
            nws_missing_cap_multiplier=self._config.nws_missing_cap_multiplier,
        )
        effective_market_cap = self._config.market_cap_pct * cap_mult
        effective_city_cap = self._config.city_cap_pct * cap_mult
```

Then change the `check_risk_caps` call (lines 393-394) from:
```python
                market_cap_pct=self._config.market_cap_pct,
                city_cap_pct=self._config.city_cap_pct,
```
to:
```python
                market_cap_pct=effective_market_cap,
                city_cap_pct=effective_city_cap,
```

**Step 6: Run full test suite**

Run: `uv run python -m pytest --ignore=tests/test_live_confidence.py --ignore=tests/test_live_signals.py -v`
Expected: All PASS

**Step 7: Commit**

```bash
git add kalshi/strategy.py tests/test_strategy.py
git commit -m "feat: add cap multiplier for NWS-missing and low-price contracts"
```

---

### Task 9: Review Task 8

**Trigger:** Both reviewers start when Task 8 completes.

**Spec review — verify these specific items:**
- [ ] `compute_cap_multiplier()` multipliers stack: `0.5 * 0.5 = 0.25` when both penalties apply
- [ ] Low price check uses strict less-than (`<`), so `anchor_cents == low_price_threshold_cents` gets NO penalty
- [ ] `_reprice_ladder()` computes multiplier BEFORE the ladder loop and applies it to BOTH `market_cap_pct` and `city_cap_pct`
- [ ] `check_risk_caps()` itself is NOT modified — multiplier is applied upstream

**Code quality review — verify these specific items:**
- [ ] `compute_cap_multiplier` is a pure function (no side effects, no strategy access) — testable in isolation
- [ ] The `effective_market_cap` / `effective_city_cap` variables are computed once, not per-level
- [ ] Test covers: full caps, low-price only, NWS-missing only, both stacking, exact boundary, custom multipliers
- [ ] `pytest.approx` used for floating point comparison in `test_custom_multipliers`

---

### Task 10: Milestone — Cap reduction functional

**Present to user:**
- Contracts below 75¢ get 50% caps
- Missing NWS data gets 50% caps
- Both penalties stack (25% caps)
- All parameters configurable via `WeatherMakerConfig`
- Full test coverage

**Wait for user response before proceeding.**

---

### Task 11: Add new params to sweep grid

**Files:**
- Modify: `scripts/sweep_params.py:27-33`

**Step 1: Add sweep entries**

Add to `_SWEEP_GRID` list:

```python
    ("min_nws_margin",           "min_nws_margin",           [1.0, 2.0, 3.0, 5.0]),
    ("max_nws_model_divergence", "max_nws_model_divergence", [3.0, 5.0, 8.0]),
    ("nws_missing_cap_mult",     "nws_missing_cap_multiplier", [0.25, 0.5, 0.75]),
    ("low_price_threshold",      "low_price_threshold_cents",  [60, 75, 85]),
    ("low_price_cap_mult",       "low_price_cap_multiplier",   [0.25, 0.5, 0.75]),
```

**Step 2: Verify config fields exist**

Run: `uv run python -c "from kalshi.strategy import WeatherMakerConfig; c = WeatherMakerConfig(min_nws_margin=1.0, low_price_threshold_cents=60); print('OK')"`

**Step 3: Commit**

```bash
git add scripts/sweep_params.py
git commit -m "feat: add NWS and low-price params to backtest sweep grid"
```

---

### Task 12: Review Task 11

**Trigger:** Both reviewers start when Task 11 completes.

**Spec review — verify these specific items:**
- [ ] Sweep grid field names match `WeatherMakerConfig` field names exactly (not labels)
- [ ] Value ranges are reasonable: margin 1-5°F, divergence 3-8°F, multipliers 0.25-0.75, price thresholds 60-85¢
- [ ] Total run count increase is manageable (adding 4+3+3+3+3 = 16 runs)

**Code quality review — verify these specific items:**
- [ ] Grid entries follow existing tuple format `(label, field_name, values)`
- [ ] No duplicate entries in the grid

---

### Task 13: Final Milestone — Complete

**Present to user:**
- All features implemented and tested:
  - `nws_max` field on SignalScore, parsed from wire
  - NWS margin and divergence hard filters in `should_quote()`
  - Cap multiplier for NWS-missing and low-price contracts
  - Sweep grid updated with all new params
- Backward compatible: existing behavior unchanged when `nws_max=0.0`
- Ready for deployment and backtest sweeps

**Wait for user response. Suggest deploying to droplet and running a sweep.**
