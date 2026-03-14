#!/usr/bin/env python
"""Experiment 2: Orderbook Depth Profiling.

How liquid are these markets? Can we fill meaningful size?
Loads from both sessions for more data.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(Path.home() / "code/kalshi-trader"))

import pandas as pd
from tick_loader import load_ticks_df


def main():
    print("=" * 80)
    print("EXPERIMENT 2: Orderbook Depth Profiling")
    print("=" * 80)

    # Load both sessions, T-type NO only
    df = load_ticks_df(["89b0770f", "3ff8c34d"], no_only=True, t_type_only=True)
    if df.empty:
        print("No data"); return

    print(f"\n{len(df):,} ticks, {df['ticker'].nunique()} tickers")

    # --- Depth by price bucket ---
    print("\n" + "=" * 80)
    print("DEPTH BY PRICE BUCKET")
    print("=" * 80)

    bins = [0, 0.05, 0.15, 0.40, 0.60, 0.85, 0.95, 1.0]
    labels = ["<5c", "5-15c", "15-40c", "40-60c", "60-85c", "85-95c", ">95c"]
    df["pb"] = pd.cut(df["mid"], bins=bins, labels=labels)

    print(f"\n{'Bucket':<10} {'MdBid':>6} {'P5Bid':>6} {'P95Bid':>7} {'MdAsk':>6} {'P5Ask':>6} {'P95Ask':>7} {'MdSprd':>7} {'N':>9}")
    print("-" * 72)
    for lbl in labels:
        s = df[df["pb"] == lbl]
        if s.empty: continue
        print(f"{lbl:<10} {s['bid_size'].median():>6.0f} {s['bid_size'].quantile(.05):>6.0f} {s['bid_size'].quantile(.95):>7.0f} "
              f"{s['ask_size'].median():>6.0f} {s['ask_size'].quantile(.05):>6.0f} {s['ask_size'].quantile(.95):>7.0f} "
              f"{s['spread'].median()*100:>6.1f}c {len(s):>9,}")

    # --- Depth by time ---
    print("\n" + "=" * 80)
    print("DEPTH BY TIME OF DAY (ET)")
    print("=" * 80)
    for s, e, lbl in [(10,12,"10am-12pm"),(12,15,"12-3pm"),(15,17,"3-5pm"),(17,20,"5-8pm"),(20,24,"8pm-12am")]:
        sub = df[(df["hour_et"] >= s) & (df["hour_et"] < e)]
        if sub.empty: continue
        print(f"  {lbl:<12} bid={sub['bid_size'].median():>4.0f}  ask={sub['ask_size'].median():>4.0f}  sprd={sub['spread'].median()*100:.1f}c  n={len(sub):,}")

    # --- Target range 85-95c ---
    print("\n" + "=" * 80)
    print("TARGET RANGE: 85-95c NO")
    print("=" * 80)
    tgt = df[(df["mid"] >= 0.85) & (df["mid"] <= 0.95)]
    if tgt.empty:
        print("No ticks in 85-95c"); return

    print(f"\nTicks: {len(tgt):,} ({len(tgt)/len(df)*100:.1f}%)")
    print(f"Bid: med={tgt['bid_size'].median():.0f} P5={tgt['bid_size'].quantile(.05):.0f} P95={tgt['bid_size'].quantile(.95):.0f}")
    print(f"Ask: med={tgt['ask_size'].median():.0f} P5={tgt['ask_size'].quantile(.05):.0f} P95={tgt['ask_size'].quantile(.95):.0f}")
    print(f"Sprd: med={tgt['spread'].median()*100:.1f}c P5={tgt['spread'].quantile(.05)*100:.1f}c P95={tgt['spread'].quantile(.95)*100:.1f}c")

    # Per-ticker at target range
    print(f"\n{'Ticker':<40} {'MdBid':>6} {'MdAsk':>6} {'MdSprd':>7} {'N':>6}")
    print("-" * 68)
    pt = tgt.groupby("ticker").agg(mb=("bid_size","median"), ma=("ask_size","median"), ms=("spread","median"), n=("bid_size","count"))
    for t, r in pt.sort_values("mb", ascending=False).head(25).iterrows():
        print(f"{t:<40} {r['mb']:>6.0f} {r['ma']:>6.0f} {r['ms']*100:>6.1f}c {r['n']:>6.0f}")

    # --- Fill assessment ---
    print("\n" + "=" * 80)
    print("FILL ASSESSMENT")
    print("=" * 80)
    td = tgt.groupby("ticker")["ask_size"].median()
    print(f"Ask depth at 85-95c per ticker: med={td.median():.0f} P25={td.quantile(.25):.0f} P75={td.quantile(.75):.0f} min={td.min():.0f} max={td.max():.0f}")
    if td.median() < 5:
        print(">> THIN: Use maker mode. max_contracts=20 unrealistic for taker.")
    elif td.median() < 20:
        print(">> MODERATE: Taker up to ~10, use maker for 20.")
    else:
        print(">> GOOD: Taker fills at 20 feasible.")

    # Exit zone >95c
    ex = df[df["mid"] > 0.95]
    if not ex.empty:
        print(f"\nExit zone (>95c): {len(ex):,} ticks, bid depth med={ex['bid_size'].median():.0f}, sprd={ex['spread'].median()*100:.1f}c")


if __name__ == "__main__":
    main()
