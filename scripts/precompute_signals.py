#!/usr/bin/env python
"""Pre-compute ModelSignals offline for fast backtesting.

Runs ML inference once per (ticker, side) combination for each climate event
window, producing model_signals.parquet. This avoids repeated inference
during NautilusTrader backtest — signals are injected as CustomData.

Usage:
    cd ~/code/kalshi-trader
    uv run python scripts/precompute_signals.py
"""
import json
import logging
import sys
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq

# kalshi-weather on path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, "/home/mike/code/altmarkets/kalshi-weather/src")

from kalshi_weather_ml.config import load_config
from kalshi_weather_ml.markets import parse_ticker, SERIES_CONFIG
from kalshi_weather_ml.strategy import score_opportunities
from kalshi_weather_ml.models.emos import EMOSModel
from kalshi_weather_ml.models.ngboost_model import NGBoostModel
from kalshi_weather_ml.models.drn_model import DRNModel
from datetime import datetime

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
log = logging.getLogger(__name__)

KW_ROOT = Path("/home/mike/code/altmarkets/kalshi-weather")
TRADER_ROOT = Path(__file__).resolve().parent.parent
CATALOG_DIR = TRADER_ROOT / "kalshi_data_catalog"
CLIMATE_EVENTS = KW_ROOT / "data" / "climate_events.parquet"
OUTPUT = TRADER_ROOT / "data" / "model_signals.parquet"


def load_models(config):
    """Load ensemble models from config, matching FeatureActor logic."""
    models_dir = KW_ROOT / "data" / "models"
    model_loaders = {
        "emos": lambda: EMOSModel.load(models_dir / "emos_normal.json"),
        "ngboost": lambda: NGBoostModel.load(models_dir / "ngboost_normal.pkl"),
        "ngboost_spread": lambda: NGBoostModel.load(models_dir / "ngboost_normal_wx_spread.pkl"),
        "drn": lambda: DRNModel.load(models_dir / "drn_normal"),
        "drn_spread": lambda: DRNModel.load(models_dir / "drn_normal_wx_spread"),
    }
    model_names = config.ensemble_models or ["emos", "ngboost"]
    weights_map = config.ensemble_weights or {}

    models, names, weights = [], [], []
    for name in model_names:
        loader = model_loaders.get(name)
        if not loader:
            continue
        try:
            models.append(loader())
            names.append(name)
            weights.append(weights_map.get(name, 1.0))
            log.info(f"Loaded {name} (weight={weights[-1]})")
        except Exception as e:
            log.error(f"Failed to load {name}: {e}")

    return models, names, weights


def discover_tickers(catalog_path: Path) -> list[str]:
    """Get unique tickers from catalog instruments."""
    qt_dir = catalog_path / "data" / "quote_tick"
    if not qt_dir.exists():
        return []
    tickers = set()
    for inst_dir in qt_dir.iterdir():
        if not inst_dir.is_dir():
            continue
        # Directory names: "KXHIGHCHI-26MAR10-T55-NO.KALSHI"
        name = inst_dir.name.replace(".KALSHI", "")
        if name.endswith("-YES") or name.endswith("-NO"):
            ticker = name.rsplit("-", 1)[0]
            tickers.add(ticker)
    return sorted(tickers)


def get_first_tick_ns(catalog_path: Path) -> int | None:
    """Get first tick timestamp from catalog."""
    qt_dir = catalog_path / "data" / "quote_tick"
    if not qt_dir.exists():
        return None
    for inst_dir in sorted(qt_dir.iterdir()):
        if not inst_dir.is_dir():
            continue
        for f in sorted(inst_dir.glob("*.parquet")):
            try:
                col = pq.read_table(str(f), columns=["ts_event"]).column("ts_event")
                if len(col) > 0:
                    return int(col[0].as_py())
            except Exception:
                continue
    return None


def get_per_instrument_first_tick(catalog_path: Path) -> dict[str, int]:
    """Get first tick timestamp per instrument directory.

    Returns {instrument_dir_name: first_tick_ns}.
    Signals must arrive AFTER the instrument's first quote tick,
    otherwise the strategy has no price to evaluate against.
    """
    qt_dir = catalog_path / "data" / "quote_tick"
    result = {}
    if not qt_dir.exists():
        return result
    for inst_dir in sorted(qt_dir.iterdir()):
        if not inst_dir.is_dir():
            continue
        for f in sorted(inst_dir.glob("*.parquet")):
            try:
                col = pq.read_table(str(f), columns=["ts_event"]).column("ts_event")
                if len(col) > 0:
                    result[inst_dir.name] = int(col[0].as_py())
                    break
            except Exception:
                continue
    return result


def load_city_features(climate_events_path: Path, cutoff_ns: int | None) -> dict[tuple[str, str], dict]:
    """Load climate events and merge features per (city, date).

    Returns {(city, date): {feature_name: value}} with date-specific isolation.
    Date-specific events go to exact (city, date) buckets.
    City-level events (date=="") fan out to all known dates for that city.
    """
    table = pq.read_table(str(climate_events_path))
    df = table.to_pandas()

    if cutoff_ns is not None:
        df = df[df["ts_event"] >= cutoff_ns]

    # First pass: collect all (city, date) pairs from date-specific events
    city_dates: dict[str, set[str]] = {}
    for _, row in df.iterrows():
        date_val = str(row.get("date", ""))
        if date_val:
            city = str(row["city"])
            city_dates.setdefault(city, set()).add(date_val)

    # Second pass: route events to (city, date) buckets
    cd_features: dict[tuple[str, str], dict] = {}
    for _, row in df.iterrows():
        city = str(row["city"])
        date_val = str(row.get("date", ""))
        features = json.loads(row["features"]) if isinstance(row["features"], str) else row["features"]
        parsed_features = {k: float(v) for k, v in features.items()}

        if date_val:
            # Date-specific: exact bucket
            state = cd_features.setdefault((city, date_val), {})
            state.update(parsed_features)
        else:
            # City-level: fan out to all known dates for this city
            for dt in city_dates.get(city, set()):
                state = cd_features.setdefault((city, dt), {})
                state.update(parsed_features)

    return cd_features


def main():
    config = load_config()
    # No min_p_win hack needed — score_opportunities() never filters.
    models, names, weights = load_models(config)
    if not models:
        log.error("No models loaded")
        return

    tickers = discover_tickers(CATALOG_DIR)
    log.info(f"Found {len(tickers)} tickers in catalog")

    first_ns = get_first_tick_ns(CATALOG_DIR)
    log.info(f"First tick ns: {first_ns}")

    # Per-instrument first tick: signals must arrive AFTER quotes exist
    inst_first_tick = get_per_instrument_first_tick(CATALOG_DIR)
    log.info(f"Per-instrument first ticks: {len(inst_first_tick)} instruments")

    city_features = load_city_features(CLIMATE_EVENTS, first_ns)
    log.info(f"Loaded features for {len(city_features)} cities")

    series_to_city = {s: c for s, c in SERIES_CONFIG}
    now = datetime.now()

    rows = []
    n_accept = 0
    n_reject = 0
    n_skip = 0
    SIGNAL_OFFSET_NS = 5_000_000_000  # 5 seconds after first quote

    for ticker in tickers:
        parsed = parse_ticker(ticker)
        if not parsed:
            n_skip += 1
            continue

        city = series_to_city.get(parsed["series"], "")
        if not city:
            n_skip += 1
            continue

        sd = parsed["settlement_date"]
        features = city_features.get((city, sd))
        if not features:
            n_skip += 1
            continue

        ecmwf = features.get("ecmwf_high")
        gfs = features.get("gfs_high")
        if ecmwf is None or gfs is None:
            n_skip += 1
            continue

        try:
            scores = score_opportunities(
                ticker=ticker, city=city, direction="above",
                threshold=float(parsed["threshold"]),
                settlement_date=parsed["settlement_date"],
                ecmwf=ecmwf, gfs=gfs,
                models=models, model_names=names, model_weights=weights,
                now=now, extra_features=features,
            )
        except Exception as e:
            log.warning(f"Eval failed for {ticker}: {e}")
            n_skip += 1
            continue

        for s in scores:
            if s.side != "no":
                continue
            # Timestamp signal AFTER the instrument's first quote tick
            # so the strategy has a price to evaluate against
            inst_key = f"{ticker}-{s.side.upper()}.KALSHI"
            inst_first = inst_first_tick.get(inst_key)
            if inst_first is None:
                n_skip += 1
                continue
            signal_ts = inst_first + SIGNAL_OFFSET_NS

            model_scores = {n: s.model_scores.get(n, 0.0) for n in names}
            rows.append({
                "city": city,
                "ticker": ticker,
                "side": s.side,
                "p_win": s.p_win,
                "model_scores": json.dumps(model_scores),
                "features_snapshot": json.dumps({}),  # not needed for backtest
                "ts_event": signal_ts,
                "ts_init": signal_ts,
            })
            if s.p_win >= 0.95:
                n_accept += 1
            else:
                n_reject += 1

    log.info(f"Generated {len(rows)} signals: {n_accept} with pw>=0.95, {n_reject} with pw<0.95, {n_skip} skipped")

    if rows:
        OUTPUT.parent.mkdir(parents=True, exist_ok=True)
        table = pa.Table.from_pylist(rows)
        pq.write_table(table, str(OUTPUT))
        log.info(f"Wrote {OUTPUT}")


if __name__ == "__main__":
    main()
