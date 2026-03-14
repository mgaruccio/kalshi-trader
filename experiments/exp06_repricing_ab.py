#!/usr/bin/env python
"""Experiment 6: A/B test — static ladder vs bid-tracking repricing.

Runs the same backtest twice:
  A) Baseline: static ladder (deploy once, never reprice)
  B) Treatment: reprice ladder when bid moves

Reports order counts, fills, and final P&L for each.
"""
import logging
import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path.home() / "code/kalshi-trader"))
sys.path.insert(0, str(Path.home() / "code/altmarkets/kalshi-weather/src"))

logging.basicConfig(
    level=logging.WARNING,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger(__name__)
log.setLevel(logging.INFO)


def extract_results(engine, label: str) -> dict:
    """Extract key metrics from a completed backtest engine."""
    from nautilus_trader.model.enums import OrderStatus, OrderSide

    strategies = engine.trader.strategies()
    strategy = strategies[0] if strategies else None

    orders = engine.cache.orders()
    filled_orders = [o for o in orders if o.status == OrderStatus.FILLED]
    open_orders = [o for o in orders if o.is_open]
    cancelled_orders = [o for o in orders if o.is_canceled]

    buy_fills = [o for o in filled_orders if o.side == OrderSide.BUY]
    sell_fills = [o for o in filled_orders if o.side == OrderSide.SELL]

    total_cost = sum(
        int(o.avg_px.as_double() * 100) * int(o.filled_qty.as_double())
        for o in buy_fills if o.avg_px
    )
    total_revenue = sum(
        int(o.avg_px.as_double() * 100) * int(o.filled_qty.as_double())
        for o in sell_fills if o.avg_px
    )

    accounts = engine.cache.accounts()
    balance = None
    if accounts:
        for b in accounts[0].balances():
            balance = str(b)

    positions = engine.cache.positions()

    results = {
        "label": label,
        "signals": strategy.signals_received if strategy else 0,
        "total_orders": len(orders),
        "filled": len(filled_orders),
        "buy_fills": len(buy_fills),
        "sell_fills": len(sell_fills),
        "open": len(open_orders),
        "cancelled": len(cancelled_orders),
        "total_cost_cents": total_cost,
        "total_revenue_cents": total_revenue,
        "spread_orders": strategy._spread_orders_placed if strategy else 0,
        "stable_orders": strategy._stable_orders_placed if strategy else 0,
        "open_positions": len(strategy._positions_info) if strategy else 0,
        "balance": balance,
        "cache_positions": len(positions),
    }

    log.info(f"\n{'='*60}")
    log.info(f"  {label}")
    log.info(f"{'='*60}")
    log.info(f"  Signals received:    {results['signals']}")
    log.info(f"  Total orders:        {results['total_orders']}")
    log.info(f"  Filled:              {results['filled']} (buy={results['buy_fills']}, sell={results['sell_fills']})")
    log.info(f"  Open (unfilled):     {results['open']}")
    log.info(f"  Cancelled:           {results['cancelled']}")
    log.info(f"  Spread orders:       {results['spread_orders']}")
    log.info(f"  Stable orders:       {results['stable_orders']}")
    log.info(f"  Total cost:          {results['total_cost_cents']}c")
    log.info(f"  Total revenue:       {results['total_revenue_cents']}c")
    log.info(f"  Open positions:      {results['open_positions']}")
    log.info(f"  Account balance:     {results['balance']}")

    if filled_orders:
        log.info(f"\n  Filled orders:")
        for o in filled_orders:
            price_c = int(o.avg_px.as_double() * 100) if o.avg_px else 0
            qty = int(o.filled_qty.as_double())
            log.info(f"    {o.instrument_id.symbol.value} {o.side.name} {qty}x @ {price_c}c")

    return results


def run_variant(label: str, static_ladder: bool) -> dict:
    """Run backtest with either static or repricing ladder."""
    from backtest_runner import run_backtest
    import weather_strategy

    climate_events_path = Path.home() / "code/altmarkets/kalshi-weather/data/climate_events.parquet"
    catalog_path = Path.home() / "code/kalshi-trader/kalshi_data_catalog"
    model_signals_path = Path.home() / "code/kalshi-trader/data/model_signals.parquet"

    if static_ladder:
        # Patch _deploy_ladder to use old static behavior
        original_deploy = weather_strategy.WeatherStrategy._deploy_ladder

        def static_deploy(self, ticker, signal, budget_remaining=None):
            if ticker in self._ladder_orders:
                return 0
            return original_deploy(self, ticker, signal, budget_remaining=budget_remaining)

        with patch.object(weather_strategy.WeatherStrategy, '_deploy_ladder', static_deploy):
            engine = run_backtest(
                climate_events_path=climate_events_path,
                catalog_path=catalog_path,
                starting_balance_usd=100,
                model_signals_path=model_signals_path,
            )
            return extract_results(engine, label)
    else:
        engine = run_backtest(
            climate_events_path=climate_events_path,
            catalog_path=catalog_path,
            starting_balance_usd=100,
            model_signals_path=model_signals_path,
        )
        return extract_results(engine, label)


def main():
    log.info("EXPERIMENT 6: Static Ladder vs Bid-Tracking Repricing")
    log.info("=" * 60)

    log.info("\nRunning A: Static ladder (baseline)...")
    a = run_variant("A) Static Ladder (baseline)", static_ladder=True)

    log.info("\nRunning B: Bid-tracking repricing...")
    b = run_variant("B) Bid-Tracking Repricing", static_ladder=False)

    log.info(f"\n{'='*60}")
    log.info(f"  COMPARISON")
    log.info(f"{'='*60}")
    log.info(f"{'Metric':<25} {'Static':>12} {'Repricing':>12} {'Delta':>12}")
    log.info(f"{'-'*25} {'-'*12} {'-'*12} {'-'*12}")

    for key in ['total_orders', 'filled', 'buy_fills', 'sell_fills',
                'cancelled', 'stable_orders', 'total_cost_cents',
                'total_revenue_cents', 'open_positions']:
        va = a.get(key, 0)
        vb = b.get(key, 0)
        delta = vb - va
        sign = "+" if delta > 0 else ""
        log.info(f"{key:<25} {va:>12} {vb:>12} {sign}{delta:>11}")

    log.info(f"\n{'Balance':<25} {a['balance']:>12} {b['balance']:>12}")


if __name__ == "__main__":
    main()
