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

import numpy as np
import sqlite3

from datetime import timedelta

from nautilus_trader.common.actor import Actor, ActorConfig
from nautilus_trader.common.config import MessageBusConfig, DatabaseConfig
from nautilus_trader.live.node import TradingNode
from nautilus_trader.live.config import TradingNodeConfig
from nautilus_trader.model.data import DataType

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
from shared_features import build_extra_features

log = logging.getLogger(__name__)


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


def _get_filter_gate(score, config) -> str:
    """Return the name of the first filter gate that rejects this score, or 'pass'."""
    strategy_key = f"{score.direction}_{score.side}"
    enabled_key = f"{strategy_key}_enabled"

    if not getattr(config, enabled_key, True):
        return "strategy_disabled"
    if score.n_models_scored < getattr(config, "min_models_scored", 0):
        return "min_models"
    min_margin = getattr(config, "min_margin", None)
    if min_margin is not None and score.margin < min_margin:
        return "min_margin"
    if score.p_win < config.min_p_win:
        return "min_p_win"
    if getattr(config, "ensemble_require_unanimous", False):
        failing = [n for n, pw in score.model_scores.items() if pw < config.min_p_win]
        if failing:
            return "unanimous"
    return "pass"


def _check_rolling_features_staleness(max_stale_hours: float = 36.0) -> bool:
    """Return True if rolling features are fresh enough to trade on."""
    mart_path = Path(_KW_ROOT) / "data" / "rolling_features.json"
    if not mart_path.exists():
        log.error("Rolling features mart not found — cannot generate orders")
        return False
    try:
        mart = json.loads(mart_path.read_text())
        built_at = mart.get("built_at")
        if not built_at:
            log.error("Rolling features mart has no built_at — cannot verify freshness")
            return False
        built_ts = datetime.fromisoformat(built_at)
        age_hours = (datetime.now(timezone.utc) - built_ts).total_seconds() / 3600
        if age_hours > max_stale_hours:
            log.error(
                "Rolling features are %.0fh stale (limit %.0fh) — "
                "evaluations will run but NO orders will be generated. "
                "Run collect_daily_obs.py to refresh.",
                age_hours, max_stale_hours,
            )
            return False
        return True
    except Exception as e:
        log.error(f"Failed to check rolling features staleness: {e}")
        return False


def evaluate_cycle(db_conn, publish_signal, models, model_names, model_weights, config):
    """One evaluation cycle: score all markets, write to DB, publish to Redis."""
    cycle_id = str(uuid.uuid4())[:8]
    cycle_ts = datetime.now(timezone.utc).isoformat()

    # Gate: refuse to generate orders if rolling features are stale
    features_fresh = _check_rolling_features_staleness()

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
            try:
                icon = get_forecast(city, sd, "icon_seamless")
            except Exception:
                icon = None
            wx = get_weather_features(city, sd)
        except Exception as e:
            log.warning(f"Forecast fetch failed for {city}/{sd}: {e}")
            continue

        if ecmwf is None or gfs is None:
            continue

        # Build features
        features = build_extra_features(ecmwf, gfs, icon, city, wx or {})

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
                if filter_result == "pass":
                    filter_reason = ""
                else:
                    gate = _get_filter_gate(s, config)
                    if gate == "min_p_win":
                        filter_reason = f"pw={s.p_win:.3f} < {config.min_p_win:.3f}"
                    elif gate == "min_margin":
                        filter_reason = f"margin={s.margin:.1f}F < {config.min_margin:.1f}F"
                    elif gate == "min_models":
                        filter_reason = f"n_models={s.n_models_scored} < {getattr(config, 'min_models_scored', 0)}"
                    elif gate == "unanimous":
                        filter_reason = "ensemble_not_unanimous"
                    elif gate == "strategy_disabled":
                        filter_reason = f"{s.direction}_{s.side}_disabled"
                    else:
                        filter_reason = gate

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
    #    GATED on features_fresh — stale rolling features = no orders, no signals.
    #    Evaluations are still written (step 4) for dashboard visibility.
    if not features_fresh:
        beat_heartbeat(db_conn, "evaluator", status="stale",
                      message=f"cycle={cycle_id} evals={len(all_evals)} STALE — no orders generated")
        log_event(db_conn, "evaluator", "error", "Rolling features stale — orders suppressed")
        db_conn.commit()
        log.warning(f"Cycle {cycle_id}: {len(all_evals)} evals, 0 orders (STALE features)")
        return

    #    Sort cheapest-first (matches _on_refresh global rebalance logic)
    #    and track global budget across all tickers.
    max_budget = getattr(config, "max_total_deployed_cents", 4000)
    max_cost = getattr(config, "max_no_cost_cents", 92)

    # Subtract capital already deployed in positions
    positions = get_positions(db_conn)
    deployed = sum(p.get("contracts", 0) * max_cost for p in positions)
    remaining_budget = max_budget - deployed

    # Sort by bid price ascending (cheapest first)
    for sig_info in passing_signals:
        s = sig_info["score"]
        m = sig_info["market"]
        if s.side == "no":
            sig_info["bid_cents"] = 100 - m.get("yes_ask", 50)
        else:
            sig_info["bid_cents"] = m.get("yes_bid", 50)
    passing_signals.sort(key=lambda x: x["bid_cents"])

    # Build position lookup for per-ticker capacity
    position_by_ticker = {p["ticker"]: p.get("contracts", 0) for p in positions}
    max_per_ticker = getattr(config, "max_contracts_per_ticker", 20)

    desired = []
    for sig_info in passing_signals:
        s = sig_info["score"]
        m = sig_info["market"]
        sig_features = sig_info["features"]
        bid_cents = sig_info["bid_cents"]

        if remaining_budget <= 0:
            break

        held = position_by_ticker.get(s.ticker, 0)
        capacity = max(0, max_per_ticker - held)
        if capacity <= 0:
            continue

        ladder = compute_desired_ladder(
            bid_cents=bid_cents,
            config_offsets=getattr(config, "stable_ladder_offsets_cents", (0, 1, 3, 5, 10)),
            config_size=getattr(config, "stable_size", 3),
            max_cost_cents=getattr(config, "max_no_cost_cents", 92),
            thin_margin_threshold_f=getattr(config, "thin_margin_threshold_f", 2.0),
            thin_margin_size_factor=getattr(config, "thin_margin_size_factor", 0.5),
            margin=s.margin,
            capacity=capacity,
            budget=remaining_budget,
        )

        ladder_cost = sum(o["price_cents"] * o["qty"] for o in ladder)
        remaining_budget -= ladder_cost

        for order in ladder:
            desired.append({
                "ticker": s.ticker,
                "side": s.side,
                **order,
            })

        # Publish ModelSignal via callback (NT Actor's publish_data in production)
        if publish_signal:
            now_ns = int(time.time_ns())
            signal = ModelSignal(
                city=s.city, ticker=s.ticker, side=s.side,
                p_win=s.p_win, model_scores=s.model_scores,
                features_snapshot=sig_features,
                ts_event=now_ns, ts_init=now_ns,
            )
            try:
                publish_signal(signal)
            except Exception as e:
                log.error(f"Signal publish failed: {e}")

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


class EvaluatorActorConfig(ActorConfig):
    db_path: str = "data/trading.db"
    cycle_seconds: int = 300


class EvaluatorActor(Actor):
    """NT Actor that runs ML evaluation cycles and publishes ModelSignals.

    Runs inside a producer TradingNode. Publishes via self.publish_data()
    which NT routes to Redis streams automatically.
    """

    def __init__(self, config: EvaluatorActorConfig):
        super().__init__(config)
        self._cfg = config
        self._db_conn = None
        self._models = []
        self._model_names = []
        self._model_weights = []
        self._kw_config = None

    def on_start(self):
        # DB setup
        db_path = Path(self._cfg.db_path)
        db_path.parent.mkdir(parents=True, exist_ok=True)
        init_db(db_path)
        self._db_conn = get_connection(db_path)

        # Load ML models
        self._kw_config = load_config()
        self._models, self._model_names, self._model_weights = load_models(self._kw_config)

        # Start evaluation timer
        self.clock.set_timer(
            "eval_cycle",
            interval=timedelta(seconds=self._cfg.cycle_seconds),
            callback=self._on_eval_timer,
        )
        self.log.info(f"EvaluatorActor started. DB={self._cfg.db_path}, interval={self._cfg.cycle_seconds}s")

        # Run first cycle immediately
        self._run_cycle()

    def _on_eval_timer(self, event=None):
        self._run_cycle()

    def _run_cycle(self):
        try:
            self._kw_config = load_config()  # hot reload
            evaluate_cycle(
                db_conn=self._db_conn,
                publish_signal=self._publish_signal,
                models=self._models,
                model_names=self._model_names,
                model_weights=self._model_weights,
                config=self._kw_config,
            )
        except sqlite3.DatabaseError as e:
            self.log.error(f"DB error, reconnecting: {e}")
            try:
                self._db_conn.close()
            except Exception:
                pass
            db_path = Path(self._cfg.db_path)
            self._db_conn = get_connection(db_path)
        except Exception as e:
            self.log.error(f"Cycle failed: {e}")
            try:
                log_event(self._db_conn, "evaluator", "error", str(e))
                self._db_conn.commit()
            except Exception:
                pass

    def _publish_signal(self, signal: ModelSignal):
        self.publish_data(DataType(ModelSignal), signal)


def main():
    parser = argparse.ArgumentParser(description="Evaluator TradingNode (producer)")
    parser.add_argument("--db", default="data/trading.db")
    parser.add_argument("--redis-host", default="localhost")
    parser.add_argument("--redis-port", type=int, default=6379)
    parser.add_argument("--stream-key", default="weather-signals")
    parser.add_argument("--interval", type=int, default=300, help="Seconds between cycles")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    config = TradingNodeConfig(
        trader_id="EVALUATOR-001",
        message_bus=MessageBusConfig(
            database=DatabaseConfig(
                type="redis",
                host=args.redis_host,
                port=args.redis_port,
            ),
            use_trader_id=False,
            use_trader_prefix=False,
            use_instance_id=False,
            streams_prefix=args.stream_key,
            stream_per_topic=False,
            encoding="msgpack",
        ),
    )

    node = TradingNode(config=config)
    node.build()

    actor = EvaluatorActor(EvaluatorActorConfig(
        db_path=args.db,
        cycle_seconds=args.interval,
    ))
    node.trader.add_actor(actor)

    log.info("Starting Evaluator TradingNode. Press Ctrl+C to stop.")
    try:
        node.run()
    except KeyboardInterrupt:
        log.info("Stopping...")
        node.stop()
    finally:
        node.dispose()


if __name__ == "__main__":
    main()
