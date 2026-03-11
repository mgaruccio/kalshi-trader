#!/usr/bin/env python
"""Experiment 5: End-to-End Backtest Sweep.

Phase A — Validation Run: single run with production defaults, full catalog.
Reports signals, orders, fills, balance and diagnoses failures at each stage.

Phase B — Parameter Sweep: 12-combo grid over min_p_win × max_cost_cents,
using a thin catalog for speed (falls back to full catalog if thin doesn't exist).

Usage:
    cd ~/code/kalshi-trader
    uv run python experiments/exp05_backtest_sweep.py
"""
import logging
import sys
from pathlib import Path

# kalshi-trader root must be on sys.path for local imports to resolve
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backtest_runner import run_backtest

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
CATALOG_FULL = Path.home() / "code/kalshi-trader/kalshi_data_catalog"
CATALOG_THIN = Path.home() / "code/kalshi-trader/kalshi_data_catalog_thin"
CLIMATE_EVENTS = Path.home() / "code/altmarkets/kalshi-weather/data/climate_events.parquet"

# Production defaults
PROD_MIN_P_WIN = 0.95
PROD_MAX_COST = 92
PROD_SELL_TARGET = 97

# Sweep grid
SWEEP_MIN_P_WIN = [0.90, 0.93, 0.95, 0.97]
SWEEP_MAX_COST = [88, 92, 96]

logging.basicConfig(level=logging.WARNING, format="%(levelname)s: %(message)s")
log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Result extraction helpers
# ---------------------------------------------------------------------------

def _extract_results(engine) -> dict:
    """Pull summary stats from a completed BacktestEngine."""
    results = {
        "signals": 0,
        "alerts": 0,
        "positions_open": 0,
        "danger_exited": 0,
        "events_received": 0,
        "cities_with_features": 0,
        "models_loaded": False,
        "orders": 0,
        "fills": 0,
        "balance": None,
    }

    strategies = list(engine.trader.strategies())
    actors = list(engine.trader.actors())

    if strategies:
        strat = strategies[0]
        results["signals"] = getattr(strat, "signals_received", 0)
        results["alerts"] = getattr(strat, "alerts_received", 0)
        results["positions_open"] = len(getattr(strat, "_positions_info", {}))
        results["danger_exited"] = len(getattr(strat, "_danger_exited", set()))

    if actors:
        actor = actors[0]
        results["events_received"] = getattr(actor, "events_received", 0)
        results["cities_with_features"] = len(getattr(actor, "city_features", {}))
        results["models_loaded"] = getattr(actor, "_models_loaded", False)

    try:
        orders = engine.cache.orders()
        results["orders"] = len(orders)
        results["fills"] = sum(
            1 for o in orders if o.filled_qty.as_double() > 0
        )
    except Exception as e:
        log.warning(f"Could not inspect orders: {e}")

    try:
        accounts = engine.cache.accounts()
        if accounts:
            results["balance"] = accounts[0].balance_total().as_double()
    except Exception as e:
        log.warning(f"Could not inspect accounts: {e}")

    return results


def _diagnose(results: dict) -> str:
    """Return a human-readable diagnosis of where the pipeline stalled."""
    if not results["models_loaded"]:
        return "FAIL: Models not loaded — check model file paths in FeatureActor"
    if results["events_received"] == 0:
        return "FAIL: No ClimateEvents received — check climate_events.parquet path and content"
    if results["cities_with_features"] == 0:
        return "FAIL: No city features accumulated — ClimateEvents may be filtered out (timing issue)"
    if results["signals"] == 0:
        return "FAIL: No ModelSignals emitted — model timer may never have fired, or features are missing ecmwf_high/gfs_high"
    if results["orders"] == 0:
        return "FAIL: No orders placed — signals emitted but no quote ticks matched; check catalog contains NO-side instruments"
    if results["fills"] == 0:
        return "FAIL: Orders placed but none filled — ask price exceeded max_cost_cents, or FOK/GTC matching issue"
    return "OK"


# ---------------------------------------------------------------------------
# Phase A: Validation run
# ---------------------------------------------------------------------------

def run_phase_a():
    print("=" * 80)
    print("PHASE A: VALIDATION RUN")
    print(f"  min_p_win={PROD_MIN_P_WIN}  max_cost={PROD_MAX_COST}c  sell_target={PROD_SELL_TARGET}c")
    print(f"  catalog : {CATALOG_FULL}")
    print(f"  events  : {CLIMATE_EVENTS}")
    print("=" * 80)

    if not CATALOG_FULL.exists():
        print(f"ERROR: Catalog not found at {CATALOG_FULL}")
        print("  Run: cd ~/code/kalshi-trader && python backtest_runner.py --convert")
        return

    if not CLIMATE_EVENTS.exists():
        print(f"ERROR: climate_events.parquet not found at {CLIMATE_EVENTS}")
        print("  Run: uv run python scripts/build_calibration_dataset.py  (in kalshi-weather)")
        return

    strategy_config = {
        "min_p_win": PROD_MIN_P_WIN,
        "max_cost_cents": PROD_MAX_COST,
        "sell_target_cents": PROD_SELL_TARGET,
    }

    try:
        engine = run_backtest(
            climate_events_path=CLIMATE_EVENTS,
            catalog_path=CATALOG_FULL,
            starting_balance_usd=100,
            strategy_config=strategy_config,
        )
    except Exception as e:
        print(f"\nERROR: run_backtest() raised an exception:")
        import traceback
        traceback.print_exc()
        return

    r = _extract_results(engine)

    print("\n--- Actor ---")
    print(f"  Models loaded         : {r['models_loaded']}")
    print(f"  ClimateEvents received: {r['events_received']:,}")
    print(f"  Cities with features  : {r['cities_with_features']}")

    print("\n--- Strategy ---")
    print(f"  ModelSignals received : {r['signals']:,}")
    print(f"  DangerAlerts received : {r['alerts']:,}")
    print(f"  Positions open        : {r['positions_open']}")
    print(f"  Danger-exited tickers : {r['danger_exited']}")

    print("\n--- Orders ---")
    print(f"  Total orders          : {r['orders']}")
    print(f"  Filled orders         : {r['fills']}")

    print("\n--- Account ---")
    if r["balance"] is not None:
        print(f"  Final balance         : ${r['balance']:.2f}")
    else:
        print("  Final balance         : (unavailable)")

    diagnosis = _diagnose(r)
    print(f"\n--- Diagnosis ---")
    print(f"  {diagnosis}")

    if r["fills"] == 0:
        print("\n  Pipeline stages that passed:")
        stages = [
            ("Models loaded",           r["models_loaded"]),
            ("ClimateEvents received",  r["events_received"] > 0),
            ("City features built",     r["cities_with_features"] > 0),
            ("ModelSignals emitted",    r["signals"] > 0),
            ("Orders placed",           r["orders"] > 0),
            ("Fills recorded",          r["fills"] > 0),
        ]
        for label, ok in stages:
            mark = "PASS" if ok else "FAIL"
            print(f"    [{mark}] {label}")

    return r


# ---------------------------------------------------------------------------
# Phase B: Parameter sweep
# ---------------------------------------------------------------------------

def run_phase_b():
    print("\n" + "=" * 80)
    print("PHASE B: PARAMETER SWEEP")
    print(f"  min_p_win x max_cost grid ({len(SWEEP_MIN_P_WIN)} x {len(SWEEP_MAX_COST)} = {len(SWEEP_MIN_P_WIN) * len(SWEEP_MAX_COST)} combos)")

    # Use thin catalog if available, else fall back to full
    catalog = CATALOG_THIN if CATALOG_THIN.exists() else CATALOG_FULL
    print(f"  catalog : {catalog}")
    if catalog == CATALOG_FULL:
        print("  (thin catalog not found — using full catalog, sweep will be slow)")
    print("=" * 80)

    if not catalog.exists():
        print(f"ERROR: No catalog found at {catalog}")
        return

    header = f"{'min_pw':>8}  {'max_cost':>8}  {'events':>7}  {'signals':>8}  {'orders':>7}  {'fills':>6}  {'balance':>10}"
    sep = "-" * len(header)
    print(f"\n{header}")
    print(sep)

    for min_pw in SWEEP_MIN_P_WIN:
        for max_cost in SWEEP_MAX_COST:
            strategy_config = {
                "min_p_win": min_pw,
                "max_cost_cents": max_cost,
                "sell_target_cents": PROD_SELL_TARGET,
            }
            try:
                engine = run_backtest(
                    climate_events_path=CLIMATE_EVENTS,
                    catalog_path=catalog,
                    starting_balance_usd=100,
                    strategy_config=strategy_config,
                )
                r = _extract_results(engine)
                bal = f"${r['balance']:.2f}" if r["balance"] is not None else "N/A"
                print(
                    f"{min_pw:>8.2f}  {max_cost:>8}  "
                    f"{r['events_received']:>7,}  {r['signals']:>8,}  "
                    f"{r['orders']:>7}  {r['fills']:>6}  {bal:>10}"
                )
            except Exception as e:
                print(f"{min_pw:>8.2f}  {max_cost:>8}  ERROR: {e}")

    print(sep)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    r = run_phase_a()
    if r is None or r.get("fills", 0) == 0:
        print("\nSkipping Phase B — Phase A had zero fills. Fix pipeline first.")
        return
    run_phase_b()


if __name__ == "__main__":
    main()
