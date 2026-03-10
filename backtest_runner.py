"""Backtest runner for weather climate strategy.

Loads climate events from parquet (exported by kalshi-weather) and
quote tick data, configures a NautilusTrader BacktestEngine, and
runs the WeatherStrategy with FeatureActor.
"""
import json
import logging
from pathlib import Path
from typing import Optional

import pyarrow.ipc as ipc
import pyarrow.parquet as pq

from nautilus_trader.backtest.engine import BacktestEngine, BacktestEngineConfig
from nautilus_trader.backtest.models import FillModel
from nautilus_trader.model.currencies import USD
from nautilus_trader.model.data import CustomData, DataType, QuoteTick
from nautilus_trader.model.enums import AccountType, OmsType
from nautilus_trader.model.identifiers import ClientId
from nautilus_trader.model.instruments import CurrencyPair
from nautilus_trader.model.objects import Money
from nautilus_trader.serialization.arrow.serializer import ArrowSerializer

from adapter import KALSHI_VENUE
from data_types import ClimateEvent

CLIMATE_CLIENT = ClientId("CLIMATE")

log = logging.getLogger(__name__)


def load_climate_events(parquet_path: Path) -> list[ClimateEvent]:
    """Load climate events from parquet exported by kalshi-weather.

    Expected parquet schema:
        source: str
        city: str
        date: str
        features: str (JSON-encoded dict)
        ts_event: int64 (unix nanos)
        ts_init: int64 (unix nanos)

    Returns list of ClimateEvent sorted by ts_event.
    """
    if not parquet_path.exists():
        log.warning(f"No climate events at {parquet_path}")
        return []

    table = pq.read_table(str(parquet_path))
    df = table.to_pandas()

    events = []
    for _, row in df.iterrows():
        # Parse features from JSON string
        features = json.loads(row["features"]) if isinstance(row["features"], str) else row["features"]

        events.append(ClimateEvent(
            source=str(row["source"]),
            city=str(row["city"]),
            features={k: float(v) for k, v in features.items()},
            ts_event=int(row["ts_event"]),
            ts_init=int(row["ts_init"]),
        ))

    # Sort by ts_event
    events.sort(key=lambda e: e.ts_event)
    return events


def _find_session_dir(catalog_path: Path) -> Path | None:
    """Find the session directory inside a streaming catalog.

    NT streaming writer creates: catalog_path/live/<session_id>/
    Returns the first session dir found, or None.
    """
    live_dir = catalog_path / "live"
    if not live_dir.exists():
        return None
    sessions = [d for d in live_dir.iterdir() if d.is_dir()]
    if not sessions:
        return None
    if len(sessions) > 1:
        log.warning(f"Multiple sessions found, using first: {sessions[0].name}")
    return sessions[0]


def _read_ipc_stream(path: Path):
    """Read an Arrow IPC stream file and return a pyarrow Table."""
    with open(path, "rb") as f:
        return ipc.open_stream(f).read_all()


def load_instruments_from_catalog(catalog_path: Path) -> list:
    """Load CurrencyPair instruments from an NT streaming catalog.

    Reads currency_pair_*.feather files (Arrow IPC stream format) and
    deserializes them via ArrowSerializer.
    """
    if not catalog_path.exists():
        log.warning(f"No catalog at {catalog_path}")
        return []

    session_dir = _find_session_dir(catalog_path)
    if session_dir is None:
        log.warning(f"No session directory in {catalog_path}")
        return []

    feather_files = sorted(session_dir.glob("currency_pair_*.feather"))
    if not feather_files:
        log.warning(f"No currency_pair feather files in {session_dir}")
        return []

    instruments = []
    skipped = 0
    for fp in feather_files:
        try:
            table = _read_ipc_stream(fp)
            instruments.extend(ArrowSerializer.deserialize(CurrencyPair, table))
        except Exception:
            skipped += 1
            log.warning(f"Failed to read {fp.name}", exc_info=True)

    if skipped > 0:
        log.warning(f"Skipped {skipped}/{len(feather_files)} instrument files")
    log.info(f"Loaded {len(instruments)} instruments from {session_dir.name}")
    return instruments


def load_quote_ticks_from_catalog(catalog_path: Path, instrument_ids: list | None = None) -> list[QuoteTick]:
    """Load QuoteTick data from an NT streaming catalog.

    Reads quote_tick/<instrument_id>/*.feather files (Arrow IPC stream format)
    and deserializes them via ArrowSerializer.
    """
    if not catalog_path.exists():
        log.warning(f"No catalog at {catalog_path}")
        return []

    session_dir = _find_session_dir(catalog_path)
    if session_dir is None:
        log.warning(f"No session directory in {catalog_path}")
        return []

    quote_tick_dir = session_dir / "quote_tick"
    if not quote_tick_dir.exists():
        log.warning(f"No quote_tick directory in {session_dir}")
        return []

    # Filter to requested instrument IDs if specified
    if instrument_ids is not None:
        id_strs = {str(iid) for iid in instrument_ids}
    else:
        id_strs = None

    ticks = []
    skipped = 0
    total_files = 0
    subdirs = sorted(d for d in quote_tick_dir.iterdir() if d.is_dir())
    for subdir in subdirs:
        if id_strs is not None and subdir.name not in id_strs:
            continue
        for fp in sorted(subdir.glob("*.feather")):
            total_files += 1
            try:
                table = _read_ipc_stream(fp)
                # ArrowSerializer returns pyo3 QuoteTick; convert to Cython
                # for BacktestEngine compatibility
                pyo3_ticks = ArrowSerializer.deserialize(QuoteTick, table)
                ticks.extend(QuoteTick.from_pyo3(t) for t in pyo3_ticks)
            except Exception:
                skipped += 1
                log.warning(f"Failed to read {fp.name}", exc_info=True)

    if skipped > 0:
        log.warning(f"Skipped {skipped}/{total_files} quote tick files")
    log.info(f"Loaded {len(ticks)} QuoteTicks from {len(subdirs)} instruments")
    ticks.sort(key=lambda t: t.ts_event)
    return ticks


def create_backtest_engine(
    starting_balance_usd: int = 10_000,
) -> BacktestEngine:
    """Create and configure a BacktestEngine for weather trading.

    Returns a configured engine ready for add_data / add_strategy / run.
    """
    config = BacktestEngineConfig(
        trader_id="WEATHER-BACKTEST-001",
    )
    engine = BacktestEngine(config=config)

    # Add Kalshi venue
    engine.add_venue(
        venue=KALSHI_VENUE,
        oms_type=OmsType.HEDGING,
        account_type=AccountType.MARGIN,
        base_currency=USD,
        starting_balances=[Money(starting_balance_usd, USD)],
        fill_model=FillModel(),
    )

    return engine


def run_backtest(
    climate_events_path: Path,
    catalog_path: Optional[Path] = None,
    instruments: list | None = None,
    quote_ticks: list | None = None,
    starting_balance_usd: int = 10_000,
) -> BacktestEngine:
    """Run a complete backtest.

    Args:
        climate_events_path: Path to climate_events.parquet
        catalog_path: Optional path to Nautilus ParquetDataCatalog (contains ticks + instruments)
        instruments: List of NT Instrument objects (optional if catalog provided)
        quote_ticks: List of QuoteTick objects (optional if catalog provided)
        starting_balance_usd: Starting balance in USD

    Returns:
        The BacktestEngine after running (for result inspection).
    """
    # Load climate events
    climate_events = load_climate_events(climate_events_path)
    log.info(f"Loaded {len(climate_events)} climate events")

    # Load from catalog if provided
    if catalog_path and catalog_path.exists():
        if instruments is None:
            instruments = load_instruments_from_catalog(catalog_path)
        if quote_ticks is None:
            # Only load ticks for the instruments we have
            ids = [inst.id for inst in instruments] if instruments else None
            quote_ticks = load_quote_ticks_from_catalog(catalog_path, ids)

    # Filter climate events to quote tick window (avoid processing years of
    # history when only a few hours of quotes exist)
    if climate_events and quote_ticks:
        first_qt_ns = quote_ticks[0].ts_event
        buffer_ns = 72 * 3600 * 1_000_000_000  # 72h buffer
        cutoff_ns = first_qt_ns - buffer_ns
        original = len(climate_events)
        climate_events = [e for e in climate_events if e.ts_event >= cutoff_ns]
        log.info(f"Filtered climate events: {original} -> {len(climate_events)} (72h before first quote tick)")

    # Create engine
    engine = create_backtest_engine(starting_balance_usd)

    # Add instruments
    if instruments:
        for inst in instruments:
            engine.add_instrument(inst)

    # Add data -- wrap ClimateEvents in CustomData for NT DataEngine routing
    if climate_events:
        wrapped = [CustomData(DataType(ClimateEvent), e) for e in climate_events]
        engine.add_data(wrapped, client_id=CLIMATE_CLIENT)

    if quote_ticks:
        engine.add_data(quote_ticks)

    # Import strategy and actor
    from feature_actor import FeatureActor, FeatureActorConfig
    from weather_strategy import WeatherStrategy, WeatherStrategyConfig

    feature_actor = FeatureActor(FeatureActorConfig())
    weather_strategy = WeatherStrategy(WeatherStrategyConfig())
    weather_strategy.set_feature_actor(feature_actor)

    engine.add_actor(feature_actor)
    engine.add_strategy(weather_strategy)

    # Run
    engine.run()
    log.info("Backtest complete")

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
        default=10_000,
        help="Starting balance in USD",
    )
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO)
    engine = run_backtest(
        args.climate_events,
        catalog_path=args.catalog,
        starting_balance_usd=args.balance,
    )
