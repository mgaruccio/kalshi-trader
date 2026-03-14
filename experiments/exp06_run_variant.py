#!/usr/bin/env python
"""Run a single backtest variant and print results as JSON."""
import json
import logging
import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path.home() / "code/kalshi-trader"))
sys.path.insert(0, str(Path.home() / "code/altmarkets/kalshi-weather/src"))

logging.basicConfig(level=logging.WARNING)


def main():
    variant = sys.argv[1] if len(sys.argv) > 1 else "repricing"

    from backtest_runner import run_backtest
    from nautilus_trader.model.enums import OrderStatus, OrderSide
    import weather_strategy

    climate_events_path = Path.home() / "code/altmarkets/kalshi-weather/data/climate_events.parquet"
    catalog_path = Path.home() / "code/kalshi-trader/kalshi_data_catalog"
    model_signals_path = Path.home() / "code/kalshi-trader/data/model_signals.parquet"

    if variant == "static":
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
    else:
        engine = run_backtest(
            climate_events_path=climate_events_path,
            catalog_path=catalog_path,
            starting_balance_usd=100,
            model_signals_path=model_signals_path,
        )

    strategies = engine.trader.strategies()
    strategy = strategies[0] if strategies else None

    orders = engine.cache.orders()
    filled_orders = [o for o in orders if o.status == OrderStatus.FILLED]
    buy_fills = [o for o in filled_orders if o.side == OrderSide.BUY]
    sell_fills = [o for o in filled_orders if o.side == OrderSide.SELL]
    cancelled = [o for o in orders if o.is_canceled]

    total_cost = sum(
        int(o.avg_px.as_double() * 100) * int(o.filled_qty.as_double())
        for o in buy_fills if o.avg_px
    )
    total_revenue = sum(
        int(o.avg_px.as_double() * 100) * int(o.filled_qty.as_double())
        for o in sell_fills if o.avg_px
    )

    accounts = engine.cache.accounts()
    balance = ""
    if accounts:
        for b in accounts[0].balances():
            balance = str(b)

    fills_detail = []
    for o in filled_orders:
        price_c = int(o.avg_px.as_double() * 100) if o.avg_px else 0
        qty = int(o.filled_qty.as_double())
        fills_detail.append(f"{o.instrument_id.symbol.value} {o.side.name} {qty}x@{price_c}c")

    result = {
        "variant": variant,
        "signals": strategy.signals_received if strategy else 0,
        "total_orders": len(orders),
        "filled": len(filled_orders),
        "buy_fills": len(buy_fills),
        "sell_fills": len(sell_fills),
        "cancelled": len(cancelled),
        "spread_orders": strategy._spread_orders_placed if strategy else 0,
        "stable_orders": strategy._stable_orders_placed if strategy else 0,
        "total_cost_cents": total_cost,
        "total_revenue_cents": total_revenue,
        "open_positions": len(strategy._positions_info) if strategy else 0,
        "balance": balance,
        "fills": fills_detail,
    }
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
