"""Backtest runner for weather climate strategy.

Uses BacktestNode (high-level API) which streams data from a
ParquetDataCatalog on disk — no need to load all ticks into memory.

Prerequisite: streaming session data must be converted to catalog format first.
Use `convert_sessions()` or the CLI `--convert` flag.
"""
import json
import logging
from pathlib import Path
from typing import Optional

import pyarrow.parquet as pq

from nautilus_trader.backtest.engine import BacktestEngine, BacktestEngineConfig
from nautilus_trader.backtest.node import BacktestNode
from nautilus_trader.common.config import LoggingConfig
from nautilus_trader.config import (
    BacktestDataConfig,
    BacktestRunConfig,
    BacktestVenueConfig,
    ImportableActorConfig,
    ImportableStrategyConfig,
)
from nautilus_trader.model.data import CustomData, DataType, QuoteTick
from nautilus_trader.model.identifiers import ClientId
from nautilus_trader.model.instruments import CurrencyPair
from nautilus_trader.persistence.catalog import ParquetDataCatalog

from adapter import KALSHI_VENUE
from data_types import ClimateEvent

CLIMATE_CLIENT = ClientId("CLIMATE")
SIGNAL_CLIENT = ClientId("SIGNAL")

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Session conversion: streaming feather → catalog parquet (run once per session)
# ---------------------------------------------------------------------------

def convert_sessions(catalog_path: Path, session_ids: list[str] | None = None) -> None:
    """Convert streaming feather sessions to catalog parquet format.

    This must be run once per session before backtesting. The catalog
    deduplicates instruments and merges tick data across sessions.
    """
    catalog = ParquetDataCatalog(str(catalog_path))
    live_dir = catalog_path / "live"
    if not live_dir.exists():
        log.warning(f"No live dir at {live_dir}")
        return

    sessions = sorted(d for d in live_dir.iterdir() if d.is_dir())
    if session_ids:
        sessions = [
            s for s in sessions
            if any(s.name.startswith(sid) for sid in session_ids)
        ]

    for session_dir in sessions:
        sid = session_dir.name
        log.info(f"Converting session {sid[:8]}...")
        try:
            catalog.convert_stream_to_data(
                instance_id=sid,
                data_cls=CurrencyPair,
                subdirectory="live",
            )
        except Exception as e:
            log.warning(f"CurrencyPair conversion for {sid[:8]}: {e}")

        try:
            catalog.convert_stream_to_data(
                instance_id=sid,
                data_cls=QuoteTick,
                subdirectory="live",
            )
        except Exception as e:
            log.warning(f"QuoteTick conversion for {sid[:8]}: {e}")

    instruments = catalog.instruments()
    log.info(f"Catalog ready: {len(instruments)} instruments")


# ---------------------------------------------------------------------------
# Climate events loader (stays in-memory — these are small, ~60K rows)
# ---------------------------------------------------------------------------

def load_climate_events(parquet_path: Path) -> list[ClimateEvent]:
    """Load climate events from parquet exported by kalshi-weather."""
    if not parquet_path.exists():
        log.warning(f"No climate events at {parquet_path}")
        return []

    table = pq.read_table(str(parquet_path))
    df = table.to_pandas()

    events = []
    for _, row in df.iterrows():
        features = json.loads(row["features"]) if isinstance(row["features"], str) else row["features"]
        events.append(ClimateEvent(
            source=str(row["source"]),
            city=str(row["city"]),
            features={k: float(v) for k, v in features.items()},
            ts_event=int(row["ts_event"]),
            ts_init=int(row["ts_init"]),
            date=str(row.get("date", "")),
        ))

    events.sort(key=lambda e: e.ts_event)
    return events


def load_model_signals(parquet_path: Path) -> list:
    """Load pre-computed ModelSignals from parquet."""
    from data_types import ModelSignal

    if not parquet_path.exists():
        log.warning(f"No model signals at {parquet_path}")
        return []

    table = pq.read_table(str(parquet_path))
    df = table.to_pandas()

    signals = []
    for _, row in df.iterrows():
        model_scores = json.loads(row["model_scores"]) if isinstance(row["model_scores"], str) else row["model_scores"]
        features = json.loads(row["features_snapshot"]) if isinstance(row["features_snapshot"], str) else row["features_snapshot"]
        signals.append(ModelSignal(
            city=str(row["city"]),
            ticker=str(row["ticker"]),
            side=str(row["side"]),
            p_win=float(row["p_win"]),
            model_scores={k: float(v) for k, v in model_scores.items()},
            features_snapshot={k: float(v) for k, v in features.items()} if features else {},
            ts_event=int(row["ts_event"]),
            ts_init=int(row["ts_init"]),
        ))

    signals.sort(key=lambda s: s.ts_event)
    log.info(f"Loaded {len(signals)} pre-computed model signals")
    return signals


# ---------------------------------------------------------------------------
# High-level backtest via BacktestNode (streams from catalog)
# ---------------------------------------------------------------------------

def run_backtest(
    climate_events_path: Path,
    catalog_path: Path,
    starting_balance_usd: int = 100,
    start: str | None = None,
    end: str | None = None,
    strategy_config: dict | None = None,
    actor_config: dict | None = None,
    model_signals_path: Path | None = None,
) -> BacktestEngine:
    """Run a backtest using BacktestNode streaming from ParquetDataCatalog.

    Data is streamed from disk — only a small working set is in memory at
    any time, so this scales to weeks/months of tick data.

    Args:
        climate_events_path: Path to climate_events.parquet
        catalog_path: Path to NT ParquetDataCatalog (run convert_sessions first)
        starting_balance_usd: Starting balance in USD
        start: Optional start time filter (ISO-8601)
        end: Optional end time filter (ISO-8601)

    Returns:
        The BacktestEngine after running.
    """
    catalog = ParquetDataCatalog(str(catalog_path))

    instruments = catalog.instruments()
    if not instruments:
        raise RuntimeError(f"No instruments in catalog at {catalog_path}. Run --convert first.")

    t_count = sum(1 for i in instruments if "-T" in str(i.id))
    log.info(f"Catalog has {len(instruments)} instruments ({t_count} T-type, {len(instruments) - t_count} B-bracket)")

    # NOTE: catalog.instruments(instrument_ids=...) doesn't filter correctly on
    # converted data, so we can't use per-instrument data configs. Load all ticks.
    # Speed optimization: remove B-bracket dirs from data/quote_tick/ if needed.
    data_configs = [
        BacktestDataConfig(
            catalog_path=str(catalog_path),
            data_cls=QuoteTick,
            start_time=start,
            end_time=end,
        ),
    ]

    # Climate events: load into memory (small) and add via low-level engine
    # after BacktestNode creates it. We use a callback pattern.
    climate_events = load_climate_events(climate_events_path)
    log.info(f"Loaded {len(climate_events)} climate events")

    venue_config = BacktestVenueConfig(
        name="KALSHI",
        oms_type="NETTING",
        account_type="MARGIN",
        base_currency="USD",
        starting_balances=[f"{starting_balance_usd} USD"],
    )

    # When using pre-computed signals, disable the model timer in FeatureActor
    # (it still receives ClimateEvents for exit rule checks, but no inference)
    effective_actor_config = dict(actor_config or {})
    if model_signals_path:
        effective_actor_config["model_cycle_seconds"] = 999_999  # effectively disabled
        effective_actor_config["scan_opportunities"] = False

    actor_configs = [
        ImportableActorConfig(
            actor_path="feature_actor:FeatureActor",
            config_path="feature_actor:FeatureActorConfig",
            config=effective_actor_config,
        ),
    ]

    strategy_configs = [
        ImportableStrategyConfig(
            strategy_path="weather_strategy:WeatherStrategy",
            config_path="weather_strategy:WeatherStrategyConfig",
            config=strategy_config or {},
        ),
    ]

    engine_config = BacktestEngineConfig(
        trader_id="WEATHER-BACKTEST-001",
        logging=LoggingConfig(bypass_logging=True),
        actors=actor_configs,
        strategies=strategy_configs,
    )

    run_config = BacktestRunConfig(
        engine=engine_config,
        venues=[venue_config],
        data=data_configs,
        chunk_size=500_000,  # Stream 500K ticks at a time — keeps memory bounded
        start=start,
        end=end,
        dispose_on_completion=False,  # Keep engine alive so caller can inspect results
    )

    node = BacktestNode(configs=[run_config])

    # build() creates the engine + actors + strategies from config
    node.build()

    engines = node.get_engines()
    if not engines:
        raise RuntimeError("BacktestNode created no engines — check config")
    engine = engines[0]

    # Inject climate events, filtered to the tick data window.
    # Climate events from before tick data would fire before instruments
    # are registered with the exchange, causing "No matching engine" errors.
    if climate_events:
        # Find earliest tick timestamp from catalog parquet metadata
        qt_dir = catalog_path / "data" / "quote_tick"
        first_ns = None
        if qt_dir.exists():
            import pyarrow.parquet as pq
            for inst_dir in sorted(qt_dir.iterdir()):
                if not inst_dir.is_dir():
                    continue
                for f in sorted(inst_dir.glob("*.parquet")):
                    try:
                        col = pq.read_table(str(f), columns=["ts_event"]).column("ts_event")
                        if len(col) > 0:
                            first_ns = int(col[0].as_py())
                            break
                    except Exception:
                        continue
                if first_ns is not None:
                    break

        if first_ns is not None:
            # Climate events must NOT precede tick data — otherwise they trigger
            # model inference before instruments are registered with the exchange.
            # Use first tick timestamp as the hard cutoff (no buffer before).
            cutoff_ns = first_ns
            original = len(climate_events)
            climate_events = [e for e in climate_events if e.ts_event >= cutoff_ns]
            log.info(f"Filtered climate events: {original} -> {len(climate_events)} (at or after first tick)")
        else:
            log.warning("Could not determine first tick timestamp — using all climate events")

        wrapped = [CustomData(DataType(ClimateEvent), e) for e in climate_events]
        engine.add_data(wrapped, client_id=CLIMATE_CLIENT)
        log.info(f"Injected {len(climate_events)} climate events")

    # Inject pre-computed model signals (bypasses FeatureActor inference)
    if model_signals_path:
        from data_types import ModelSignal
        signals = load_model_signals(model_signals_path)
        if signals:
            wrapped = [CustomData(DataType(ModelSignal), s) for s in signals]
            engine.add_data(wrapped, client_id=SIGNAL_CLIENT)
            log.info(f"Injected {len(signals)} pre-computed model signals")

    # Wire FeatureActor → WeatherStrategy
    actors = engine.trader.actors()
    strategies = engine.trader.strategies()
    if actors and strategies:
        strategies[0].set_feature_actor(actors[0])
        log.info("Wired FeatureActor → WeatherStrategy")

    # Run (streaming — processes chunk_size ticks at a time)
    results = node.run()
    log.info(f"Backtest complete: {results}")

    return engine


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Run weather strategy backtest")
    parser.add_argument(
        "--climate-events",
        type=Path,
        default=Path("data/climate_events.parquet"),
        help="Path to climate events parquet file",
    )
    parser.add_argument(
        "--catalog",
        type=Path,
        default=Path("kalshi_data_catalog"),
        help="Path to Nautilus ParquetDataCatalog",
    )
    parser.add_argument(
        "--balance",
        type=int,
        default=100,
        help="Starting balance in USD",
    )
    parser.add_argument(
        "--start",
        type=str,
        default=None,
        help="Start time filter (ISO-8601)",
    )
    parser.add_argument(
        "--end",
        type=str,
        default=None,
        help="End time filter (ISO-8601)",
    )
    parser.add_argument(
        "--model-signals",
        type=Path,
        default=None,
        help="Path to pre-computed model signals parquet (skips live inference)",
    )
    parser.add_argument(
        "--convert",
        action="store_true",
        help="Convert streaming sessions to catalog format before running",
    )
    parser.add_argument(
        "--convert-only",
        action="store_true",
        help="Only convert sessions, don't run backtest",
    )
    parser.add_argument(
        "--session",
        type=str,
        nargs="+",
        default=None,
        help="Session ID(s) to convert (default: all sessions)",
    )
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO)

    if args.convert or args.convert_only:
        convert_sessions(args.catalog, args.session)
        if args.convert_only:
            raise SystemExit(0)

    engine = run_backtest(
        args.climate_events,
        catalog_path=args.catalog,
        starting_balance_usd=args.balance,
        start=args.start,
        end=args.end,
        model_signals_path=args.model_signals,
    )
