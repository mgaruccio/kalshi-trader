# Product Requirements Document (PRD): Nautilus Trader Adapter for Kalshi

## 1. Overview
The objective is to build a robust, production-grade adapter connecting the Nautilus Trader framework to the Kalshi prediction market exchange. The adapter will consist of a Data Client, an Execution Client, and an Instrument Provider, all adhering to the strict typing, precision, and state machine requirements of Nautilus.

## 2. Architecture & Concurrency
- **Asynchronous Execution:** Avoid blocking the main asyncio event loop.
- **WebSocket First:** Market data (orderbooks, ticks) and execution reports (fills, cancels) MUST be streamed via Kalshi WebSockets rather than polled.
- **Thread Pool Management:** If REST endpoints must be used for order placement/cancellation or fallback, utilize a dedicated `ThreadPoolExecutor` with a safely bounded max worker count and strict network timeouts to prevent deadlocks.
- **Reconciliation:** Implement periodic REST polling to reconcile order states and portfolio positions, complementing WebSocket updates.

## 3. Data Client Requirements
- **WebSocket Subscriptions:** Stream depth/quote updates and translate them into Nautilus `OrderBookDelta`, `QuoteTick`, or `TradeTick` events.
- **Precision Matching:** Respect the `size_precision` and `price_precision` defined by the `InstrumentProvider`. Size must be integral (0 precision), and prices must reflect cents (2 decimal places) accurately, avoiding implicit float-to-int truncations.

## 4. Execution Client Requirements
- **Order Mapping (Yes vs. No):** Kalshi does not support traditional "shorting". To take the opposing view on an event, one must BUY the "No" contract.
  - The adapter must recognize distinct instruments for Yes and No contracts (e.g., `<TICKER>-YES` and `<TICKER>-NO`), OR map `OrderSide.SELL` to buying the "No" contract transparently. The recommended approach is to map Yes and No as distinct instruments, so standard `OrderSide.BUY` can be used for both. Selling an instrument then unambiguously means reducing long exposure to that side.
- **Execution Prices vs. Quantities:** Fix the critical bug where fill prices were mistakenly assigned to filled quantities.
- **Order States:** Correctly handle Nautilus `ACCEPTED`, `PENDING_CANCEL`, and `CANCELED` states based on explicit server acknowledgments via REST or Websocket.
- **Market Orders:** Market orders must not pass limit price fields (`yes_price` or `no_price`) to Kalshi API endpoints unless emulating market orders via aggressive limits.
- **Portfolio Loading:** Implement `generate_order_status_reports` and `generate_position_status_reports` to initialize the OMS state from the live API upon startup.

## 5. Instrument Provider
- **Dynamic Configuration:** Accept authentication credentials and endpoints via a `Configuration` object, not hardcoded paths.
- **Margin & Collateral:** Reflect the fully-collateralized nature of Kalshi by setting appropriate `margin_init` and `margin_maint` or handling risk limits correctly in the adapter configuration.

## 6. Secrets & Authentication
- Remove any hardcoded `~/.config/kalshi/` file paths.
- Rely on Nautilus framework-level configuration models (e.g. env vars or secure config injection) to supply API keys and private keys.

## 7. Sample Strategy Guidelines
- A complete sample strategy must be provided that is mechanically sound.
- It must gracefully handle `QuoteTick` or `OrderBook` events, limit order placement, and properly manage its active orders by responding to `OrderFilled`, `OrderCanceled`, and `OrderRejected` events.
