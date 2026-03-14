"""Validate the backtest pipeline end-to-end with synthetic signals.

Generates SignalScore events matching the catalog instruments and runs
a full backtest without needing the signal server.
"""
from __future__ import annotations

import re
import sys
from datetime import datetime, timezone
from pathlib import Path

from nautilus_trader.persistence.catalog import ParquetDataCatalog

from kalshi.backtest import run_full_backtest
from kalshi.backtest_results import extract_results, format_report
from kalshi.signals import SignalScore

# City code -> city name mapping for catalog tickers
CITY_CODES = {
    "AUS": "austin",
    "CHI": "chicago",
    "DEN": "denver",
    "LAX": "los_angeles",
    "MIA": "miami",
    "NY": "new_york",
    "PHI": "philadelphia",
    "PHX": "phoenix",
    "SEA": "seattle",
    "SF": "san_francisco",
    "ATL": "atlanta",
    "HOU": "houston",
    "DAL": "dallas",
    "DCA": "washington_dc",
}

# Parse ticker: KXHIGH<CITY>-<YYMONDD>-T<THRESHOLD>
TICKER_RE = re.compile(r"^(KXHIGH\w+)-(\d{2}[A-Z]{3}\d{2})-T(\d+)$")


def _extract_city_code(ticker_prefix: str) -> str:
    """Extract city code from KXHIGH prefix, e.g. KXHIGHNY -> NY."""
    return ticker_prefix[6:]


def _generate_signals(catalog_path: Path) -> list[SignalScore]:
    """Generate synthetic SignalScore events for each unique ticker in the catalog.

    Strategy: for each ticker, generate a high-confidence NO signal at the
    start of the trading day (10:00 ET) and another at 12:00 ET. This should
    trigger the filter and cause ladder placements.
    """
    catalog = ParquetDataCatalog(str(catalog_path))
    instruments = catalog.instruments()

    # Discover unique base tickers (without -YES/-NO suffix)
    tickers: dict[str, dict] = {}
    for inst in instruments:
        symbol = inst.id.symbol.value  # e.g. "KXHIGHNY-26MAR15-T54-YES"
        parts = symbol.split("-")
        if len(parts) < 4:
            continue
        base_ticker = "-".join(parts[:3])  # e.g. "KXHIGHNY-26MAR15-T54"
        if base_ticker in tickers:
            continue

        m = TICKER_RE.match(base_ticker)
        if not m:
            continue

        city_code = _extract_city_code(m.group(1))
        date_str = m.group(2)  # e.g. "26MAR15"
        threshold = int(m.group(3))

        try:
            settlement = datetime.strptime(date_str, "%y%b%d")
        except ValueError:
            continue

        city_name = CITY_CODES.get(city_code, city_code.lower())
        tickers[base_ticker] = {
            "city_code": city_code,
            "city_name": city_name,
            "threshold": threshold,
            "settlement": settlement,
        }

    print(f"Discovered {len(tickers)} unique tickers across {len(set(t['city_name'] for t in tickers.values()))} cities")

    # Generate two signals per ticker at realistic times
    scores: list[SignalScore] = []
    for ticker, info in tickers.items():
        settlement = info["settlement"]
        # Signal at 10:00 ET on settlement day (14:00 UTC)
        dt_morning = settlement.replace(hour=14, minute=0, tzinfo=timezone.utc)
        ts_morning = int(dt_morning.timestamp() * 1_000_000_000)

        # Signal at 12:00 ET (16:00 UTC)
        dt_midday = settlement.replace(hour=16, minute=0, tzinfo=timezone.utc)
        ts_midday = int(dt_midday.timestamp() * 1_000_000_000)

        for ts_ns in (ts_morning, ts_midday):
            scores.append(SignalScore(
                ticker=ticker,
                city=info["city_name"],
                threshold=float(info["threshold"]),
                direction="above",
                no_p_win=0.97,   # High confidence NO
                yes_p_win=0.03,
                no_margin=5.0,
                n_models=3,
                emos_no=0.96,
                ngboost_no=0.98,
                drn_no=0.97,
                yes_bid=85,      # Market YES bid at 85c -> good entry for NO
                yes_ask=90,
                status="open",
                ts_event=ts_ns,
                ts_init=ts_ns,
            ))

    scores.sort(key=lambda s: s.ts_event)
    print(f"Generated {len(scores)} synthetic signal scores")
    return scores


def main() -> None:
    catalog_path = Path("kalshi_data_catalog")
    if not catalog_path.exists():
        print("ERROR: kalshi_data_catalog not found", file=sys.stderr)
        sys.exit(1)

    # Generate synthetic signals
    scores = _generate_signals(catalog_path)
    if not scores:
        print("ERROR: No signals generated — check catalog", file=sys.stderr)
        sys.exit(1)

    # Filter to a single city for fast validation (NY = ~20 instruments vs 190 total)
    ny_scores = [s for s in scores if s.city == "new_york"]
    print(f"Filtered to {len(ny_scores)} NY-only signals for fast validation")

    # Run backtest with instrument filter for speed
    ny_instrument_ids = [
        f"KXHIGHNY-{s.ticker.split('-')[1]}-{s.ticker.split('-')[2]}-{side}.KALSHI"
        for s in ny_scores
        for side in ("YES", "NO")
    ]

    print("\nRunning backtest...")
    engine, strategy = run_full_backtest(
        catalog_path=catalog_path,
        scores=ny_scores,
        starting_balance_usd=10_000,
        instrument_ids=ny_instrument_ids,
    )

    # Extract and print results
    results = extract_results(engine, strategy)
    print(format_report(results))

    # Pipeline validation checks
    print("\n--- Pipeline Validation ---")
    checks = {
        "Signals received > 0": results.signals_received > 0,
        "Filter passes > 0": results.filter_passes > 0,
        "Orders submitted > 0": results.orders_submitted > 0,
        "Ladders placed > 0": results.ladders_placed > 0,
    }
    all_pass = True
    for check, passed in checks.items():
        status = "PASS" if passed else "FAIL"
        if not passed:
            all_pass = False
        print(f"  [{status}] {check}")

    if results.fill_count > 0:
        print(f"  [PASS] Fills: {results.fill_count}")
    else:
        print(f"  [INFO] No fills (expected with limit orders in thin backtest data)")

    print()
    if all_pass:
        print("Pipeline validation PASSED — strategy is processing signals and placing orders.")
    else:
        print("Pipeline validation FAILED — investigate above.")
        sys.exit(1)


if __name__ == "__main__":
    main()
