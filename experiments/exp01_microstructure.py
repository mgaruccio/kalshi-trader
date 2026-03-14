#!/usr/bin/env python
"""Experiment 1: Market Microstructure at Open.

How do prices behave in the first 60 minutes after 10am ET market open?
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(Path.home() / "code/kalshi-trader"))

import pandas as pd
from tick_loader import load_ticks_df


def main():
    print("=" * 80)
    print("EXPERIMENT 1: Market Microstructure at Open")
    print("=" * 80)

    # Session 3ff8c34d has Mar 11 tickers that genuinely opened at 14:00 UTC
    df = load_ticks_df(["3ff8c34d"], no_only=True, t_type_only=True)
    if df.empty:
        print("No data"); return

    # Only Mar 11 tickers — these have real market opens in this session
    df = df[df["ticker"].str.contains("MAR11")].copy()
    print(f"\n{len(df):,} ticks, {df['ticker'].nunique()} Mar11 T-type NO tickers")
    print(f"Time range: {df['ts_dt'].min()} to {df['ts_dt'].max()}")

    # The open window starts at 14:00 UTC (10am ET)
    open_window = df[(df["hour_utc"] >= 14) & (df["hour_utc"] < 16)]
    if open_window.empty:
        print(f"\nNo data 14:00-16:00 UTC. Hours available: {sorted(df['hour_utc'].unique())}")
        open_window = df

    print(f"Open window: {len(open_window):,} ticks, {open_window['ticker'].nunique()} tickers")

    # --- Opening price vs stabilized price ---
    print("\n" + "=" * 80)
    print("OPENING vs STABILIZED PRICE (first 5min vs after 60min)")
    print("=" * 80)

    results = []
    for ticker, grp in open_window.groupby("ticker"):
        grp = grp.sort_values("ts_dt")
        t0 = grp["ts_dt"].iloc[0]
        early = grp[grp["ts_dt"] <= t0 + pd.Timedelta(minutes=5)]
        late = grp[grp["ts_dt"] >= t0 + pd.Timedelta(minutes=60)]
        if early.empty or late.empty:
            continue
        results.append({
            "ticker": ticker,
            "open_c": early["mid"].mean() * 100,
            "stable_c": late["mid"].median() * 100,
            "delta_c": (late["mid"].median() - early["mid"].mean()) * 100,
            "open_sprd_c": early["spread"].mean() * 100,
            "stbl_sprd_c": late["spread"].median() * 100,
            "n": len(grp),
        })

    if results:
        res = pd.DataFrame(results).sort_values("delta_c", key=abs, ascending=False)
        print(f"\n{'Ticker':<40} {'Open':>6} {'Stbl':>6} {'Delta':>7} {'OSprd':>6} {'SSprd':>6} {'N':>6}")
        print("-" * 78)
        for _, r in res.head(30).iterrows():
            print(f"{r['ticker']:<40} {r['open_c']:>5.1f}c {r['stable_c']:>5.1f}c {r['delta_c']:>+6.1f}c {r['open_sprd_c']:>5.1f}c {r['stbl_sprd_c']:>5.1f}c {r['n']:>6}")

        big = res[res["delta_c"].abs() > 2]
        print(f"\n{len(big)}/{len(res)} moved >2c ({len(big)/len(res)*100:.0f}%)")
        print(f"Mean |delta|={res['delta_c'].abs().mean():.1f}c, Median={res['delta_c'].abs().median():.1f}c")
        print(f"Mean open spread={res['open_sprd_c'].mean():.1f}c, stable spread={res['stbl_sprd_c'].mean():.1f}c")

    # --- Spread evolution (relative to open, not absolute time) ---
    print("\n" + "=" * 80)
    print("SPREAD BY MINUTES AFTER OPEN")
    print("=" * 80)

    # Compute minutes since each ticker's first tick (= market open)
    first_per_ticker = df.groupby("ticker")["ts_dt"].min()
    df = df.merge(first_per_ticker.rename("open_time"), on="ticker")
    df["min_since_open"] = (df["ts_dt"] - df["open_time"]).dt.total_seconds() / 60
    df["open_bucket"] = (df["min_since_open"] // 15).astype(int) * 15

    agg = df.groupby("open_bucket").agg(med_sprd=("spread", "median"), med_mid=("mid", "median"), n=("spread", "count"))
    agg = agg[agg.index >= 0]
    print(f"\n{'After Open':>10} {'MedSprd':>9} {'MedMid':>8} {'Ticks':>8}")
    print("-" * 40)
    for b, r in agg.iterrows():
        if b > 360:  # cap at 6h
            break
        print(f"T+{int(b):>3}min   {r['med_sprd']*100:>8.1f}c {r['med_mid']*100:>7.1f}c {r['n']:>8.0f}")

    # Also show by absolute time
    print("\n" + "=" * 80)
    print("SPREAD BY 15-MIN BUCKET (absolute UTC)")
    print("=" * 80)

    df["bucket"] = (df["hour_utc"] * 60 + df["ts_dt"].dt.minute) // 15 * 15
    agg = df.groupby("bucket").agg(med_sprd=("spread", "median"), med_mid=("mid", "median"), n=("spread", "count"))
    print(f"\n{'UTC':>8} {'MedSprd':>9} {'MedMid':>8} {'Ticks':>8}")
    print("-" * 38)
    for b, r in agg.iterrows():
        h, m = divmod(int(b), 60)
        print(f"{h:02d}:{m:02d}    {r['med_sprd']*100:>8.1f}c {r['med_mid']*100:>7.1f}c {r['n']:>8.0f}")

    # --- Price distribution ---
    print("\n" + "=" * 80)
    print("PRICE DISTRIBUTION")
    print("=" * 80)
    bins = pd.cut(df["mid"], bins=[0, 0.05, 0.15, 0.40, 0.60, 0.85, 0.95, 1.0],
                  labels=["<5c", "5-15c", "15-40c", "40-60c", "60-85c", "85-95c", ">95c"])
    for label, cnt in bins.value_counts().sort_index().items():
        print(f"  {label:<10} {cnt:>10,} ({cnt/len(df)*100:.1f}%)")

    # --- First tick times ---
    print("\n" + "=" * 80)
    print("FIRST TICK TIMES")
    print("=" * 80)
    first = df.groupby("ticker")["ts_dt"].min().sort_values()
    for t, ft in first.head(15).items():
        print(f"  {t:<40} {ft.strftime('%H:%M:%S')} UTC")
    if len(first) > 15:
        print(f"  ... {len(first)-15} more")
    hours = first.dt.hour.value_counts().sort_index()
    print("\nFirst ticks by hour (UTC):")
    for h, c in hours.items():
        print(f"  {h:02d}:00 UTC ({(h-4)%24:02d}:00 ET): {c} tickers")


if __name__ == "__main__":
    main()
