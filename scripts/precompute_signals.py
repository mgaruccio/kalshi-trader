#!/usr/bin/env python
"""Pre-compute SignalScore events offline for fast backtesting.

Runs ML inference once per ticker, producing SignalScore objects saved to
parquet via NT's catalog.write_data(). Backtests load from this file instead
of hitting the signal server's slow backfill endpoint.

Requires kalshi-weather on PYTHONPATH (for models + scoring logic).

Usage:
    cd ~/code/kalshi-trader
    uv run python scripts/precompute_signals.py
    uv run python scripts/precompute_signals.py --catalog-path kalshi_data_catalog
    uv run python scripts/precompute_signals.py --output data/model_signals.parquet
"""
from __future__ import annotations

import json
import logging
import sys
from datetime import datetime
from pathlib import Path

import pyarrow.parquet as pq

# kalshi-trader root on path for kalshi package
TRADER_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(TRADER_ROOT))

# kalshi-weather must be on path for model loading and scoring
KW_ROOT = Path.home() / "code/altmarkets/kalshi-weather"
sys.path.insert(0, str(KW_ROOT / "src"))

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
log = logging.getLogger(__name__)

CATALOG_DIR = Path(__file__).resolve().parent.parent / "kalshi_data_catalog"
CLIMATE_EVENTS = KW_ROOT / "data" / "climate_events.parquet"
OUTPUT = Path(__file__).resolve().parent.parent / "data" / "model_signals.parquet"

# Signal arrives 5 seconds after first quote tick for the instrument,
# so the strategy has a price to evaluate against.
SIGNAL_OFFSET_NS = 5_000_000_000


def load_models():
    """Load production ensemble models (V11 Brier-optimized weights)."""
    from kalshi_weather_ml.models.drn_model import DRNModel
    from kalshi_weather_ml.models.emos import EMOSModel
    from kalshi_weather_ml.models.ngboost_model import NGBoostModel

    models_dir = KW_ROOT / "data" / "models"

    emos = EMOSModel.load(models_dir / "emos_normal.json")
    log.info("Loaded emos (weight=0.029)")
    ngb = NGBoostModel.load(models_dir / "ngboost_normal_wx_spread.pkl")
    log.info("Loaded ngboost (weight=0.580)")
    drn = DRNModel.load(models_dir / "drn_normal_wx_spread")
    log.info("Loaded drn (weight=0.392)")

    return [emos, ngb, drn], ["emos", "ngboost", "drn"], [0.029, 0.580, 0.392]


def discover_tickers(catalog_path: Path) -> list[str]:
    """Get unique tickers from catalog quote_tick directories."""
    qt_dir = catalog_path / "data" / "quote_tick"
    if not qt_dir.exists():
        return []
    tickers = set()
    for inst_dir in qt_dir.iterdir():
        if not inst_dir.is_dir():
            continue
        name = inst_dir.name.replace(".KALSHI", "")
        if name.endswith("-YES") or name.endswith("-NO"):
            ticker = name.rsplit("-", 1)[0]
            tickers.add(ticker)
    return sorted(tickers)


def get_per_instrument_first_tick(catalog_path: Path) -> dict[str, dict]:
    """Get first tick data (timestamp, bid, ask) per instrument directory.

    Returns {instrument_name: {"ts_event": int, "bid_cents": int, "ask_cents": int}}.
    Uses NT's ArrowSerializer to properly decode fixed-point prices.
    """
    from nautilus_trader.model.data import QuoteTick
    from nautilus_trader.serialization.arrow.serializer import ArrowSerializer

    qt_dir = catalog_path / "data" / "quote_tick"
    result = {}
    if not qt_dir.exists():
        return result
    for inst_dir in sorted(qt_dir.iterdir()):
        if not inst_dir.is_dir():
            continue
        for f in sorted(inst_dir.glob("*.parquet")):
            try:
                table = pq.read_table(str(f))
                if len(table) == 0:
                    continue
                ticks = ArrowSerializer.deserialize(QuoteTick, table.slice(0, 1))
                if ticks:
                    tick = ticks[0]
                    result[inst_dir.name] = {
                        "ts_event": int(tick.ts_event),
                        "bid_cents": round(float(tick.bid_price) * 100),
                        "ask_cents": round(float(tick.ask_price) * 100),
                    }
                    break
            except Exception:
                continue
    return result


def load_city_features(
    climate_events_path: Path,
    needed_dates: set[str] | None = None,
) -> dict[tuple[str, str], dict]:
    """Load climate events and merge features per (city, date).

    Args:
        climate_events_path: Path to climate_events.parquet.
        needed_dates: Optional set of settlement dates (YYYY-MM-DD) to keep.
            If None, loads all dates.

    Returns {(city, date): {feature_name: value}}.
    """
    table = pq.read_table(str(climate_events_path))
    df = table.to_pandas()

    if needed_dates is not None:
        df = df[df["date"].isin(needed_dates)]

    city_dates: dict[str, set[str]] = {}
    for _, row in df.iterrows():
        date_val = str(row.get("date", ""))
        if date_val:
            city = str(row["city"])
            city_dates.setdefault(city, set()).add(date_val)

    cd_features: dict[tuple[str, str], dict] = {}
    for _, row in df.iterrows():
        city = str(row["city"])
        date_val = str(row.get("date", ""))
        features = json.loads(row["features"]) if isinstance(row["features"], str) else row["features"]
        parsed_features = {k: float(v) for k, v in features.items() if v is not None}

        if date_val:
            state = cd_features.setdefault((city, date_val), {})
            state.update(parsed_features)
        else:
            for dt in city_dates.get(city, set()):
                state = cd_features.setdefault((city, dt), {})
                state.update(parsed_features)

    return cd_features


def main():
    import argparse

    parser = argparse.ArgumentParser(description="Pre-compute SignalScore events for backtesting.")
    parser.add_argument("--catalog-path", default=str(CATALOG_DIR), help="Path to ParquetDataCatalog")
    parser.add_argument("--climate-events", default=str(CLIMATE_EVENTS), help="Path to climate_events.parquet")
    parser.add_argument("--output", default=str(OUTPUT), help="Output parquet path")
    args = parser.parse_args()

    catalog_path = Path(args.catalog_path)
    climate_path = Path(args.climate_events)
    output_path = Path(args.output)

    if not catalog_path.exists():
        log.error(f"Catalog not found: {catalog_path}")
        return 1
    if not climate_path.exists():
        log.error(f"Climate events not found: {climate_path}")
        return 1

    from kalshi_weather_ml.markets import SERIES_CONFIG, parse_ticker
    from kalshi_weather_ml.strategy import score_opportunities

    models, names, weights = load_models()
    if not models:
        log.error("No models loaded")
        return 1

    tickers = discover_tickers(catalog_path)
    log.info(f"Found {len(tickers)} tickers in catalog")

    inst_first_tick = get_per_instrument_first_tick(catalog_path)
    log.info(f"Per-instrument first ticks: {len(inst_first_tick)} instruments")

    # Extract settlement dates from tickers to filter climate events
    needed_dates: set[str] = set()
    for ticker in tickers:
        parsed = parse_ticker(ticker)
        if parsed:
            needed_dates.add(parsed["settlement_date"])
    log.info(f"Settlement dates in catalog: {sorted(needed_dates)}")

    city_features = load_city_features(climate_path, needed_dates=needed_dates)
    log.info(f"Loaded features for {len(city_features)} (city, date) pairs")

    series_to_city = {s: c for s, c in SERIES_CONFIG}
    now = datetime.now()

    # Map model names to the SignalScore field names
    # The old architecture used "emos", "ngboost_spread", "drn_spread"
    # SignalScore has emos_no, ngboost_no, drn_no fields
    MODEL_FIELD_MAP = {
        "emos": "emos_no",
        "ngboost": "ngboost_no",
        "ngboost_spread": "ngboost_no",
        "drn": "drn_no",
        "drn_spread": "drn_no",
    }

    from kalshi.signals import SignalScore

    scores: list[SignalScore] = []
    n_skip = 0

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
            model_scores = score_opportunities(
                ticker=ticker, city=city, direction="above",
                threshold=float(parsed["threshold"]),
                settlement_date=sd,
                ecmwf=ecmwf, gfs=gfs,
                models=models, model_names=names, model_weights=weights,
                now=now, extra_features=features,
            )
        except Exception as e:
            log.warning(f"Eval failed for {ticker}: {e}")
            n_skip += 1
            continue

        # Extract NO and YES side scores
        no_score = next((s for s in model_scores if s.side == "no"), None)
        yes_score = next((s for s in model_scores if s.side == "yes"), None)

        if no_score is None:
            n_skip += 1
            continue

        # Timestamp signal after the NO instrument's first quote tick
        no_inst_key = f"{ticker}-NO.KALSHI"
        no_tick_data = inst_first_tick.get(no_inst_key)
        if no_tick_data is None:
            n_skip += 1
            continue
        signal_ts = no_tick_data["ts_event"] + SIGNAL_OFFSET_NS

        # Read YES side bid/ask from first tick
        yes_inst_key = f"{ticker}-YES.KALSHI"
        yes_tick_data = inst_first_tick.get(yes_inst_key)
        if yes_tick_data is not None:
            yes_bid = yes_tick_data["bid_cents"]
            yes_ask = yes_tick_data["ask_cents"]
        else:
            log.warning(f"No YES tick data for {ticker} — using bid=0, ask=0")
            yes_bid = 0
            yes_ask = 0

        # Map per-model scores to SignalScore fields
        per_model = {}
        for model_name, pw in no_score.model_scores.items():
            field_name = MODEL_FIELD_MAP.get(model_name)
            if field_name:
                per_model[field_name] = pw

        signal = SignalScore(
            ticker=ticker,
            city=city,
            threshold=float(parsed["threshold"]),
            direction="above",
            no_p_win=no_score.p_win,
            yes_p_win=yes_score.p_win if yes_score else 0.0,
            no_margin=no_score.margin,
            n_models=no_score.n_models_scored,
            emos_no=per_model.get("emos_no", 0.0),
            ngboost_no=per_model.get("ngboost_no", 0.0),
            drn_no=per_model.get("drn_no", 0.0),
            yes_bid=yes_bid,
            yes_ask=yes_ask,
            status="active",
            ts_event=signal_ts,
            ts_init=signal_ts,
        )
        scores.append(signal)

    log.info(
        f"Generated {len(scores)} signals "
        f"({sum(1 for s in scores if s.no_p_win >= 0.95)} with pw>=0.95), "
        f"{n_skip} skipped"
    )

    if not scores:
        log.error("No signals generated — nothing to write")
        return 1

    # Sort by ts_event (required for catalog write and backtest replay)
    scores.sort(key=lambda s: s.ts_event)

    # Write using NT's catalog system — @customdataclass auto-registers Arrow serialization
    from nautilus_trader.persistence.catalog import ParquetDataCatalog

    output_path.parent.mkdir(parents=True, exist_ok=True)

    # Write to a temporary catalog directory, then copy the parquet file
    # to the desired output path for portability
    import tempfile

    with tempfile.TemporaryDirectory() as tmpdir:
        cat = ParquetDataCatalog(tmpdir)
        cat.write_data(scores)
        # Find the written file
        written = list(Path(tmpdir).rglob("*.parquet"))
        if written:
            import shutil
            shutil.copy2(written[0], output_path)
            log.info(f"Wrote {len(scores)} signals to {output_path}")
        else:
            log.error("catalog.write_data() produced no parquet files")
            return 1

    return 0


if __name__ == "__main__":
    sys.exit(main() or 0)
