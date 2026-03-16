---
date: 2026-03-16
topic: nws-filters-and-risk-caps
---

# NWS Forecast Filters & Low-Price Risk Caps

## What We're Building

Two safety features for the WeatherMaker strategy:

1. **NWS-aware filtering** — use the new `nws_max` field from the signal server to hard-filter contracts where the NWS hourly forecast shows a tight margin or disagrees significantly with model consensus. When NWS data is unavailable, reduce position caps as a soft penalty.

2. **Low-price position cap reduction** — contracts trading below a configurable price threshold get reduced position caps, preventing over-concentration in uncertain markets.

## Motivation

DC T69 NO revealed a blind spot: the ensemble models confidently predicted the high would exceed 69°F, but the NWS local forecast had it at 70°F — barely clearing. The local forecaster knew about dynamics (sea breeze, frontal timing) the general models missed. The bot accumulated 8 contracts on a 1°F margin.

The signal server is adding `nws_max` (NWS hourly forecast max temp) and folding it into consensus/margin calculations. The trader needs to accept this field and layer on additional safety filters.

## Why This Approach

Extended the existing filter chain (`should_quote()` + `check_risk_caps()`) rather than introducing a risk tier abstraction. The logic is simple conditionals — an abstraction layer would add complexity without clarity. All new params live on `WeatherMakerConfig` so they're immediately sweepable in backtests.

## Key Decisions

- **`nws_max` on SignalScore**: `float = 0.0` where 0.0 means unavailable (consistent with existing model fields `emos_no`, `ngboost_no`, `drn_no`)
- **Hard filters in `should_quote()`**: NWS margin and divergence checks reject before any orders are placed
- **Cap multipliers stack**: a cheap contract with missing NWS data gets `nws_missing_cap_multiplier * low_price_cap_multiplier` (e.g., 0.25x normal caps)
- **NWS missing = soft negative**: reduce caps, don't block trading entirely
- **All values configurable**: for backtest parameter sweeps

## Design Details

### New Config Fields (`WeatherMakerConfig`)

```python
# NWS filters
min_nws_margin: float = 2.0              # min °F between nws_max and threshold; skip if tighter
max_nws_model_divergence: float = 5.0    # max °F disagreement between nws_max and consensus
nws_missing_cap_multiplier: float = 0.5  # scale caps when nws_max unavailable

# Low-price cap reduction
low_price_threshold_cents: int = 75      # contracts below this price get reduced caps
low_price_cap_multiplier: float = 0.5    # cap multiplier for low-price contracts
```

### Hard Filters (`should_quote`)

When `nws_max > 0` (available):
- Compute NWS margin: distance from `nws_max` to `threshold`, direction-aware
- If `abs(nws_margin) < min_nws_margin` → reject
- If `abs(nws_max - consensus) > max_nws_model_divergence` → reject

When `nws_max == 0` (unavailable): no hard filter — caps reduced downstream.

### Cap Modification

A multiplier is computed before calling `check_risk_caps`, applied to `market_cap_pct` and `city_cap_pct`:

```
multiplier = 1.0
if nws_max == 0:                          multiplier *= nws_missing_cap_multiplier
if anchor_cents < low_price_threshold_cents: multiplier *= low_price_cap_multiplier
```

### What Doesn't Change

- Core signal consumption (consensus/margin already improved server-side)
- Order ladder logic
- Exit logic
- Circuit breaker

## Open Questions

- Exact default values for `min_nws_margin` and `max_nws_model_divergence` — will tune via backtest sweeps
- Whether `nws_max` field name on the wire matches `nws_max` or `nws_hourly` (user initially said `nws_hourly`, then clarified the field semantics suggest `nws_max`)

## Next Steps

→ writing-plans skill for implementation details
