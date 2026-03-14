---
date: 2026-03-10
topic: phased-weather-strategy
---

# Phased Weather Strategy

## What We're Building

Replace the current single-mode WeatherStrategy with a phased strategy that treats tomorrow contracts (just opened) differently from today contracts (settling today). Two entry modes:

1. **Open-spread phase** (tomorrow contracts, first 30min after open): Place laddered resting GTC buys across all confident tickers at below-market prices to catch opening turbulence. Small size, wide spread of tickers.

2. **Stable phase** (after 30min, or any today contract): Standard maker-only entry at market price when model confidence is high enough.

All entries are GTC limit orders (maker-only). No taker orders ever.

## Why This Approach

Empirical tick data (Mar 10 2026, 24 T-type tickers) shows:
- 96% of tickers move >2c in first hour after open (mean 21c)
- Spreads start at 14c, collapse to 5c by T+30min, stabilize at 2-4c by T+90min
- Taker orders at open cross a 14c+ spread — pure waste
- Resting buys at favorable prices catch downswings during price discovery

Single-mode strategy that waits for stable prices misses the opening turbulence edge entirely. Phased approach captures both the opening mispricing and the stable-market edge.

## Key Decisions

- **Phase detection via ticker date + time since first tick**: Parse settlement date from ticker, compare to engine clock. `settlement_date > today` + `minutes_since_first_tick < 30` = open-spread phase.
- **Open-spread orders placed on first ModelSignal**: When FeatureActor emits a signal for a tomorrow ticker that hasn't been spread-ordered yet, place the full ladder immediately.
- **Unfilled spread orders cancelled after phase window**: Timer fires at T+30min to cancel unfilled open-spread orders, transitioning to stable-phase evaluation.
- **All config in WeatherStrategyConfig**: Every tunable is a config field.

## Config Parameters

```python
# Open-spread phase (tomorrow contracts, first 30min)
open_spread_enabled: bool = True
open_spread_prices_cents: tuple[int, ...] = (90, 92, 94)
open_spread_size: int = 3                    # contracts per price level
open_spread_min_p_win: float = 0.93          # lower gate for cheap fills
open_spread_window_minutes: int = 30         # duration of spread phase

# Stable phase (post-30min, or today contracts)
stable_min_p_win: float = 0.95
stable_max_cost_cents: int = 96
stable_size: int = 5
sell_target_cents: int = 97

# Global
max_position_per_ticker: int = 20
danger_exit_enabled: bool = True
```

## Entry Flow

```
ModelSignal received for ticker X:
  1. Parse settlement_date from ticker
  2. Is it a "tomorrow" contract? (settlement_date > today from engine clock)
     YES → Is it within open_spread_window of first quote?
       YES → Has spread been placed for this ticker already?
         NO  → Place ladder of GTC buys at open_spread_prices
         YES → Skip (already fishing)
       NO  → Fall through to stable eval
     NO → Stable eval (today contract)
  3. Stable eval: p_win >= stable_min_p_win AND best ask <= stable_max_cost_cents
     → Place GTC buy at ask price, stable_size contracts
```

## What Changes

- `weather_strategy.py`: New config fields, `_open_spread_placed` set, `_open_spread_orders` dict, phase detection in `_evaluate_entry`, timer to cancel unfilled spread orders, maker-only everywhere
- `backtest_runner.py`: Pass new config to WeatherStrategyConfig
- Nothing else: FeatureActor, data_types, adapter unchanged

## Open Questions

- Timer cancellation in backtest: NT BacktestEngine supports clock.set_timer — use to fire at T+30min per ticker for unfilled order cleanup.
