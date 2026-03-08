import asyncio
import logging

from nautilus_trader.live.node import TradingNode
from nautilus_trader.live.config import TradingNodeConfig, LiveDataClientConfig, LiveExecClientConfig

from adapter import (
    KalshiDataClientFactory,
    KalshiExecutionClientFactory,
    KalshiInstrumentProvider,
    KalshiConfig,
)
from strategy import KalshiSpreadCaptureStrategy, KalshiSpreadCaptureConfig

logging.basicConfig(level=logging.INFO)


def main():
    try:
        # Load from environment or .env file automatically
        config_obj = KalshiConfig()
    except Exception as e:
        print(f"Failed to load configuration: {e}")
        print(
            "Please ensure KALSHI_API_KEY_ID and KALSHI_PRIVATE_KEY_PATH are set in your environment or .env file."
        )
        return

    if not config_obj.api_key_id or not config_obj.private_key_path:
        print(
            "Configuration Error: KALSHI_API_KEY_ID or KALSHI_PRIVATE_KEY_PATH is empty."
        )
        print("Please configure these in your .env file or environment variables.")
        return

    # 1. Provide Instruments
    provider = KalshiInstrumentProvider(config=config_obj)
    print(f"Loading instruments from Kalshi {config_obj.environment}...")
    provider.load_all()
    instruments = provider.list_all()

    if not instruments:
        print("No instruments found!")
        return

    # Pick the first YES instrument for our demo
    demo_instrument = next(
        (inst for inst in instruments if inst.id.value.endswith("-YES")), instruments[0]
    )
    print(f"Using instrument for demo: {demo_instrument.id}")

    # 2. Setup the Trading Node
    config = TradingNodeConfig(
        trader_id=f"KALSHI-{config_obj.environment.upper()}-TRADER",
        data_clients={
            "KALSHI": LiveDataClientConfig()
        },
        exec_clients={
            "KALSHI": LiveExecClientConfig()
        },
    )
    node = TradingNode(config=config)

    # 3. Build and add clients
    node.add_data_client_factory("KALSHI", KalshiDataClientFactory)
    node.add_exec_client_factory("KALSHI", KalshiExecutionClientFactory)
    node.build()

    # Register instruments with the node's portfolio/cache
    for inst in instruments:
        node.cache.add_instrument(inst)

    # 4. Add the Strategy
    strategy_config = KalshiSpreadCaptureConfig(
        instrument_id=str(demo_instrument.id),
        trade_size=1,
        target_spread_cents=0,
    )
    strategy = KalshiSpreadCaptureStrategy(strategy_config)
    node.trader.add_strategy(strategy)

    # 5. Run the node
    print("Starting TradingNode. Press Ctrl+C to stop.")
    try:
        loop = node.get_event_loop()

        async def stop_after_delay():
            await asyncio.sleep(30)
            print("Stopping after 30 seconds demo...")
            node.stop()

        if loop:
            loop.create_task(stop_after_delay())
        node.run()
    except KeyboardInterrupt:
        print("Stopping TradingNode...")
        node.stop()
    finally:
        node.dispose()


if __name__ == "__main__":
    main()
