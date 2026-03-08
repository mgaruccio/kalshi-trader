"""Backtest runner for weather climate strategy.

Loads climate events from parquet (exported by kalshi-weather) and
quote tick data, configures a NautilusTrader BacktestEngine, and
runs the WeatherStrategy with FeatureActor.
"""
import json
import logging
from pathlib import Path

import pyarrow.parquet as pq

from nautilus_trader.backtest.engine import BacktestEngine, BacktestEngineConfig
from nautilus_trader.backtest.models import FillModel
from nautilus_trader.model.currencies import USD
from nautilus_trader.model.enums import AccountType, OmsType
from nautilus_trader.model.objects import Money

from adapter import KALSHI_VENUE
from data_types import ClimateEvent

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
    instruments: list | None = None,
    quote_ticks: list | None = None,
    starting_balance_usd: int = 10_000,
) -> BacktestEngine:
    """Run a complete backtest.

    Args:
        climate_events_path: Path to climate_events.parquet
        instruments: List of NT Instrument objects for Kalshi contracts
        quote_ticks: List of QuoteTick objects (optional, can be empty for smoke test)
        starting_balance_usd: Starting balance in USD

    Returns:
        The BacktestEngine after running (for result inspection).
    """
    # Load climate events
    climate_events = load_climate_events(climate_events_path)
    log.info(f"Loaded {len(climate_events)} climate events")

    # Create engine
    engine = create_backtest_engine(starting_balance_usd)

    # Add instruments
    if instruments:
        for inst in instruments:
            engine.add_instrument(inst)

    # Add data
    if climate_events:
        engine.add_data(climate_events)

    if quote_ticks:
        engine.add_data(quote_ticks)

    # Import strategy and actor (may not be available yet -- Phase 2 parallel)
    try:
        from feature_actor import FeatureActor, FeatureActorConfig
        from weather_strategy import WeatherStrategy, WeatherStrategyConfig

        feature_actor = FeatureActor(FeatureActorConfig())
        weather_strategy = WeatherStrategy(WeatherStrategyConfig())
        weather_strategy.set_feature_actor(feature_actor)

        engine.add_actor(feature_actor)
        engine.add_strategy(weather_strategy)
    except ImportError:
        log.warning("feature_actor/weather_strategy not yet available -- running data-only backtest")

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
        "--balance",
        type=int,
        default=10_000,
        help="Starting balance in USD",
    )
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO)
    engine = run_backtest(args.climate_events, starting_balance_usd=args.balance)
