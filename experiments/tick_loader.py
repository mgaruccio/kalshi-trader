"""Shared tick data loader for experiments.

Uses ArrowSerializer to decode Nautilus fixed-point binary encoding,
then builds pandas DataFrames directly (not per-tick dicts) for
memory efficiency. A full session is ~4M ticks = ~200MB as DataFrame
vs ~2GB as list[dict].
"""
import sys
from pathlib import Path

import pandas as pd
import pyarrow.ipc as ipc

CATALOG = Path.home() / "code/kalshi-trader/kalshi_data_catalog"


def load_ticks_df(session_ids: list[str], no_only: bool = False, t_type_only: bool = False) -> pd.DataFrame:
    """Load QuoteTicks from sessions directly into a DataFrame.

    Deserializes via ArrowSerializer, extracts values in batches per file,
    then concatenates. Much more memory-efficient than per-tick dicts.

    Args:
        session_ids: List of session ID prefixes (e.g. ["89b0770f"])
        no_only: If True, only load -NO instruments
        t_type_only: If True, only load -T (threshold) instruments, skip -B (bracket)

    Returns DataFrame with columns:
        instrument, bid_price, ask_price, bid_size, ask_size, ts_event, ts_init,
        ts_dt, mid, spread, ticker, side, hour_utc, hour_et
    """
    from nautilus_trader.model.data import QuoteTick
    from nautilus_trader.serialization.arrow.serializer import ArrowSerializer

    frames = []
    total = 0

    for session_id in session_ids:
        live_dir = CATALOG / "live"
        matches = [d for d in live_dir.iterdir() if d.is_dir() and d.name.startswith(session_id)]
        if not matches:
            print(f"WARNING: No session matching {session_id}")
            continue
        session_dir = matches[0]
        qt_dir = session_dir / "quote_tick"
        if not qt_dir.exists():
            print(f"WARNING: No quote_tick dir in {session_dir.name}")
            continue

        subdirs = sorted(qt_dir.iterdir())
        count = 0
        for subdir in subdirs:
            if not subdir.is_dir():
                continue
            instrument = subdir.name
            if no_only and "-NO" not in instrument:
                continue
            if t_type_only and "-T" not in instrument:
                continue

            for fp in sorted(subdir.glob("*.feather")):
                try:
                    with open(fp, "rb") as f:
                        table = ipc.open_stream(f).read_all()
                    pyo3_ticks = ArrowSerializer.deserialize(QuoteTick, table)

                    # Batch extract into columnar arrays
                    n = len(pyo3_ticks)
                    if n == 0:
                        continue

                    bid_p = [0.0] * n
                    ask_p = [0.0] * n
                    bid_s = [0] * n
                    ask_s = [0] * n
                    ts_ev = [0] * n
                    ts_in = [0] * n

                    for i, t in enumerate(pyo3_ticks):
                        ct = QuoteTick.from_pyo3(t)
                        bid_p[i] = ct.bid_price.as_double()
                        ask_p[i] = ct.ask_price.as_double()
                        bid_s[i] = int(ct.bid_size.as_double())
                        ask_s[i] = int(ct.ask_size.as_double())
                        ts_ev[i] = ct.ts_event
                        ts_in[i] = ct.ts_init

                    chunk = pd.DataFrame({
                        "instrument": instrument,
                        "bid_price": bid_p,
                        "ask_price": ask_p,
                        "bid_size": bid_s,
                        "ask_size": ask_s,
                        "ts_event": ts_ev,
                        "ts_init": ts_in,
                    })
                    frames.append(chunk)
                    count += n
                except Exception as e:
                    print(f"  Skip {fp.name}: {e}")

        print(f"Session {session_dir.name[:8]}: {count:,} ticks")
        total += count

    if not frames:
        print("ERROR: No tick data loaded")
        return pd.DataFrame()

    df = pd.concat(frames, ignore_index=True)
    del frames  # Free memory immediately

    # Derive columns
    df["ts_dt"] = pd.to_datetime(df["ts_event"], unit="ns", utc=True)
    df["mid"] = (df["bid_price"] + df["ask_price"]) / 2
    df["spread"] = df["ask_price"] - df["bid_price"]
    df["ticker"] = df["instrument"].str.replace(r"-(YES|NO)(\.KALSHI)?$", "", regex=True)
    df["side"] = df["instrument"].apply(
        lambda x: "NO" if "-NO" in x else ("YES" if "-YES" in x else "?")
    )
    df["hour_utc"] = df["ts_dt"].dt.hour
    df["hour_et"] = (df["hour_utc"] - 4) % 24  # Rough EDT

    print(f"Total: {total:,} ticks, {df['ticker'].nunique()} tickers, {df.memory_usage(deep=True).sum()/1e6:.0f}MB")
    return df
