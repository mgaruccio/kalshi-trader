"""Display current trader portfolio from the snapshot file.

Usage:
    uv run python scripts/portfolio_status.py
    ssh root@161.35.114.105 "python3 /root/kalshi-trader/scripts/portfolio_status.py"
"""
from __future__ import annotations

import json
import sys

SNAPSHOT_PATH = "/tmp/kalshi-trader-portfolio.json"


def main() -> None:
    try:
        with open(SNAPSHOT_PATH) as f:
            data = json.load(f)
    except FileNotFoundError:
        print(f"No snapshot file at {SNAPSHOT_PATH}")
        print("Is the trader running? Snapshots are written every 60s.")
        sys.exit(1)

    ts = data["timestamp"]
    acct = data["account"]
    summary = data["summary"]
    positions = data["positions"]

    print(f"Portfolio Snapshot — {ts}")
    print("=" * 80)

    # Account
    print(f"\nAccount Balance: ${acct['total_usd']:.2f} total"
          f"  (${acct['free_usd']:.2f} free, ${acct['locked_usd']:.2f} locked)")

    # Summary
    print(f"\nOpen Positions: {summary['open_position_count']}"
          f"  ({summary['total_contracts']} contracts)")
    print(f"  Cost Basis:     ${summary['total_cost_cents'] / 100:.2f}")
    print(f"  Mark-to-Market: ${summary['total_mtm_cents'] / 100:.2f}")
    print(f"  Unrealized P&L: ${summary['total_unrealized_pnl_cents'] / 100:+.2f}")
    print(f"  Portfolio Value: ${summary['portfolio_value_cents'] / 100:.2f}")

    if not positions:
        print("\nNo open positions.")
        return

    # Position table
    print(f"\n{'Instrument':<45} {'Side':<6} {'Qty':>4} {'Avg Entry':>10}"
          f" {'Bid':>5} {'Ask':>5} {'MTM':>8} {'Unrl P&L':>9}")
    print("-" * 100)

    for p in positions:
        inst = p["instrument"].replace(".KALSHI", "")
        bid = f"{p['bid_cents']}" if p["bid_cents"] is not None else "—"
        ask = f"{p['ask_cents']}" if p["ask_cents"] is not None else "—"
        mtm = f"${p['mtm_value_cents'] / 100:.2f}" if p["mtm_value_cents"] is not None else "—"
        pnl = (
            f"${p['unrealized_pnl_cents'] / 100:+.2f}"
            if p["unrealized_pnl_cents"] is not None
            else "—"
        )
        print(
            f"{inst:<45} {p['side']:<6} {p['qty']:>4} "
            f"{p['avg_entry_cents']:>8}¢  {bid:>5} {ask:>5} {mtm:>8} {pnl:>9}"
        )

    print("-" * 100)


if __name__ == "__main__":
    main()
