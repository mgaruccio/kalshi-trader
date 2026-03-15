"""Parameter sweep script for WeatherMakerStrategy.

Runs one-at-a-time sweeps — each parameter varied while all others remain at
default — then outputs a CSV ranking parameter impact on adjusted PnL.

Usage:
    uv run python scripts/sweep_params.py --help
    uv run python scripts/sweep_params.py \\
        --signal-url http://localhost:8000 \\
        --catalog-path kalshi_data_catalog \\
        --start-date 2026-01-01 \\
        --end-date 2026-03-14 \\
        --output-csv sweep_results.csv
"""
from __future__ import annotations

import argparse
import asyncio
import csv
import sys
from datetime import datetime, timezone
from pathlib import Path

# Ensure project root is on sys.path when run as a script
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# Parameter sweep grid — (param_name, field_name, values)
# 4 + 3 + 3 + 4 + 3 = 17 runs total
_SWEEP_GRID: list[tuple[str, str, list]] = [
    ("confidence_threshold", "confidence_threshold", [0.90, 0.93, 0.95, 0.97]),
    ("ladder_depth",         "ladder_depth",         [2, 3, 4]),
    ("level_quantity",       "level_quantity",        [5, 10, 15]),
    ("exit_price_cents",     "exit_price_cents",      [95, 96, 97, 98]),
    ("reprice_threshold",    "reprice_threshold",     [1, 2, 3]),
]

_CSV_COLUMNS = [
    "param_name",
    "param_value",
    "pnl",
    "fills",
    "fill_rate",
    "max_drawdown",
    "orders",
    "adjusted_pnl",
]


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="One-at-a-time parameter sweep for WeatherMakerStrategy.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    signal_group = parser.add_mutually_exclusive_group()
    signal_group.add_argument(
        "--signal-file",
        default=None,
        help="Path to pre-computed signals parquet (from scripts/precompute_signals.py)",
    )
    signal_group.add_argument(
        "--signal-url",
        default=None,
        help="Base URL of the signal server (for backfill fetch — slow)",
    )
    parser.add_argument(
        "--catalog-path",
        default="kalshi_data_catalog",
        help="Path to the ParquetDataCatalog directory",
    )
    parser.add_argument(
        "--start-date",
        default=None,
        help="Earliest date to include (ISO format: YYYY-MM-DD)",
    )
    parser.add_argument(
        "--end-date",
        default=None,
        help="Latest date to include (ISO format: YYYY-MM-DD)",
    )
    parser.add_argument(
        "--starting-balance",
        type=int,
        default=10_000,
        help="Starting account balance in USD",
    )
    parser.add_argument(
        "--output-csv",
        default="sweep_results.csv",
        help="Path to write sweep results CSV",
    )
    return parser.parse_args()


def _parse_date(date_str: str | None) -> datetime | None:
    if date_str is None:
        return None
    return datetime.fromisoformat(date_str).replace(tzinfo=timezone.utc)


def _filter_scores_to_date_range(scores, start: datetime | None, end: datetime | None):
    if not (start or end):
        return scores
    start_ns = int(start.timestamp() * 1_000_000_000) if start else None
    end_ns = int(end.timestamp() * 1_000_000_000) if end else None
    return [
        s for s in scores
        if (start_ns is None or s.ts_event >= start_ns)
        and (end_ns is None or s.ts_event <= end_ns)
    ]


async def _fetch_scores(signal_url: str, start: datetime | None) -> list:
    from kalshi.backtest_loader import fetch_backfill
    start_date_str = start.strftime("%Y-%m-%d") if start else "2026-01-01"
    print(f"Fetching signal backfill from {signal_url} (start={start_date_str})...")
    scores = await fetch_backfill(http_url=signal_url, start_date=start_date_str)
    print(f"  Fetched {len(scores)} signal scores.")
    return scores


def _run_single(
    param_name: str,
    param_value,
    field_name: str,
    catalog_path: Path,
    scores: list,
    starting_balance: int,
    start: datetime | None,
    end: datetime | None,
) -> dict:
    """Run one backtest with a single parameter override. Returns a CSV row dict."""
    from kalshi.backtest import run_full_backtest
    from kalshi.backtest_results import extract_results
    from kalshi.strategy import WeatherMakerConfig

    config = WeatherMakerConfig(**{field_name: param_value})
    engine, strategy = run_full_backtest(
        catalog_path=catalog_path,
        scores=scores,
        strategy_config=config,
        starting_balance_usd=starting_balance,
        start=start,
        end=end,
    )
    try:
        results = extract_results(engine, strategy, starting_balance_usd=starting_balance)
    finally:
        engine.dispose()

    return {
        "param_name":   param_name,
        "param_value":  param_value,
        "pnl":          results.total_pnl_cents,
        "fills":        results.fill_count,
        "fill_rate":    round(results.fill_rate, 4),
        "max_drawdown": results.max_drawdown_cents,
        "orders":       results.order_count,
        "adjusted_pnl": round(results.adjusted_pnl_cents, 2),
    }


def _write_csv(rows: list[dict], output_csv: str) -> None:
    path = Path(output_csv)
    with path.open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=_CSV_COLUMNS)
        writer.writeheader()
        writer.writerows(rows)
    print(f"\nResults written to {path}")


def _print_summary(rows: list[dict]) -> None:
    """Print a ranking of parameter values sorted by adjusted_pnl descending."""
    print("\n" + "=" * 70)
    print("SWEEP SUMMARY — ranked by adjusted_pnl (desc)")
    print("=" * 70)
    print(f"{'param_name':<25} {'value':<10} {'adj_pnl':>10} {'pnl':>8} {'fills':>6} {'fill%':>6} {'drawdown':>10}")
    print("-" * 70)

    sorted_rows = sorted(rows, key=lambda r: r["adjusted_pnl"], reverse=True)
    for r in sorted_rows:
        print(
            f"{r['param_name']:<25} {str(r['param_value']):<10} "
            f"{r['adjusted_pnl']:>10.2f} {r['pnl']:>8} {r['fills']:>6} "
            f"{r['fill_rate']:>6.1%} {r['max_drawdown']:>10}"
        )
    print("=" * 70)

    # Per-parameter best values
    print("\nBest value per parameter (by adjusted_pnl):")
    seen_params: dict[str, dict] = {}
    for r in sorted_rows:
        if r["param_name"] not in seen_params:
            seen_params[r["param_name"]] = r
    for param_name, best_row in seen_params.items():
        print(f"  {param_name:<25}: {best_row['param_value']}  (adj_pnl={best_row['adjusted_pnl']:.2f})")


def main() -> None:
    args = _parse_args()
    start = _parse_date(args.start_date)
    end = _parse_date(args.end_date)
    catalog_path = Path(args.catalog_path)

    # --- Load signals once ---
    if args.signal_file:
        from kalshi.backtest_loader import load_signal_file
        print(f"Loading pre-computed signals from {args.signal_file}...")
        try:
            scores = load_signal_file(args.signal_file)
        except Exception as exc:
            print(f"ERROR: Failed to load signal file: {exc}", file=sys.stderr)
            sys.exit(1)
        print(f"  Loaded {len(scores)} signal scores.")
    elif args.signal_url:
        try:
            scores = asyncio.run(_fetch_scores(args.signal_url, start))
        except Exception as exc:
            print(f"ERROR: Failed to fetch signal data: {exc}", file=sys.stderr)
            sys.exit(1)
    else:
        signal_file = Path("data/model_signals.parquet")
        if signal_file.exists():
            from kalshi.backtest_loader import load_signal_file
            print(f"Loading pre-computed signals from {signal_file}...")
            try:
                scores = load_signal_file(signal_file)
            except Exception as exc:
                print(f"ERROR: Failed to load signal file: {exc}", file=sys.stderr)
                sys.exit(1)
            print(f"  Loaded {len(scores)} signal scores.")
        else:
            try:
                scores = asyncio.run(_fetch_scores("http://localhost:8000", start))
            except Exception as exc:
                print(f"ERROR: Failed to fetch signal data: {exc}", file=sys.stderr)
                sys.exit(1)

    scores = _filter_scores_to_date_range(scores, start, end)
    print(f"  Using {len(scores)} scores after date filter.\n")

    # --- Count total runs ---
    total_runs = sum(len(values) for _, _, values in _SWEEP_GRID)
    print(f"Running {total_runs} parameter sweep runs...")

    rows: list[dict] = []
    run_num = 0
    for param_name, field_name, values in _SWEEP_GRID:
        for value in values:
            run_num += 1
            print(f"[{run_num}/{total_runs}] {param_name}={value} ...", end=" ", flush=True)
            try:
                row = _run_single(
                    param_name=param_name,
                    param_value=value,
                    field_name=field_name,
                    catalog_path=catalog_path,
                    scores=scores,
                    starting_balance=args.starting_balance,
                    start=start,
                    end=end,
                )
                print(f"adj_pnl={row['adjusted_pnl']:.2f}  fills={row['fills']}")
                rows.append(row)
            except Exception as exc:
                print(f"FAILED: {exc}", file=sys.stderr)
                rows.append({
                    "param_name":   param_name,
                    "param_value":  value,
                    "pnl":          "ERROR",
                    "fills":        "ERROR",
                    "fill_rate":    "ERROR",
                    "max_drawdown": "ERROR",
                    "orders":       "ERROR",
                    "adjusted_pnl": "ERROR",
                })

    _write_csv(rows, args.output_csv)
    _print_summary([r for r in rows if r["adjusted_pnl"] != "ERROR"])


if __name__ == "__main__":
    main()
