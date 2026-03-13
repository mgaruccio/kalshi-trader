#!/usr/bin/env python3
"""CLI for querying the trading SQLite database."""
import argparse
import json
from pathlib import Path

from db import (
    get_connection,
    get_latest_evaluations,
    get_positions,
    get_desired_orders,
    get_recent_fills,
    get_recent_orders,
    get_heartbeats,
    get_danger_exited,
    get_config,
    get_recent_events,
)


def cmd_status(conn, args):
    """Health + positions + summary."""
    # Heartbeats
    heartbeats = get_heartbeats(conn)
    print("=== Health ===")
    if not heartbeats:
        print("  No heartbeats recorded")
    for hb in heartbeats:
        status_icon = "●" if hb["status"] == "ok" else "○"
        print(f"  {status_icon} {hb['process_name']:12s} {hb['status']:6s} {hb.get('last_beat', '')}")
        if hb.get("message"):
            print(f"    {hb['message']}")

    # Positions
    positions = get_positions(conn)
    print(f"\n=== Positions ({len(positions)}) ===")
    if positions:
        print(f"  {'Ticker':<30s} {'Side':>4s} {'Qty':>4s} {'Avg':>6s} {'City':<12s}")
        print(f"  {'-'*30} {'-'*4} {'-'*4} {'-'*6} {'-'*12}")
        total_at_risk = 0
        for p in positions:
            avg = p.get("avg_price_cents") or 0
            qty = p.get("contracts", 0)
            total_at_risk += avg * qty
            print(f"  {p['ticker']:<30s} {p['side']:>4s} {qty:>4d} {avg:>5.0f}c {p.get('city', ''):12s}")
        print(f"  Total at risk: {total_at_risk}c (${total_at_risk/100:.2f})")
    else:
        print("  No open positions")

    # Danger exited
    danger = get_danger_exited(conn)
    if danger:
        print(f"\n=== Danger Exited ({len(danger)}) ===")
        for t in sorted(danger):
            print(f"  {t}")


def cmd_evals(conn, args):
    """Latest cycle evaluations."""
    evals = get_latest_evaluations(conn)
    if not evals:
        print("No evaluations found")
        return

    # If ticker filter, do a direct query
    if args.ticker:
        evals = [e for e in evals if args.ticker.upper() in e.get("ticker", "").upper()]

    # Sort: passing first, then by p_win descending
    evals.sort(key=lambda e: (0 if e.get("filter_result") == "pass" else 1, -(e.get("p_win") or 0)))

    print(f"{'Ticker':<30s} {'Side':>4s} {'p_win':>6s} {'Margin':>7s} {'ECMWF':>5s} {'GFS':>5s} {'Filter':<15s}")
    print(f"{'-'*30} {'-'*4} {'-'*6} {'-'*7} {'-'*5} {'-'*5} {'-'*15}")
    for e in evals:
        pw = e.get("p_win") or 0
        margin = e.get("margin") or 0
        ecmwf = e.get("ecmwf") or 0
        gfs = e.get("gfs") or 0
        fr = e.get("filter_result", "?")
        marker = "✓" if fr == "pass" else "✗"
        print(f"  {marker} {e['ticker']:<28s} {e.get('side',''):>4s} {pw:>5.1%} {margin:>+6.1f}F {ecmwf:>5.1f} {gfs:>5.1f} {fr:<15s}")


def cmd_orders(conn, args):
    """Desired and actual orders."""
    desired = get_desired_orders(conn)
    print(f"=== Desired Orders ({len(desired)}) ===")
    if desired:
        print(f"  {'Ticker':<30s} {'Side':>4s} {'Price':>6s} {'Qty':>4s} {'Status':<12s} {'Reason'}")
        print(f"  {'-'*30} {'-'*4} {'-'*6} {'-'*4} {'-'*12} {'-'*20}")
        for o in desired:
            print(f"  {o['ticker']:<30s} {o.get('side',''):>4s} {o.get('price_cents',0):>5d}c {o.get('qty',0):>4d} {o.get('status',''):12s} {o.get('reason','')}")

    actual = get_recent_orders(conn, limit=20)
    if actual:
        print(f"\n=== Recent Orders ({len(actual)}) ===")
        for o in actual:
            print(f"  {o.get('ticker',''):<30s} {o.get('order_side',''):>4s} {o.get('price_cents',0):>5d}c x{o.get('qty',0)} [{o.get('status','')}]")


def cmd_fills(conn, args):
    """Recent fills."""
    fills = get_recent_fills(conn, limit=args.limit)
    if not fills:
        print("No fills")
        return
    print(f"{'Time':<20s} {'Ticker':<30s} {'Action':>6s} {'Side':>4s} {'Price':>6s} {'Qty':>4s} {'Fee':>5s}")
    print(f"{'-'*20} {'-'*30} {'-'*6} {'-'*4} {'-'*6} {'-'*4} {'-'*5}")
    for f in fills:
        print(f"  {f.get('created_at',''):<18s} {f['ticker']:<30s} {f.get('action',''):>6s} {f.get('side',''):>4s} {f.get('price_cents',0):>5d}c {f.get('qty',0):>4d} {f.get('fee_cents',0):>4.1f}c")


def cmd_pnl(conn, args):
    """Cumulative P&L from fills."""
    fills = get_recent_fills(conn, limit=10000)  # fetch all (practical upper bound)
    if not fills:
        print("No fills to compute P&L")
        return

    total_spent = 0  # cents spent on buys
    total_received = 0  # cents received from sells + settlements
    total_fees = 0
    buy_count = 0
    sell_count = 0

    for f in fills:
        action = f.get("action", "")
        price = f.get("price_cents", 0)
        qty = f.get("qty", 0)
        fee = f.get("fee_cents", 0) or 0
        total_fees += fee

        if action == "buy":
            total_spent += price * qty
            buy_count += qty
        elif action == "sell":
            total_received += price * qty
            sell_count += qty

    # Settled positions: contracts bought but not sold = assume settled at 100c (NO wins)
    # This is approximate — real settlement tracking would need more info
    positions = get_positions(conn)
    held_contracts = sum(p.get("contracts", 0) for p in positions)

    print(f"=== P&L Summary ===")
    print(f"  Buys:     {buy_count} contracts, {total_spent}c (${total_spent/100:.2f})")
    print(f"  Sells:    {sell_count} contracts, {total_received}c (${total_received/100:.2f})")
    print(f"  Fees:     {total_fees:.1f}c (${total_fees/100:.2f})")
    print(f"  Held:     {held_contracts} contracts")
    net = total_received - total_spent - total_fees
    print(f"  Net P&L:  {net:+.0f}c (${net/100:+.2f}) [excludes unsettled]")


def cmd_danger(conn, args):
    """Danger-exited tickers."""
    danger = get_danger_exited(conn)
    if not danger:
        print("No danger-exited tickers")
        return
    print(f"=== Danger Exited ({len(danger)}) ===")
    for t in sorted(danger):
        print(f"  {t}")


def cmd_events(conn, args):
    """Recent events."""
    events = get_recent_events(conn, limit=args.limit)
    if not events:
        print("No events")
        return
    for e in events:
        level = e.get("level", "info").upper()
        marker = "!" if level == "ERROR" else "·"
        print(f"  {marker} [{e.get('created_at','')}] {e.get('source',''):>10s} {level:>5s}: {e.get('message','')}")


def cmd_config(conn, args):
    """Current config."""
    config = get_config(conn)
    if not config:
        print("No config stored")
        return
    print("=== Config ===")
    for k, v in sorted(config.items()):
        print(f"  {k:<30s} = {v}")


def main():
    parser = argparse.ArgumentParser(description="Trading DB CLI")
    parser.add_argument("--db", default="data/trading.db", help="SQLite DB path")

    sub = parser.add_subparsers(dest="command")

    sub.add_parser("status", help="Health + positions summary")

    evals_p = sub.add_parser("evals", help="Latest evaluations")
    evals_p.add_argument("-t", "--ticker", help="Filter by ticker")

    sub.add_parser("orders", help="Desired + actual orders")

    fills_p = sub.add_parser("fills", help="Recent fills")
    fills_p.add_argument("-n", "--limit", type=int, default=50)

    sub.add_parser("pnl", help="P&L summary")
    sub.add_parser("danger", help="Danger-exited tickers")

    events_p = sub.add_parser("events", help="Recent events")
    events_p.add_argument("-n", "--limit", type=int, default=100)

    sub.add_parser("config", help="Current config")

    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        return

    db_path = Path(args.db)
    if not db_path.exists():
        print(f"DB not found: {db_path}")
        return

    conn = get_connection(db_path)

    commands = {
        "status": cmd_status,
        "evals": cmd_evals,
        "orders": cmd_orders,
        "fills": cmd_fills,
        "pnl": cmd_pnl,
        "danger": cmd_danger,
        "events": cmd_events,
        "config": cmd_config,
    }

    try:
        commands[args.command](conn, args)
    finally:
        conn.close()


if __name__ == "__main__":
    main()
