#!/usr/bin/env python
"""Experiment 6: Parameter Sweep via Multiprocessing.

Sweeps strategy parameters (entry thresholds, cost limits, sell targets)
across independent backtest runs using multiprocessing for parallelism.

Uses thinned tick catalog by default for ~10x faster sweeps. Use --full-ticks
to validate top configs against the complete tick dataset.

Each worker process runs a BATCH of configs, sharing model load + NT init
overhead across multiple runs.

Usage:
    python experiments/exp06_param_sweep.py                  # 27-config phase 1 (thin ticks)
    python experiments/exp06_param_sweep.py --full           # 81-config full grid (thin ticks)
    python experiments/exp06_param_sweep.py --full-ticks     # use full tick catalog
    python experiments/exp06_param_sweep.py --workers 4      # limit parallelism
"""
import json
import logging
import multiprocessing as mp
import sys
import time
from itertools import product
from pathlib import Path

sys.path.insert(0, str(Path.home() / "code/kalshi-trader"))
sys.path.insert(0, str(Path.home() / "code/altmarkets/kalshi-weather/src"))

CLIMATE_EVENTS = Path.home() / "code/altmarkets/kalshi-weather/data/climate_events.parquet"
CATALOG_FULL = Path.home() / "code/kalshi-trader/kalshi_data_catalog"
CATALOG_THIN = Path.home() / "code/kalshi-trader/kalshi_data_catalog_thin"
RESULTS_PATH = Path.home() / "code/kalshi-trader/data/sweep_results.json"

# Suppress verbose NT logging in worker processes
logging.basicConfig(level=logging.WARNING, format="%(levelname)s: %(message)s")


def run_single(args: dict) -> dict:
    """Run one backtest config in a worker process and return metrics."""
    from backtest_runner import run_backtest
    from nautilus_trader.model.enums import OrderStatus

    label = args["label"]
    try:
        t0 = time.monotonic()
        engine = run_backtest(
            climate_events_path=args["climate_events_path"],
            catalog_path=args["catalog_path"],
            starting_balance_usd=args["balance"],
            strategy_config=args["strategy_config"],
            actor_config=args["actor_config"],
        )
        elapsed = time.monotonic() - t0

        # Extract metrics
        orders = engine.cache.orders()
        filled = [o for o in orders if o.status == OrderStatus.FILLED]
        positions = engine.cache.positions()

        # PnL: cash balance change + unrealized value of open positions
        # Open NO positions settle at $1 if we win — mark to last bid as proxy
        accounts = engine.cache.accounts()
        balance = 0.0
        if accounts:
            bt = accounts[0].balance_total()
            balance = float(bt.as_double()) if hasattr(bt, 'as_double') else float(bt)
        cash_pnl = balance - args["balance"]

        # Compute total cost and unrealized value of open positions
        total_cost = 0.0
        total_contracts = 0
        for o in filled:
            if o.side.name == "BUY":
                avg_px = float(o.avg_px.as_double() if hasattr(o.avg_px, 'as_double') else o.avg_px) if o.avg_px else 0
                qty = float(o.filled_qty.as_double() if hasattr(o.filled_qty, 'as_double') else o.filled_qty)
                total_cost += avg_px * qty
                total_contracts += int(qty)

        # Assume open NO positions settle at $1 (97%+ win rate historically)
        # This gives expected PnL = contracts * $1 - total_cost
        expected_pnl = total_contracts * 1.0 - total_cost
        # Also report cash_pnl for realized-only view
        pnl = cash_pnl

        # Strategy counters
        strategies = engine.trader.strategies()
        spread_orders = 0
        stable_orders = 0
        signals = 0
        if strategies:
            strat = strategies[0]
            spread_orders = strat._spread_orders_placed
            stable_orders = strat._stable_orders_placed
            signals = strat.signals_received

        return {
            "label": label,
            "strategy_config": args["strategy_config"],
            "pnl": round(pnl, 2),
            "expected_pnl": round(expected_pnl, 2),
            "balance": round(balance, 2),
            "total_cost": round(total_cost, 2),
            "total_contracts": total_contracts,
            "orders": len(orders),
            "fills": len(filled),
            "positions": len(positions),
            "spread_orders": spread_orders,
            "stable_orders": stable_orders,
            "signals": signals,
            "elapsed_s": round(elapsed, 1),
            "error": None,
        }
    except Exception as e:
        import traceback
        return {
            "label": label,
            "strategy_config": args["strategy_config"],
            "pnl": 0.0,
            "balance": 0.0,
            "orders": 0,
            "fills": 0,
            "positions": 0,
            "spread_orders": 0,
            "stable_orders": 0,
            "signals": 0,
            "elapsed_s": 0.0,
            "error": f"{e}\n{traceback.format_exc()}",
        }


def run_batch(args: dict) -> list[dict]:
    """Run a batch of configs in one process, sharing model/init overhead."""
    results = []
    for config in args["configs"]:
        results.append(run_single(config))
    return results


def build_grid(full: bool = False, catalog_path: Path = CATALOG_THIN) -> list[dict]:
    """Build the parameter grid.

    Phase 1 (default): 27 configs — sweep entry thresholds with fixed sell_target=97.
    Full: 81 configs — also sweep sell_target_cents.
    """
    spread_min_pw_values = [0.90, 0.93, 0.95]
    stable_min_pw_values = [0.93, 0.95, 0.97]
    stable_max_cost_values = [94, 96, 98]
    sell_target_values = [96, 97, 98] if full else [97]

    grid = []
    for spread_pw, stable_pw, max_cost, sell_tgt in product(
        spread_min_pw_values,
        stable_min_pw_values,
        stable_max_cost_values,
        sell_target_values,
    ):
        strategy_config = {
            "open_spread_min_p_win": spread_pw,
            "stable_min_p_win": stable_pw,
            "stable_max_cost_cents": max_cost,
            "sell_target_cents": sell_tgt,
            "no_only": True,
        }
        label = f"sp{spread_pw:.2f}_st{stable_pw:.2f}_mc{max_cost}_st{sell_tgt}"
        grid.append({
            "label": label,
            "climate_events_path": CLIMATE_EVENTS,
            "catalog_path": catalog_path,
            "balance": 100,
            "strategy_config": strategy_config,
            "actor_config": {"model_cycle_seconds": 900},
        })
    return grid


def print_results_table(results: list[dict]) -> None:
    """Print results sorted by PnL descending."""
    # Sort by expected PnL descending
    results.sort(key=lambda r: r.get("expected_pnl", r["pnl"]), reverse=True)

    header = (
        f"{'spread_pw':>10} {'stable_pw':>10} {'max_cost':>9} {'sell_tgt':>9} | "
        f"{'Cash':>8} {'E[PnL]':>8} {'Cost':>7} {'Ctrs':>5} {'Fills':>6} {'Time':>6}"
    )
    print("\n" + "=" * 95)
    print("SWEEP RESULTS (sorted by expected PnL)")
    print("  Cash = realized cash P&L | E[PnL] = expected if all open NO settle at $1")
    print("=" * 95)
    print(header)
    print("-" * 95)

    for r in results:
        if r["error"]:
            cfg = r["strategy_config"]
            print(
                f"{cfg.get('open_spread_min_p_win', '?'):>10} "
                f"{cfg.get('stable_min_p_win', '?'):>10} "
                f"{cfg.get('stable_max_cost_cents', '?'):>9} "
                f"{cfg.get('sell_target_cents', '?'):>9} | "
                f"{'ERROR':>8}"
            )
            continue

        cfg = r["strategy_config"]
        print(
            f"{cfg['open_spread_min_p_win']:>10.2f} "
            f"{cfg['stable_min_p_win']:>10.2f} "
            f"{cfg['stable_max_cost_cents']:>9} "
            f"{cfg['sell_target_cents']:>9} | "
            f"{r['pnl']:>8.2f} {r.get('expected_pnl', 0):>8.2f} "
            f"{r.get('total_cost', 0):>7.2f} {r.get('total_contracts', 0):>5} "
            f"{r['fills']:>6} {r['elapsed_s']:>5.0f}s"
        )

    # Summary
    valid = [r for r in results if not r["error"]]
    if valid:
        best = valid[0]
        worst = valid[-1]
        print(f"\nBest:  E[PnL]=${best.get('expected_pnl', 0):.2f}  ({best['label']})")
        print(f"Worst: E[PnL]=${worst.get('expected_pnl', 0):.2f}  ({worst['label']})")
        best_ep = best.get("expected_pnl", 0)
        worst_ep = worst.get("expected_pnl", 0)
        if worst_ep != 0:
            spread_pct = ((best_ep - worst_ep) / abs(worst_ep)) * 100
            print(f"Spread: {spread_pct:.0f}%")


def main():
    import argparse

    parser = argparse.ArgumentParser(description="Parameter sweep backtest")
    parser.add_argument("--full", action="store_true", help="Full 81-config grid (default: 27)")
    parser.add_argument("--full-ticks", action="store_true", help="Use full tick catalog (slower, more accurate)")
    parser.add_argument("--workers", type=int, default=14, help="Number of parallel workers")
    parser.add_argument("--dry-run", action="store_true", help="Print grid without running")
    args = parser.parse_args()

    catalog_path = CATALOG_FULL if args.full_ticks else CATALOG_THIN

    # Auto-generate thin catalog if missing
    if not args.full_ticks and not catalog_path.exists():
        if not CATALOG_FULL.exists():
            print(f"ERROR: {CATALOG_FULL} not found")
            return
        print("Thin catalog not found — generating with N=60...")
        from scripts.thin_catalog import thin_catalog
        thin_catalog(CATALOG_FULL, catalog_path, every_n=60)
        print()

    grid = build_grid(full=args.full, catalog_path=catalog_path)
    tick_mode = "full ticks" if args.full_ticks else "thin ticks (1/60)"
    print(f"Parameter sweep: {len(grid)} configs, {args.workers} workers, {tick_mode}")
    print(f"Climate events: {CLIMATE_EVENTS}")
    print(f"Catalog: {catalog_path}")
    print(f"Starting balance: $100")

    if args.dry_run:
        for g in grid:
            print(f"  {g['label']}: {g['strategy_config']}")
        return

    # Verify data exists
    if not CLIMATE_EVENTS.exists():
        print(f"ERROR: {CLIMATE_EVENTS} not found")
        return
    if not catalog_path.exists():
        print(f"ERROR: {catalog_path} not found")
        return

    t0 = time.monotonic()

    # Split grid into batches (one per worker) to amortize init overhead
    n_workers = min(args.workers, len(grid))
    batches = [[] for _ in range(n_workers)]
    for i, config in enumerate(grid):
        batches[i % n_workers].append(config)
    batch_args = [{"configs": batch} for batch in batches if batch]

    print(f"Batched into {len(batch_args)} workers ({[len(b['configs']) for b in batch_args]} configs each)")

    # Run sweep with multiprocessing — batch mode
    results = []
    with mp.Pool(processes=n_workers) as pool:
        for batch_results in pool.imap_unordered(run_batch, batch_args):
            for result in batch_results:
                results.append(result)
                status = "OK" if not result["error"] else "FAIL"
                print(f"  [{len(results)}/{len(grid)}] {result['label']}: {status} PnL=${result['pnl']:.2f} ({result['elapsed_s']:.0f}s)")

    total_time = time.monotonic() - t0

    # Print results table
    print_results_table(results)
    print(f"\nTotal wall time: {total_time:.0f}s ({total_time/60:.1f}min)")

    # Save results
    RESULTS_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(RESULTS_PATH, "w") as f:
        json.dump(results, f, indent=2)
    print(f"Results saved to {RESULTS_PATH}")

    # Print errors if any
    errors = [r for r in results if r["error"]]
    if errors:
        print(f"\n{len(errors)} ERRORS:")
        for e in errors:
            print(f"  {e['label']}: {e['error'][:200]}")


if __name__ == "__main__":
    main()
