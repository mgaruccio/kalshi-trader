#!/usr/bin/env python
"""Experiment 3: Model vs Market Fair Value.

Compares production ensemble p_win to market-implied p_win from tick data.
"""
import sys
import time
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
    print("EXPERIMENT 3: Model vs Market Fair Value")
    print("=" * 80)

    df = load_ticks_df(["89b0770f", "3ff8c34d"], no_only=True, t_type_only=True)
    if df.empty:
        print("No data"); return

    # Market mid = median of last hour per ticker
    mids = {}
    for ticker, grp in df.groupby("ticker"):
        grp = grp.sort_values("ts_dt")
        last_hr = grp[grp["ts_dt"] >= grp["ts_dt"].max() - pd.Timedelta(hours=1)]
        if not last_hr.empty:
            mids[ticker] = last_hr["mid"].median()

    print(f"Market mids for {len(mids)} tickers")
    del df  # free memory before model inference

    # Load models
    emos = EMOSModel.load(MODELS_DIR / "emos_normal.json")
    ngb = NGBoostModel.load(MODELS_DIR / "ngboost_normal.pkl", calibration_path=CAL_PATH)
    clear_cache()

    comps = []
    fc_cache = {}
    for ticker, mkt_mid in mids.items():
        parsed = parse_ticker(ticker)
        if not parsed: continue
        city = SERIES_TO_CITY.get(parsed["series"])
        if not city or city not in CITY_COORDS: continue

        ck = (city, parsed["settlement_date"])
        if ck not in fc_cache:
            ecmwf = get_forecast(city, parsed["settlement_date"], "ecmwf_ifs025")
            gfs = get_forecast(city, parsed["settlement_date"], "gfs_seamless")
            time.sleep(0.15)
            fc_cache[ck] = (ecmwf, gfs)
        ecmwf, gfs = fc_cache[ck]
        if ecmwf is None or gfs is None: continue

        ep = 1 - emos.predict_bust_prob(ecmwf, gfs, parsed["threshold"], city, lead_idx=0)
        np_ = 1 - ngb.predict_bust_prob(ecmwf, gfs, parsed["threshold"], city, lead_idx=0)
        ens = 0.5 * ep + 0.5 * np_
        comps.append({
            "ticker": ticker, "city": city, "T": parsed["threshold"],
            "date": parsed["settlement_date"], "ecmwf": ecmwf, "gfs": gfs,
            "margin": parsed["threshold"] - max(ecmwf, gfs),
            "emos": ep, "ngb": np_, "ens": ens, "mkt": mkt_mid,
            "edge_c": (ens - mkt_mid) * 100,
        })

    if not comps:
        print("No comparisons"); return

    c = pd.DataFrame(comps).sort_values("edge_c", ascending=False)

    # --- Main table ---
    print(f"\n{'Ticker':<35} {'ECMWF':>5} {'GFS':>5} {'Marg':>5} {'EMOS':>6} {'NGB':>6} {'Ens':>6} {'Mkt':>6} {'Edge':>7}")
    print("-" * 88)
    for _, r in c.head(25).iterrows():
        f = " <<" if abs(r["edge_c"]) > 5 else ""
        print(f"{r['ticker']:<35} {r['ecmwf']:>5.1f} {r['gfs']:>5.1f} {r['margin']:>+5.1f} "
              f"{r['emos']:>5.3f} {r['ngb']:>5.3f} {r['ens']:>5.3f} {r['mkt']:>5.3f} {r['edge_c']:>+6.1f}c{f}")

    print(f"\n... bottom 10:")
    for _, r in c.tail(10).iterrows():
        f = " <<" if abs(r["edge_c"]) > 5 else ""
        print(f"{r['ticker']:<35} {r['ecmwf']:>5.1f} {r['gfs']:>5.1f} {r['margin']:>+5.1f} "
              f"{r['emos']:>5.3f} {r['ngb']:>5.3f} {r['ens']:>5.3f} {r['mkt']:>5.3f} {r['edge_c']:>+6.1f}c{f}")

    # --- Summary ---
    print(f"\nN={len(c)}, mean edge={c['edge_c'].mean():+.1f}c, median={c['edge_c'].median():+.1f}c, std={c['edge_c'].std():.1f}c")
    print(f"Edge >5c: {len(c[c['edge_c']>5])}, >3c: {len(c[c['edge_c']>3])}, <-5c: {len(c[c['edge_c']<-5])}")

    # --- Calibration ---
    print("\nCalibration bins:")
    for lo, hi, lbl in [(0,.8,"<80%"),(.8,.9,"80-90%"),(.9,.95,"90-95%"),(.95,.98,"95-98%"),(.98,1.01,">98%")]:
        s = c[(c["ens"] >= lo) & (c["ens"] < hi)]
        if s.empty: continue
        print(f"  {lbl}: n={len(s)}, mkt avg={s['mkt'].mean():.3f}, edge avg={s['edge_c'].mean():+.1f}c")

    # --- Actionable ---
    print("\nActionable (ens>=0.95, edge>0):")
    act = c[(c["ens"] >= 0.95) & (c["edge_c"] > 0)]
    if act.empty:
        print("  None")
    else:
        for _, r in act.iterrows():
            cost = round((1 - r["mkt"]) * 100)
            print(f"  {r['ticker']:<35} ens={r['ens']:.3f} mkt={r['mkt']:.3f} edge={r['edge_c']:+.1f}c cost~{cost}c")


if __name__ == "__main__":
    main()
