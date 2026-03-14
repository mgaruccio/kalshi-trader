---
date: 2026-03-14
topic: weather-market-maker
---

# Weather Market Maker — Design

## What We're Building

A two-layer KXHIGH trading system on NautilusTrader:

1. **Forecast filter** — consumes win probabilities from an external signal server to identify markets where the outcome is near-certain (configurable confidence threshold).
2. **Microstructure execution** — passive market making with layered limit orders on the forecast-confirmed side in those high-conviction markets.

The system runs as a TradingNode with three NT components: a SignalActor (signal server integration), a WeatherMakerStrategy (filter + quote management + risk), and the existing Kalshi adapter (market data + order routing). Fully backtestable by replaying historical signal scores alongside recorded market data.

## Why This Approach

- **Forecast filter + microstructure** beats either alone. Pure forecast trading fights the spread. Pure market making in prediction markets has adverse selection risk from informed participants. Combining them restricts market making to markets where direction is ~known, eliminating the primary risk.
- **Actor + Strategy separation** keeps signal ingestion decoupled from trading logic. The Actor publishes custom Data events on the MessageBus; the Strategy subscribes. In backtesting, the BacktestEngine replays recorded signal events — same strategy code, no code path divergence.
- **Layered quotes** instead of single orders increase fill probability across the book depth while managing exposure through configurable ladder parameters.

## Architecture

```
┌─────────────────────────────────────────────────────────┐
│                     TradingNode                         │
│                                                         │
│  ┌──────────────┐    MessageBus     ┌────────────────┐  │
│  │ SignalActor   │── SignalScore ──▶│ WeatherMaker   │  │
│  │              │── ForecastDrift ─▶│ Strategy       │  │
│  │ (signal srv) │                   │                │  │
│  └──────────────┘                   │ - filter       │  │
│                                     │ - quote mgr    │  │
│  ┌──────────────┐                   │ - risk mgr     │  │
│  │ KalshiData   │── QuoteTicks ──▶│ - exit mgr     │  │
│  │ Client       │                   │                │  │
│  └──────────────┘                   └──────┬─────────┘  │
│                                            │            │
│  ┌──────────────┐                   ┌──────▼─────────┐  │
│  │ KalshiExec   │◀── orders ───────│ OrderFactory   │  │
│  │ Client       │                   └────────────────┘  │
│  └──────────────┘                                       │
└─────────────────────────────────────────────────────────┘
```

## Key Decisions

### Signal Actor

- **WebSocket-driven**: subscribes to `["scores", "alerts"]` on the signal server (`ws://localhost:8000/v1/stream`). No polling.
- **Bootstrap on start**: single REST call to `GET /v1/trading/scores` to seed initial state.
- **Custom data types**: `SignalScore` and `ForecastDrift` — msgspec Structs implementing NT `Data`, serializable to Parquet for backtest replay.
- **Rationale**: event-driven means the strategy reacts to score changes as they happen rather than on a fixed timer. Bootstrap ensures the strategy has state immediately on start.

### Filter Layer

A contract is quotable when ALL conditions pass:

1. `p_win >= confidence_threshold` (default 0.95) on the confirmed side
2. `n_models >= min_models` (default 2) — require sufficient ensemble coverage
3. Drift state clear (configurable: `"pause"` excludes city, `"tighten"` raises threshold, `"ignore"` does nothing)
4. Contract status is `"open"` (live order book)

On **filter entry**: begin quoting. On **filter exit**: cancel all resting orders for that contract. Existing positions are held (don't panic-sell a high-probability contract over a small score dip).

### Quote Manager

- **Side**: if NO wins the filter, trade the NO contract directly. Kalshi treats YES and NO as separate instruments with independent order books — no YES↔NO price conversion.
- **Anchor**: current bid on our contract (sit at the bid with everyone else).
- **Ladder**: `ladder_depth` levels spaced `ladder_spacing` cents apart, descending from anchor.
- **Reprice**: triggered when bid moves by more than `reprice_threshold` cents. Cancel-and-replace (Kalshi doesn't support order modification).
- **On fill**: replenish the level if still within risk caps and filter still passes.
- **Rate limit**: respects existing 10 writes/sec TokenBucket in the adapter.

### Risk Manager

- **Per-market cap**: `market_cap_pct` of account balance (default 0.20). Checked before placing/replenishing. Includes resting order exposure + filled position.
- **Per-city cap**: `city_cap_pct` of account balance (default 0.33). Summed across all contracts for a city.
- **No portfolio-level cap**: account balance is the natural limit.
- **On breach**: reduce quantity or skip levels. Never exceed caps.

### Exit Manager

- **Price-based exit**: when the contract price reaches `exit_price_cents` (configurable, 95-99 range), close the position with a market order. Locks in profit, frees capital.
- **Held to settlement**: if exit threshold is never hit, positions settle naturally at 0 or 100.
- **Future enhancement**: more situational exit intelligence (e.g., time-to-settlement, score degradation).

### Backtesting

- **Market data**: QuoteTicks from existing ParquetDataCatalog (~23GB collected by the running collector).
- **Signal data**: historical scores via `GET /v1/trading/scores/backfill?lead_idx=1&start_date=2026-01-01`. Loaded as custom `SignalScore` data events.
- **Engine setup**: `BacktestEngine` replays both streams. SignalActor receives replayed events instead of connecting to live WebSocket. Same strategy code path.
- **Venue config**: `OmsType.NETTING`, `AccountType.CASH`, starting balance configurable.

## Configuration

```python
class WeatherMakerConfig(StrategyConfig):
    # Filter
    confidence_threshold: float = 0.95
    min_models: int = 2
    drift_response: str = "pause"           # "pause" | "tighten" | "ignore"
    drift_threshold_delta: float = 0.02

    # Quote ladder
    ladder_depth: int = 3
    ladder_spacing: int = 1                 # cents
    level_quantity: int = 10                # contracts per level
    reprice_threshold: int = 1              # cents

    # Risk
    market_cap_pct: float = 0.20
    city_cap_pct: float = 0.33

    # Exit
    exit_price_cents: int = 97

    # Signal server
    signal_server_url: str = "ws://localhost:8000/v1/stream"
    signal_server_http: str = "http://localhost:8000"
```

## External Prerequisites

- **Signal server**: running at localhost:8000 with `scores` WebSocket channel — available
- **Historical backfill**: `GET /v1/trading/scores/backfill?lead_idx=1&start_date=2026-01-01` — being built
- **Kalshi adapter**: data + execution clients — built and tested on master
- **Data catalog**: QuoteTick collection running on production droplet — available
- **Kalshi credentials**: for live trading (not needed for backtesting)

## Open Questions

- Exact format of the backfill endpoint response (Parquet? JSON? Paginated?) — confirm with signal server agent
- Should the Actor subscribe to `observations` and `markets` WebSocket channels too, or are `scores` + `alerts` sufficient?
- Fill model configuration for backtesting (slippage, partial fill probability) — tune during backtest iteration

## Next Steps

→ writing-plans skill for implementation plan
