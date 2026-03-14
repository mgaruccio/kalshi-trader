#!/usr/bin/env python
"""Experiment 4: Entry Timing Window.

When is the optimal time to enter? Right at open? 30 min after?
Uses session 3ff8c34d — Mar 11 tickers opened at 14:00 UTC (10am ET).
"""
import sys
import time as time_mod
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(Path.home() / "code/kalshi-trader"))
sys.path.insert(0, str(Path.home() / "code/altmarkets/kalshi-weather/src"))

import pandas as pd
from tick_loader import load_ticks_df

from kalshi_weather_ml.forecasts import get_forecast, CITY_COORDS, clear_cache
from kalshi_weather_ml.markets import parse_ticker, SERIES_CONFIG
from kalshi_weather_ml.models.emos import EMOSModel
from kalshi_weather_ml.models.ngboost_model import NGBoostModel

MODELS_DIR = Path.home() / "code/altmarkets/kalshi-weather/data/models"
CAL_PATH = Path.home() / "code/altmarkets/kalshi-weather/data/calibration_dataset.parquet"
SERIES_TO_CITY = {s: c for s, c in SERIES_CONFIG}


def main():
    print("=" * 80)
    print("EXPERIMENT 4: Entry Timing Window")
    print("=" * 80)

    df = load_ticks_df(["3ff8c34d"], no_only=True, t_type_only=True)
    if df.empty:
        print("No data"); return

    # Only Mar 11 tickers — genuine market opens at 14:00 UTC
    df = df[df["ticker"].str.contains("MAR11")].copy()
    print(f"{len(df):,} Mar11 T-type NO ticks, {df['ticker'].nunique()} tickers")
    print(f"Time range: {df['ts_dt'].min()} to {df['ts_dt'].max()}")

    # Identify qualifying tickers
    emos = EMOSModel.load(MODELS_DIR / "emos_normal.json")
    ngb = NGBoostModel.load(MODELS_DIR / "ngboost_normal.pkl", calibration_path=CAL_PATH)
    clear_cache()

    qualifying = {}
    fc_cache = {}
    for ticker in df["ticker"].unique():
        parsed = parse_ticker(ticker)
        if not parsed: continue
        city = SERIES_TO_CITY.get(parsed["series"])
        if not city or city not in CITY_COORDS: continue

        ck = (city, parsed["settlement_date"])
        if ck not in fc_cache:
            ecmwf = get_forecast(city, parsed["settlement_date"], "ecmwf_ifs025")
            gfs = get_forecast(city, parsed["settlement_date"], "gfs_seamless")
            time_mod.sleep(0.15)
            fc_cache[ck] = (ecmwf, gfs)
        ecmwf, gfs = fc_cache[ck]
        if ecmwf is None or gfs is None: continue

        ep = 1 - emos.predict_bust_prob(ecmwf, gfs, parsed["threshold"], city, lead_idx=0)
        np_ = 1 - ngb.predict_bust_prob(ecmwf, gfs, parsed["threshold"], city, lead_idx=0)
        if min(ep, np_) >= 0.90:
            qualifying[ticker] = {"emos": ep, "ngb": np_, "min": min(ep, np_)}

    print(f"Qualifying (min_pwin >= 0.90): {len(qualifying)}")
    if not qualifying:
        print("None qualifying, using all T-type")
        qualifying = {t: {"min": 0} for t in df["ticker"].unique()}

    qdf = df[df["ticker"].isin(qualifying)]
    first_ticks = qdf.groupby("ticker")["ts_dt"].min().to_dict()

    offsets = [0, 5, 15, 30, 60, 120]
    timing = []
    for ticker, t0 in first_ticks.items():
        tdf = qdf[qdf["ticker"] == ticker].sort_values("ts_dt")
        for off in offsets:
            ws = t0 + pd.Timedelta(minutes=max(0, off - 2))
            we = t0 + pd.Timedelta(minutes=off + 3)
            w = tdf[(tdf["ts_dt"] >= ws) & (tdf["ts_dt"] <= we)]
            if w.empty: continue
            timing.append({
                "ticker": ticker, "off": off,
                "ask_c": w["ask_price"].median() * 100,
                "mid_c": w["mid"].median() * 100,
                "sprd_c": w["spread"].median() * 100,
                "depth": w["ask_size"].median(),
            })

    if not timing:
        print("No timing data"); return

    tm = pd.DataFrame(timing)

    print(f"\n{'Offset':>8} {'MedAsk':>8} {'MedMid':>8} {'MedSprd':>9} {'MedDpth':>8} {'Tickers':>8}")
    print("-" * 52)
    for off in offsets:
        s = tm[tm["off"] == off]
        if s.empty: continue
        print(f"T+{off:>3}min {s['ask_c'].median():>7.1f}c {s['mid_c'].median():>7.1f}c "
              f"{s['sprd_c'].median():>8.1f}c {s['depth'].median():>8.0f} {s['ticker'].nunique():>8}")

    # Savings vs T+0
    print("\nSavings vs T+0:")
    t0p = tm[tm["off"] == 0].set_index("ticker")["ask_c"]
    for off in [5, 15, 30, 60, 120]:
        tp = tm[tm["off"] == off].set_index("ticker")["ask_c"]
        common = t0p.index.intersection(tp.index)
        if len(common) < 3: continue
        sav = t0p[common] - tp[common]
        print(f"  T+{off}min: mean={sav.mean():+.1f}c med={sav.median():+.1f}c better@T0={int((sav<0).sum())} better@T+{off}={int((sav>0).sum())}")

    # Spread convergence
    print("\nSpread convergence (% with spread <= 2c):")
    for off in offsets:
        s = tm[tm["off"] == off]
        if s.empty: continue
        pct = (s["sprd_c"] <= 2).sum() / len(s) * 100
        print(f"  T+{off:>3}min: {pct:.0f}%")

    # Top detail
    print(f"\nTop 10 qualifying tickers:")
    for ticker, scores in sorted(qualifying.items(), key=lambda x: -x[1]["min"])[:10]:
        tt = tm[tm["ticker"] == ticker].sort_values("off")
        if tt.empty: continue
        print(f"\n  {ticker} (pwin={scores['min']:.3f}):")
        for _, r in tt.iterrows():
            print(f"    T+{r['off']:>3}min: ask={r['ask_c']:.0f}c sprd={r['sprd_c']:.1f}c depth={r['depth']:.0f}")


if __name__ == "__main__":
    main()
