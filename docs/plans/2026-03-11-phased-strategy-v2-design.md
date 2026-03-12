---
date: 2026-03-11
topic: phased-strategy-v2
supersedes: docs/plans/2026-03-10-phased-weather-strategy-design.md
---

# Phased Market-Making Weather Strategy V2

## Problem Statement

The current `WeatherStrategy` has three gaps that leave edge on the table:

1. **Single-shot entry evaluation**: When a `ModelSignal` arrives, the strategy checks the ask price once and skips if it exceeds `max_cost_cents`. There is no re-evaluation if price later falls to an attractive level.

2. **Reactive profit-target placement**: The strategy waits for the bid to reach 97c before submitting a sell order. This means a sell is never resting in the book — it can only fill at exactly the moment the strategy observes the tick, and may miss fills during gaps between ticks.

3. **Tick stream unused for entries**: `on_quote_tick` only drives `_check_profit_targets`. The tick stream is not used to find entry opportunities as prices move.

The result is a strategy that misses entries when price temporarily dips after a signal arrives, and misses exits when bid briefly touches target between tick observations.

## Design Overview

Replace single-shot evaluation with a 2-phase market-making approach. Both phases place resting GTC orders and let the NautilusTrader matching engine fill them. The strategy never chases price — it fishes at pre-set levels.

### Phase 1 — Open Spread (tomorrow contracts, ~10 AM EST window)

Immediately after markets open for the next day, prices are volatile and spreads are wide. Phase 1 fishes for cheap fills during this period.

**Trigger**: `ModelSignal` for a ticker where `settlement_date > today` (a "tomorrow" contract), received within `open_spread_window_minutes` of the market-open anchor time.

**Action**:
- Place resting GTC buy orders at a ladder of deep-discount prices: 45c, 50c, 55c (tunable).
- Lower p_win gate (0.90) to cast a wider net — early signals are less certain, fills at 45c compensate.
- One ladder per ticker, tracked in `_open_spread_placed` set. Re-arrivals of the same signal are no-ops.
- After the window expires, a timer fires to cancel all unfilled Phase 1 orders and transition to Phase 2 evaluation.

**Rationale**: Opening price discovery on weather contracts frequently produces temporary dips of 5-15c below the stabilized price. Resting orders at 45-55c cost almost nothing to place and catch these dips automatically. The V1 design placed spread orders at 90-94c — too close to stable market prices, defeating the purpose of the deep-discount phase.

### Phase 2 — Stable Ladder (post-window, or today contracts)

After Phase 1 expires, or for contracts settling today (which open already-stable), Phase 2 places ladders relative to the current bid.

**Trigger**: `ModelSignal` with `p_win >= stable_min_p_win` (0.95).

**Action**:
- Place resting GTC buy orders at offsets from current bid: bid, bid-1c, bid-3c, bid-5c, bid-10c (tunable).
- `stable_size` contracts per level (default 3).
- These orders sit in the book and fill automatically as price moves.

**On fill** (`on_order_filled`):
- Immediately place a resting GTC sell at 97c for the filled quantity.
- This replaces the old `_check_profit_targets` poll — the sell rests in the book from fill time, not from the next tick observation.

**Periodic refresh** (every 5 minutes):
- Cancel unfilled buy orders for all active tickers.
- Recompute available capital.
- Deploy fresh ladders at updated bid-relative levels.
- This keeps the ladder anchored to current market price without manual intervention.

The `_eligible_signals` dict stores qualifying `ModelSignal` objects so the refresh loop can redeploy without waiting for a new signal emission.

## Capital Conservation — 2 AM Back-off

At 2 AM EST (07:00 UTC), a scheduled timer fires:
- Cancel all resting buy orders across all tickers.
- Stop the periodic refresh loop (no new ladders placed until resume).
- Existing positions and resting sell orders at 97c are untouched — they continue to work.

At 2 PM EST (19:00 UTC), the strategy resumes accepting new signals and deploying Phase 2 ladders for today's contracts.

**Rationale**: Kalshi weather contracts settle at ~3 PM EST on observation day. By 2 AM, the remaining overnight window is not worth deploying additional capital into — open positions can hold. Freeing capital overnight makes it available for Phase 1 on the next day's contracts.

## What Stays the Same

- **NO-only filter**: `signal.side != "no"` exits immediately. Defense-in-depth against accidental YES exposure.
- **DangerAlert CRITICAL exits**: `_evaluate_exit()` logic unchanged. CRITICAL alerts trigger immediate sell at best bid. `_danger_exited` set prevents retry loops.
- **ModelSignal determines eligible tickers**: FeatureActor emits signals; strategy never self-initiates without a signal.
- **Max 20 contracts per ticker**: enforced at entry, not bypassed by multi-level ladders (aggregate across levels counted).
- **Backtest compatibility**: Pre-computed signals feed Phase 1 and Phase 2 identically. Timer-based phase transitions use NT's BacktestEngine clock.

## Config Parameters

```python
# Phase 1 — open spread
open_spread_enabled: bool = True
open_spread_prices_cents: tuple[int, ...] = (45, 50, 55)
open_spread_size: int = 3                   # contracts per price level
open_spread_min_p_win: float = 0.90         # lower gate for opening fills
open_spread_window_minutes: int = 30        # window duration from market open

# Phase 2 — stable ladder
stable_min_p_win: float = 0.95
stable_ladder_offsets_cents: tuple[int, ...] = (0, 1, 3, 5, 10)
stable_size: int = 3                        # contracts per ladder level
sell_target_cents: int = 97                 # resting sell price on fill
refresh_interval_minutes: int = 5          # ladder refresh cadence

# Capital conservation
backoff_hour_utc: int = 7                   # 2 AM EST — stop deploying buys
resume_hour_utc: int = 19                   # 2 PM EST — resume for today contracts

# Global
max_position_per_ticker: int = 20
danger_exit_enabled: bool = True
```

## Architecture

All changes are confined to `weather_strategy.py`. No other files change.

### Key State

| Attribute | Type | Purpose |
|---|---|---|
| `_open_spread_placed` | `set[str]` | Tickers that have had Phase 1 orders placed; prevents duplicate ladders |
| `_eligible_signals` | `dict[str, ModelSignal]` | Latest qualifying signal per ticker; used by refresh loop |
| `_resting_buy_orders` | `dict[str, list[ClientOrderId]]` | Active buy order IDs per ticker; needed for cancellation |
| `_latest_quotes` | `dict[str, QuoteTick]` | Latest tick per instrument; provides bid for Phase 2 anchoring |
| `_positions_info` | `dict[str, dict]` | Held contracts per ticker; unchanged from current |
| `_danger_exited` | `set[str]` | Danger-exited tickers; unchanged from current |

### Key Methods

**`_evaluate_entry(signal)`**
- Routes to Phase 1 or Phase 2 based on settlement date and window state.
- Phase 1: calls `_place_open_spread(signal)` if not already placed.
- Phase 2: calls `_place_stable_ladder(signal)` if signal qualifies.

**`on_order_filled(event)`**
- Tracks position changes (buy increments, sell decrements).
- On buy fill: immediately places resting GTC sell at `sell_target_cents`.
- Removes filled order ID from `_resting_buy_orders`.
- Syncs positions to FeatureActor.

**`_on_refresh()`** (timer callback, every `refresh_interval_minutes`)
- Skips if currently in back-off window.
- Cancels unfilled buy orders via `cancel_all_orders(instrument_id, order_side=OrderSide.BUY)`.
- Replaces ladders at fresh bid-relative prices for all tickers in `_eligible_signals`.

**`_on_phase1_expire(ticker)`** (timer callback, per-ticker)
- Cancels unfilled Phase 1 orders for the given ticker.
- Does not add ticker to `_open_spread_placed` — Phase 2 can still activate on subsequent signals.

**`_on_backoff()`** / **`_on_resume()`** (daily timers at fixed UTC hours)
- Back-off: cancels all resting buys, pauses refresh.
- Resume: restarts refresh timer.

### NT API Points

- GTC orders fill automatically when the matching engine's price crosses the order price — no polling needed.
- `cancel_all_orders(instrument_id=..., order_side=OrderSide.BUY)` cancels only buy orders, leaving resting sells in place.
- `clock.set_timer(name, interval, callback)` for periodic refresh.
- `clock.set_time_alert(name, alert_time, callback)` for one-shot phase transitions and daily back-off/resume.

## What Changes vs V1 Design (2026-03-10)

| Dimension | V1 | V2 |
|---|---|---|
| Phase 1 prices | 90, 92, 94c | 45, 50, 55c |
| Phase 1 p_win gate | 0.93 | 0.90 |
| Phase 2 entry pricing | ask price at signal time | bid-relative ladder |
| Sell placement | poll on tick >= 97c | resting GTC on fill |
| Ladder refresh | none | every 5min |
| Capital conservation | none | 2 AM back-off |
| `_eligible_signals` | absent | stores signals for refresh |

The core architectural insight is unchanged from V1: use GTC resting orders rather than taker fills. V2 corrects the Phase 1 price levels (they were too close to market to serve as a deep-discount fishing layer) and adds the refresh mechanism that V1 lacked.
