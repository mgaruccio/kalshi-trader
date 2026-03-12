#!/usr/bin/env python
"""Thin tick data for faster parameter sweeps.

Reads each instrument's parquet files from the catalog, keeps every Nth tick
(default N=60, ~1 tick/min), and writes a new "thin" catalog. The output
uses the same directory structure so BacktestNode works unchanged.

Usage:
    python scripts/thin_catalog.py                          # default N=60
    python scripts/thin_catalog.py --every 30               # every 30th tick
    python scripts/thin_catalog.py --source other_catalog/   # custom source
"""
import argparse
import shutil
from pathlib import Path

import pyarrow.parquet as pq


def thin_catalog(
    source: Path,
    dest: Path,
    every_n: int = 60,
) -> None:
    """Create a thinned copy of a NT ParquetDataCatalog.

    Samples every Nth row from quote_tick parquet files.
    Copies currency_pair (instruments) unchanged.
    """
    qt_source = source / "data" / "quote_tick"
    qt_dest = dest / "data" / "quote_tick"

    if not qt_source.exists():
        raise FileNotFoundError(f"No quote_tick dir at {qt_source}")

    # Copy currency_pair (instruments) unchanged
    cp_source = source / "data" / "currency_pair"
    cp_dest = dest / "data" / "currency_pair"
    if cp_source.exists():
        cp_dest.parent.mkdir(parents=True, exist_ok=True)
        if cp_dest.exists():
            shutil.rmtree(cp_dest)
        shutil.copytree(cp_source, cp_dest)

    total_original = 0
    total_thinned = 0

    all_instruments = sorted(d for d in qt_source.iterdir() if d.is_dir())
    # Filter: KXHIGH T-style only (exclude KXLOWT, bracket B-style from early sessions)
    instruments = [d for d in all_instruments if "KXHIGH" in d.name and "-B" not in d.name]
    skipped = len(all_instruments) - len(instruments)
    if skipped:
        print(f"Skipped {skipped} non-KXHIGH-T instruments")
    for inst_dir in instruments:
        inst_name = inst_dir.name
        out_dir = qt_dest / inst_name
        out_dir.mkdir(parents=True, exist_ok=True)

        for pq_file in sorted(inst_dir.glob("*.parquet")):
            table = pq.read_table(str(pq_file))
            n_rows = len(table)
            total_original += n_rows

            # Sample every Nth row
            indices = list(range(0, n_rows, every_n))
            # Always include the last row to preserve time range
            if indices[-1] != n_rows - 1:
                indices.append(n_rows - 1)

            thinned = table.take(indices)
            total_thinned += len(thinned)

            out_path = out_dir / pq_file.name
            pq.write_table(thinned, str(out_path))

    print(f"Thinned {len(instruments)} instruments: {total_original:,} -> {total_thinned:,} rows (1/{every_n})")
    print(f"Output: {dest}")


def main():
    parser = argparse.ArgumentParser(description="Thin tick data for sweep backtests")
    parser.add_argument(
        "--source",
        type=Path,
        default=Path("kalshi_data_catalog"),
        help="Source catalog path (default: kalshi_data_catalog)",
    )
    parser.add_argument(
        "--dest",
        type=Path,
        default=Path("kalshi_data_catalog_thin"),
        help="Destination catalog path (default: kalshi_data_catalog_thin)",
    )
    parser.add_argument(
        "--every",
        type=int,
        default=60,
        help="Keep every Nth tick (default: 60, ~1 tick/min)",
    )
    args = parser.parse_args()

    thin_catalog(args.source, args.dest, args.every)


if __name__ == "__main__":
    main()
