#!/usr/bin/env python
"""Experiment 5: Phased Strategy Backtest.

Runs the full NT pipeline with the new phased WeatherStrategy against
both sessions of real tick data + climate events.

Reports: signals, orders, fills, positions, and final state.
"""
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path.home() / "code/kalshi-trader"))
sys.path.insert(0, str(Path.home() / "code/altmarkets/kalshi-weather/src"))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)


def main():
    from backtest_runner import run_backtest

    print("=" * 80)
    print("EXPERIMENT 5: Phased Strategy Backtest")
    print("=" * 80)

    climate_events_path = Path.home() / "code/altmarkets/kalshi-weather/data/climate_events.parquet"
    catalog_path = Path.home() / "code/kalshi-trader/kalshi_data_catalog"

    # BacktestNode streams from catalog — no memory issues
    print(f"\nClimate events: {climate_events_path}")
    print(f"Catalog: {catalog_path}")
    print("Mode: BacktestNode streaming (all converted sessions)")
    print()

    engine = run_backtest(
        climate_events_path=climate_events_path,
        catalog_path=catalog_path,
        starting_balance_usd=10_000,
    )

    # --- Extract results ---
    print("\n" + "=" * 80)
    print("RESULTS")
    print("=" * 80)

    # Get strategy and actor references
    strategies = engine.trader.strategies()
    actors = engine.trader.actors()

    strategy = strategies[0] if strategies else None
    actor = actors[0] if actors else None

    if actor:
        print(f"\nFeatureActor:")
        print(f"  Events received: {actor.events_received}")
        print(f"  Cities with features: {list(actor.city_features.keys())}")
        print(f"  Models loaded: {actor._models_loaded}")
        print(f"  Ensemble: {actor.ensemble_names} weights={actor.ensemble_weights}")

    if strategy:
        print(f"\nWeatherStrategy:")
        print(f"  Signals received: {strategy.signals_received}")
        print(f"  Alerts received: {strategy.alerts_received}")
        print(f"  Spread orders placed: {strategy._spread_orders_placed}")
        print(f"  Stable orders placed: {strategy._stable_orders_placed}")
        print(f"  Open positions: {len(strategy._positions_info)}")
        for ticker, info in strategy._positions_info.items():
            print(f"    {ticker}: {info['contracts']} contracts ({info['side']})")

    # Order summary
    from nautilus_trader.model.enums import OrderStatus
    orders = engine.cache.orders()
    filled_orders = [o for o in orders if o.status == OrderStatus.FILLED]
    open_orders = [o for o in orders if o.is_open]
    cancelled_orders = [o for o in orders if o.is_canceled]

    print(f"\nOrders:")
    print(f"  Total: {len(orders)}")
    print(f"  Filled: {len(filled_orders)}")
    print(f"  Open (unfilled): {len(open_orders)}")
    print(f"  Cancelled: {len(cancelled_orders)}")

    if filled_orders:
        print(f"\nFilled orders detail:")
        for o in filled_orders:
            price_c = int(o.avg_px.as_double() * 100) if o.avg_px else 0
            qty = int(o.filled_qty.as_double())
            print(f"  {o.instrument_id.symbol.value} {o.side.name} {qty}x @ {price_c}c")

    # Positions from cache
    positions = engine.cache.positions()
    print(f"\nPositions (from cache): {len(positions)}")
    for pos in positions:
        print(f"  {pos.instrument_id.symbol.value}: qty={pos.quantity} side={pos.side.name}")

    # Account
    accounts = engine.cache.accounts()
    if accounts:
        acc = accounts[0]
        print(f"\nAccount:")
        for balance in acc.balances():
            print(f"  {balance}")


if __name__ == "__main__":
    main()
