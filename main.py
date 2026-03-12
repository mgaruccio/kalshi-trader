"""Weather TradingNode entry point.

Wires FeatureActor (climate data polling + ML signals) and
WeatherStrategy (entry/exit execution) into a NautilusTrader
live TradingNode with Kalshi data + execution clients.
"""
import argparse
import logging
import sys

from nautilus_trader.live.node import TradingNode
from nautilus_trader.live.config import TradingNodeConfig, LiveDataClientConfig, LiveExecClientConfig

from adapter import (
    KalshiDataClientFactory,
    KalshiExecutionClientFactory,
    KalshiInstrumentProvider,
    KalshiConfig,
)
from feature_actor import FeatureActor, FeatureActorConfig
from weather_strategy import WeatherStrategy, WeatherStrategyConfig

logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)


def main():
    parser = argparse.ArgumentParser(description="Weather TradingNode")
    parser.add_argument("--dry-run", action="store_true",
                        help="Paper trade: evaluate signals but block all orders (max_position=0)")
    args = parser.parse_args()

    try:
        config_obj = KalshiConfig()
    except Exception as e:
        log.error(f"Failed to load configuration: {e}")
        log.error("Please ensure KALSHI_API_KEY_ID and KALSHI_PRIVATE_KEY_PATH are set.")
        sys.exit(1)

    if not config_obj.api_key_id or not config_obj.private_key_path:
        log.error("Configuration Error: KALSHI_API_KEY_ID or KALSHI_PRIVATE_KEY_PATH is empty.")
        sys.exit(1)

    # 1. Load instruments
    provider = KalshiInstrumentProvider(config=config_obj)
    log.info("Loading instruments from Kalshi...")
    provider.load_all(filters={"series_ticker": "KXHIGH"})
    instruments = provider.list_all()

    if not instruments:
        log.error("No KXHIGH instruments found!")
        sys.exit(1)
    log.info(f"Loaded {len(instruments)} instruments")

    # 2. Setup TradingNode
    config = TradingNodeConfig(
        trader_id=f"KALSHI-WEATHER-{config_obj.environment.upper()}",
        data_clients={"KALSHI": LiveDataClientConfig()},
        exec_clients={} if args.dry_run else {"KALSHI": LiveExecClientConfig()},
    )
    node = TradingNode(config=config)
    node.add_data_client_factory("KALSHI", KalshiDataClientFactory)
    if not args.dry_run:
        node.add_exec_client_factory("KALSHI", KalshiExecutionClientFactory)
    node.build()

    # Register instruments
    for inst in instruments:
        node.cache.add_instrument(inst)

    # 3. Add FeatureActor (climate data -> signals)
    feature_actor = FeatureActor(FeatureActorConfig(
        live_mode=True,
        model_cycle_seconds=300,
    ))
    node.trader.add_actor(feature_actor)

    # 4. Add WeatherStrategy
    strategy_kwargs = dict(
        stable_min_p_win=0.95,
        max_cost_cents=92,
        sell_target_cents=97,
        stable_size=3,
        max_position_per_ticker=20,
    )
    if args.dry_run:
        strategy_kwargs["max_position_per_ticker"] = 0
        log.info("DRY RUN: max_position_per_ticker=0, no orders will be placed")

    weather_strategy = WeatherStrategy(WeatherStrategyConfig(**strategy_kwargs))
    weather_strategy.set_feature_actor(feature_actor)
    node.trader.add_strategy(weather_strategy)

    # 5. Run
    log.info("Starting Weather TradingNode. Press Ctrl+C to stop.")
    try:
        node.run()
    except KeyboardInterrupt:
        log.info("Stopping TradingNode...")
        node.stop()
    finally:
        node.dispose()


if __name__ == "__main__":
    main()
