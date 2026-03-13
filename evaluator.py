"""Standalone ML evaluator — scores all markets, writes to SQLite + Redis.

Runs independently of NautilusTrader. Publishes ModelSignal to Redis streams
for the NT executor to consume via external_streams.
"""
import argparse
import json
import logging
import os
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

import msgpack
import numpy as np
import redis

# kalshi_weather_ml imports
_KW_ROOT = os.environ.get("KALSHI_WEATHER_ROOT", "/home/mike/code/altmarkets/kalshi-weather")
sys.path.insert(0, f"{_KW_ROOT}/src")

from kalshi_weather_ml.strategy import score_opportunities, apply_entry_filters
from kalshi_weather_ml.markets import fetch_open_markets, parse_ticker, SERIES_CONFIG
from kalshi_weather_ml.forecasts import get_forecast, get_weather_features, PRIMARY_MODEL, CONSENSUS_MODEL
from kalshi_weather_ml.config import load_config
from kalshi_weather_ml.models.emos import EMOSModel
from kalshi_weather_ml.models.ngboost_model import NGBoostModel
from kalshi_weather_ml.models.drn_model import DRNModel

from db import init_db, get_connection, write_evaluations, write_desired_orders
from db import upsert_market, upsert_forecast, beat_heartbeat, log_event, get_positions
from data_types import ModelSignal

log = logging.getLogger(__name__)

# Coast normals for wind_dir_offshore — mirrors feature_actor._build_extra_features
_COAST_NORMAL = {
    "miami": 90, "tampa": 260, "jacksonville": 60,
    "new_york": 150, "boston": 90, "baltimore": 170,
    "norfolk": 130, "houston": 150, "new_orleans": 180,
    "los_angeles": 240, "san_francisco": 270, "san_diego": 260,
    "seattle": 270, "portland": 290, "charleston": 120,
    "savannah": 100, "philadelphia": 135, "washington_dc": 135,
}


def load_models(config):
    """Load ensemble models from config. Returns (models, names, weights)."""
    kw_root = Path(_KW_ROOT)
    models_dir = kw_root / "data" / "models"

    model_loaders = {
        "emos":           lambda: EMOSModel.load(models_dir / "emos_normal.json"),
        "ngboost":        lambda: NGBoostModel.load(models_dir / "ngboost_normal.pkl"),
        "ngboost_spread": lambda: NGBoostModel.load(models_dir / "ngboost_normal_wx_spread.pkl"),
        "drn":            lambda: DRNModel.load(models_dir / "drn_normal"),
        "drn_spread":     lambda: DRNModel.load(models_dir / "drn_normal_wx_spread"),
    }

    model_names = getattr(config, "ensemble_models", None) or ["emos", "ngboost"]
    config_weights = getattr(config, "ensemble_weights", None) or {}

    models, names, weights = [], [], []
    for name in model_names:
        loader = model_loaders.get(name)
        if loader is None:
            raise ValueError(f"Unknown model: {name!r}")
        models.append(loader())
        names.append(name)
        weights.append(config_weights.get(name, 1.0))
        log.info(f"Loaded model {name!r} (weight={weights[-1]})")

    return models, names, weights


def build_extra_features(ecmwf, gfs, icon, city, date, wx):
    """Compute spread + wind features. Mirrors feature_actor._build_extra_features()."""
    features = {}

    if ecmwf is not None:
        features["ecmwf_high"] = ecmwf
    if gfs is not None:
        features["gfs_high"] = gfs
    if ecmwf is not None and gfs is not None:
        features["forecast_high"] = max(ecmwf, gfs)

    temps = [t for t in (ecmwf, gfs, icon) if t is not None]
    if len(temps) >= 2:
        arr = np.array(temps)
        features["model_std"] = float(np.std(arr, ddof=0))
        features["model_range"] = float(np.ptp(arr))

    if wx:
        features.update(wx)

    coast_normal = _COAST_NORMAL.get(city)
    wind_dir = (wx or {}).get("wind_dir_afternoon")
    if coast_normal is not None and wind_dir is not None:
        angle_diff = (wind_dir - coast_normal) * np.pi / 180
        features["wind_dir_offshore"] = float(np.cos(angle_diff))
    else:
        features["wind_dir_offshore"] = 0.0

    return features


def get_forecast_icon(city, date):
    """Fetch ICON forecast. Separate function for easy mocking."""
    try:
        return get_forecast(city, date, "icon_seamless")
    except Exception:
        return None


def compute_desired_ladder(
    bid_cents: int,
    config_offsets: tuple,
    config_size: int,
    max_cost_cents: int,
    thin_margin_threshold_f: float,
    thin_margin_size_factor: float,
    margin: float,
    capacity: int,
    budget: int,
) -> list[dict]:
    """Pure function: compute ladder orders. Extracted from weather_strategy._deploy_ladder()."""
    effective_size = config_size
    if thin_margin_threshold_f > 0 and margin < thin_margin_threshold_f:
        effective_size = max(1, int(config_size * thin_margin_size_factor))

    orders = []
    remaining_capacity = capacity
    remaining_budget = budget

    for offset in config_offsets:
        price_cents = max(1, bid_cents - offset)
        if price_cents > max_cost_cents:
            continue
        size = min(effective_size, max(0, remaining_capacity))
        if size <= 0:
            break
        order_cost = price_cents * size
        if order_cost > remaining_budget:
            break
        orders.append({
            "price_cents": price_cents,
            "qty": size,
            "reason": f"ladder offset={offset}",
        })
        remaining_capacity -= size
        remaining_budget -= order_cost

    return orders


def evaluate_cycle(db_conn, redis_client, models, model_names, model_weights, config, stream_key):
    """One evaluation cycle: score all markets, write to DB, publish to Redis."""
    cycle_id = str(uuid.uuid4())[:8]
    cycle_ts = datetime.now(timezone.utc).isoformat()

    # 1. Fetch markets
    try:
        markets = fetch_open_markets()
    except Exception as e:
        log.error(f"Failed to fetch markets: {e}")
        beat_heartbeat(db_conn, "evaluator", status="error", message=str(e))
        db_conn.commit()
        return

    for m in markets:
        upsert_market(db_conn, m["ticker"], **{k: v for k, v in m.items() if k != "ticker"})

    # 2. Group by (city, settlement_date)
    series_to_city = {s: c for s, c in SERIES_CONFIG}
    groups = {}  # (city, date) -> [market_dicts]
    for m in markets:
        parsed = parse_ticker(m["ticker"])
        if not parsed:
            continue
        city = m.get("city") or series_to_city.get(parsed["series"], "")
        sd = parsed["settlement_date"]
        if city:
            groups.setdefault((city, sd), []).append({**m, "city": city, "parsed": parsed})

    # 3. Score each group
    all_evals = []
    passing_signals = []

    for (city, sd), market_list in groups.items():
        # Fetch forecasts
        try:
            ecmwf = get_forecast(city, sd, PRIMARY_MODEL)
            gfs = get_forecast(city, sd, CONSENSUS_MODEL)
            icon = get_forecast_icon(city, sd)
            wx = get_weather_features(city, sd) if get_weather_features else {}
        except Exception as e:
            log.warning(f"Forecast fetch failed for {city}/{sd}: {e}")
            continue

        if ecmwf is None or gfs is None:
            continue

        # Build features
        features = build_extra_features(ecmwf, gfs, icon, city, sd, wx or {})

        # Store forecast
        upsert_forecast(db_conn, city, sd,
                        ecmwf=ecmwf, gfs=gfs, icon=icon,
                        model_std=features.get("model_std"),
                        model_range=features.get("model_range"))

        for m in market_list:
            parsed = m["parsed"]
            ticker = m["ticker"]
            threshold = float(parsed["threshold"])
            direction = m.get("direction", "above")

            # Score all sides (returns ModelScore for each of "no" and "yes")
            try:
                scores = score_opportunities(
                    ticker=ticker, city=city, direction=direction,
                    threshold=threshold, settlement_date=sd,
                    ecmwf=ecmwf, gfs=gfs,
                    models=models, model_names=model_names,
                    model_weights=model_weights,
                    now=datetime.now(timezone.utc),
                    extra_features=features,
                )
            except Exception as e:
                log.warning(f"score_opportunities failed for {ticker}: {e}")
                continue

            # Apply filters
            filtered = apply_entry_filters(scores, config)
            passing_keys = {(s.ticker, s.side) for s in filtered}

            for s in scores:
                key = (s.ticker, s.side)
                filter_result = "pass" if key in passing_keys else "filtered"
                filter_reason = "" if filter_result == "pass" else "below_threshold_or_disabled"

                all_evals.append({
                    "cycle_id": cycle_id,
                    "cycle_ts": cycle_ts,
                    "ticker": s.ticker,
                    "city": s.city,
                    "direction": s.direction,
                    "side": s.side,
                    "threshold": s.threshold,
                    "settlement_date": s.settlement_date,
                    "ecmwf": s.ecmwf,
                    "gfs": s.gfs,
                    "margin": s.margin,
                    "consensus": s.consensus,
                    "p_win": s.p_win,
                    "model_scores": json.dumps(s.model_scores),
                    "filter_result": filter_result,
                    "filter_reason": filter_reason,
                    "features": json.dumps(features),
                })

            # Collect passing signals
            for s in filtered:
                passing_signals.append({
                    "score": s,
                    "features": features,
                    "market": m,
                })

    # 4. Write evaluations to DB
    if all_evals:
        write_evaluations(db_conn, all_evals)

    # 5. Compute desired orders for passing signals and publish to Redis
    desired = []
    for sig_info in passing_signals:
        s = sig_info["score"]
        m = sig_info["market"]
        sig_features = sig_info["features"]

        # Bid price depends on side
        if s.side == "no":
            # NO cost = 100 - yes_bid (buy at the NO ask, which is 100 - yes_bid)
            bid_cents = 100 - m.get("yes_ask", 50)
        else:
            bid_cents = m.get("yes_bid", 50)

        ladder = compute_desired_ladder(
            bid_cents=bid_cents,
            config_offsets=getattr(config, "stable_ladder_offsets_cents", (0, 1, 3, 5, 10)),
            config_size=getattr(config, "stable_size", 3),
            max_cost_cents=getattr(config, "max_no_cost_cents", 92),
            thin_margin_threshold_f=getattr(config, "thin_margin_threshold_f", 2.0),
            thin_margin_size_factor=getattr(config, "thin_margin_size_factor", 0.5),
            margin=s.margin,
            capacity=getattr(config, "max_contracts_per_ticker", 20),
            budget=getattr(config, "max_total_deployed_cents", 4000),
        )

        for order in ladder:
            desired.append({
                "ticker": s.ticker,
                "side": s.side,
                **order,
            })

        # Publish ModelSignal to Redis
        if redis_client:
            signal = ModelSignal(
                city=s.city, ticker=s.ticker, side=s.side,
                p_win=s.p_win, model_scores=s.model_scores,
                features_snapshot=sig_features,
                ts_event=int(time.time_ns()), ts_init=int(time.time_ns()),
            )
            try:
                payload = msgpack.packb(ModelSignal.to_dict(signal), use_bin_type=True)
                redis_client.xadd(stream_key, {
                    b"type": b"ModelSignal",
                    b"topic": b"data.ModelSignal",
                    b"payload": payload,
                })
            except Exception as e:
                log.error(f"Redis publish failed: {e}")

    # 6. Write desired orders
    if desired:
        write_desired_orders(db_conn, cycle_id, desired)

    # 7. Heartbeat
    beat_heartbeat(db_conn, "evaluator", status="ok",
                   message=f"cycle={cycle_id} evals={len(all_evals)} signals={len(passing_signals)}")

    # Commit all writes atomically
    db_conn.commit()

    log.info(
        f"Cycle {cycle_id}: {len(all_evals)} evals, "
        f"{len(passing_signals)} signals, {len(desired)} desired orders"
    )


def main():
    parser = argparse.ArgumentParser(description="Standalone ML evaluator")
    parser.add_argument("--db", default="data/trading.db")
    parser.add_argument("--redis-host", default="localhost")
    parser.add_argument("--redis-port", type=int, default=6379)
    parser.add_argument("--stream-key", default="weather-signals")
    parser.add_argument("--interval", type=int, default=300, help="Seconds between cycles")
    parser.add_argument("--once", action="store_true", help="Run one cycle and exit")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    db_path = Path(args.db)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    init_db(db_path)
    conn = get_connection(db_path)

    r = redis.Redis(host=args.redis_host, port=args.redis_port)
    try:
        r.ping()
        log.info(f"Redis connected: {args.redis_host}:{args.redis_port}")
    except redis.ConnectionError:
        log.warning("Redis not available — signals will only be written to DB")
        r = None

    config = load_config()
    models, names, weights = load_models(config)

    log.info(f"Evaluator started. DB={args.db}, interval={args.interval}s")

    while True:
        try:
            config = load_config()  # hot reload
            evaluate_cycle(conn, r, models, names, weights, config, args.stream_key)
        except Exception as e:
            log.error(f"Cycle failed: {e}", exc_info=True)
            try:
                log_event(conn, "evaluator", "error", str(e))
                conn.commit()
            except Exception:
                pass
        if args.once:
            break
        time.sleep(args.interval)

    conn.close()


if __name__ == "__main__":
    main()
