"""Weather TradingNode entry point.

Wires FeatureActor (climate data polling + ML signals) and
WeatherStrategy (entry/exit execution) into a NautilusTrader
live TradingNode with Kalshi data + execution clients.
"""
import logging

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


def main():
    try:
        config_obj = KalshiConfig()
    except Exception as e:
        print(f"Failed to load configuration: {e}")
        print("Please ensure KALSHI_API_KEY_ID and KALSHI_PRIVATE_KEY_PATH are set.")
        return

    if not config_obj.api_key_id or not config_obj.private_key_path:
        print("Configuration Error: KALSHI_API_KEY_ID or KALSHI_PRIVATE_KEY_PATH is empty.")
        return

    # 1. Load instruments
    provider = KalshiInstrumentProvider(config=config_obj)
    print("Loading instruments from Kalshi...")
    provider.load_all(filters={"series_ticker": "KXHIGH"})
    instruments = provider.list_all()

    if not instruments:
        print("No KXHIGH instruments found!")
        return
    print(f"Loaded {len(instruments)} instruments")

    # 2. Setup TradingNode
    config = TradingNodeConfig(
        trader_id=f"KALSHI-WEATHER-{config_obj.environment.upper()}",
        data_clients={"KALSHI": LiveDataClientConfig()},
        exec_clients={"KALSHI": LiveExecClientConfig()},
    )
    node = TradingNode(config=config)
    node.add_data_client_factory("KALSHI", KalshiDataClientFactory)
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
    weather_strategy = WeatherStrategy(WeatherStrategyConfig(
        min_p_win=0.95,
        max_cost_cents=92,
        sell_target_cents=97,
        trade_size=1,
    ))
    weather_strategy.set_feature_actor(feature_actor)
    node.trader.add_strategy(weather_strategy)

    # 5. Run
    print("Starting Weather TradingNode. Press Ctrl+C to stop.")
    try:
        node.run()
    except KeyboardInterrupt:
        print("Stopping TradingNode...")
        node.stop()
    finally:
        node.dispose()


if __name__ == "__main__":
    main()
