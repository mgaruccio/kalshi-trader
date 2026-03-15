"""CLI script for running a WeatherMakerStrategy backtest.

Usage:
    uv run python scripts/run_backtest.py --help

    # From pre-computed signals file (preferred):
    uv run python scripts/run_backtest.py \\
        --signal-file data/model_signals.parquet \\
        --catalog-path kalshi_data_catalog \\
        --starting-balance 10000

    # From signal server backfill endpoint (slow):
    uv run python scripts/run_backtest.py \\
        --signal-url http://localhost:8000 \\
        --catalog-path kalshi_data_catalog \\
        --start-date 2026-01-01 \\
        --starting-balance 10000
"""
from __future__ import annotations

import argparse
import asyncio
import dataclasses
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

# Ensure project root is on sys.path when run as a script
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run a WeatherMakerStrategy backtest against historical data.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )

    # Data sources — signal-file (preferred) or signal-url (slow HTTP fallback)
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

    # Date range
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

    # Account
    parser.add_argument(
        "--starting-balance",
        type=int,
        default=10_000,
        help="Starting account balance in USD",
    )

    # Strategy config overrides
    parser.add_argument(
        "--confidence-threshold",
        type=float,
        default=None,
        help="Override WeatherMakerConfig.confidence_threshold",
    )
    parser.add_argument(
        "--ladder-depth",
        type=int,
        default=None,
        help="Override WeatherMakerConfig.ladder_depth",
    )
    parser.add_argument(
        "--level-quantity",
        type=int,
        default=None,
        help="Override WeatherMakerConfig.level_quantity",
    )
    parser.add_argument(
        "--exit-price-cents",
        type=int,
        default=None,
        help="Override WeatherMakerConfig.exit_price_cents",
    )
    parser.add_argument(
        "--reprice-threshold",
        type=int,
        default=None,
        help="Override WeatherMakerConfig.reprice_threshold",
    )

    # Output
    parser.add_argument(
        "--output",
        default=None,
        help="Optional path to write results as JSON",
    )

    return parser.parse_args()


def _parse_date(date_str: str | None) -> datetime | None:
    """Parse ISO date string (YYYY-MM-DD) to UTC midnight datetime."""
    if date_str is None:
        return None
    return datetime.fromisoformat(date_str).replace(tzinfo=timezone.utc)


def _build_strategy_config(args: argparse.Namespace):
    """Build WeatherMakerConfig from defaults, overriding with any supplied args."""
    from kalshi.strategy import WeatherMakerConfig

    overrides: dict = {}
    if args.confidence_threshold is not None:
        overrides["confidence_threshold"] = args.confidence_threshold
    if args.ladder_depth is not None:
        overrides["ladder_depth"] = args.ladder_depth
    if args.level_quantity is not None:
        overrides["level_quantity"] = args.level_quantity
    if args.exit_price_cents is not None:
        overrides["exit_price_cents"] = args.exit_price_cents
    if args.reprice_threshold is not None:
        overrides["reprice_threshold"] = args.reprice_threshold

    return WeatherMakerConfig(**overrides)


async def _fetch_scores(signal_url: str, start: datetime | None):
    """Fetch historical signal scores from the signal server."""
    from kalshi.backtest_loader import fetch_backfill

    start_date_str = start.strftime("%Y-%m-%d") if start else "2026-01-01"
    print(f"Fetching signal backfill from {signal_url} (start={start_date_str})...")
    scores = await fetch_backfill(http_url=signal_url, start_date=start_date_str)
    print(f"  Fetched {len(scores)} signal scores.")
    return scores


def _filter_scores_to_date_range(scores, start: datetime | None, end: datetime | None):
    """Filter scores to the requested date range."""
    if not (start or end):
        return scores

    start_ns = int(start.timestamp() * 1_000_000_000) if start else None
    end_ns = int(end.timestamp() * 1_000_000_000) if end else None

    filtered = [
        s for s in scores
        if (start_ns is None or s.ts_event >= start_ns)
        and (end_ns is None or s.ts_event <= end_ns)
    ]
    print(f"  After date filter: {len(filtered)} scores.")
    return filtered


def _write_json_output(results, output_path: str) -> None:
    """Serialize BacktestResults to JSON file."""
    data = dataclasses.asdict(results)
    path = Path(output_path)
    path.write_text(json.dumps(data, indent=2))
    print(f"Results written to {path}")


def main() -> None:
    args = _parse_args()

    start = _parse_date(args.start_date)
    end = _parse_date(args.end_date)

    # --- Load signals ---
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
        # Default: try signal file, fall back to localhost
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

    # --- Build strategy config ---
    strategy_config = _build_strategy_config(args)
    print(f"Strategy config: confidence_threshold={strategy_config.confidence_threshold}, "
          f"ladder_depth={strategy_config.ladder_depth}, "
          f"level_quantity={strategy_config.level_quantity}")

    # --- Run backtest ---
    from kalshi.backtest import run_full_backtest

    catalog_path = Path(args.catalog_path)
    print(f"Running backtest: catalog={catalog_path}, "
          f"balance=${args.starting_balance:,}, "
          f"date_range={args.start_date}..{args.end_date}")

    engine, strategy = run_full_backtest(
        catalog_path=catalog_path,
        scores=scores,
        strategy_config=strategy_config,
        starting_balance_usd=args.starting_balance,
        start=start,
        end=end,
    )

    # --- Extract and print results ---
    from kalshi.backtest_results import extract_results, format_report

    results = extract_results(engine, strategy, starting_balance_usd=args.starting_balance)
    print(format_report(results))

    # --- Optional JSON output ---
    if args.output:
        _write_json_output(results, args.output)


if __name__ == "__main__":
    main()
