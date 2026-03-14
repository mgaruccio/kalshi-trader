import os
import time
import datetime
from nautilus_trader.live.node import TradingNode
from nautilus_trader.live.config import TradingNodeConfig
from nautilus_trader.persistence.config import StreamingConfig
from nautilus_trader.trading.strategy import Strategy, StrategyConfig
from dotenv import load_dotenv

import kalshi.factories
from kalshi import (
    KalshiLiveDataClientFactory,
    KalshiInstrumentProvider,
    KalshiDataClientConfig,
)

load_dotenv()

class KalshiDiscoveryConfig(StrategyConfig):
    pass

class KalshiDiscoveryStrategy(Strategy):
    """
    A simple strategy purely designed to discover new Kalshi markets
    and subscribe to their market data streams so the StreamingConfig
    can automatically persist them to Parquet.
    """
    def __init__(self, config: KalshiDiscoveryConfig, provider: KalshiInstrumentProvider):
        super().__init__(config)
        self.provider = provider
        self.subscribed_ids = set()
        self._discovery_consecutive_failures = 0

    def on_start(self):
        self.log.info("Starting KalshiDiscoveryStrategy")
        self._tick_count = 0
        self._zero_tick_streak = 0
        # Capture event loop reference — timer callbacks can't find it via get_running_loop()
        import asyncio
        self._loop = asyncio.get_running_loop()
        # Trigger an immediate discovery, then start a timer to poll every 10 minutes
        self.discover_markets()
        self.clock.set_timer("discovery_timer", interval=datetime.timedelta(seconds=600), callback=self.discover_markets)
        self.clock.set_timer("heartbeat", interval=datetime.timedelta(seconds=60), callback=self._heartbeat)

    def on_quote_tick(self, tick):
        self._tick_count += 1

    def _heartbeat(self, event=None):
        count = self._tick_count
        self._tick_count = 0
        self.log.info(f"HEARTBEAT: {count} ticks/60s, {len(self.subscribed_ids)} instruments")
        if count == 0:
            self._zero_tick_streak += 1
            if self._zero_tick_streak >= 5:
                self.log.error("NO TICKS FOR 5 MINUTES — possible connection issue")
        else:
            self._zero_tick_streak = 0

    def on_timer(self, timer_id: str):
        if timer_id == "discovery_timer":
            self.discover_markets()

    def discover_markets(self, event=None):
        self.log.info(f"[{time.strftime('%X')}] Starting background market discovery...")

        # Use stored loop reference — timer callbacks don't have asyncio context
        future = self._loop.run_in_executor(None, self._sync_discover_markets, self._loop)
        future.add_done_callback(self._on_discovery_done)

    def _on_discovery_done(self, future):
        """Log exceptions from background discovery thread."""
        exc = future.exception()
        if exc is not None:
            self.log.error(f"Background discovery thread failed: {exc}")

    def _sync_discover_markets(self, loop):
        import time
        import asyncio
        from kalshi_python.exceptions import ApiException
        from kalshi_python.api.series_api import SeriesApi
        from kalshi_python.api.markets_api import MarketsApi

        series_api = SeriesApi(self.provider.client)
        markets_api = MarketsApi(self.provider.client)

        try:
            # 1. Fetch all series and filter dynamically for High Temp weather markets
            self.log.info("Fetching series definitions...")
            while True:
                try:
                    res = series_api.get_series()
                    series_list = getattr(res, "series", []) or []

                    weather_series = []
                    for s in series_list:
                        category = getattr(s, "category", "") or ""
                        title = getattr(s, "title", "") or ""
                        ticker = getattr(s, "ticker", "") or ""

                        if category == "Climate and Weather" and "high" in title.lower():
                            weather_series.append(ticker)
                    break
                except ApiException as e:
                    if e.status == 429:
                        time.sleep(2)
                    else:
                        raise e

            self.log.info(f"Found {len(weather_series)} high temp series. Resolving markets...")

            new_instruments = []

            # 2. Fetch open + unopened markets for each series
            # "unopened" captures tomorrow's contracts before they open,
            # so we're subscribed and collecting from the first tick.
            for s_ticker in weather_series:
                for status_filter in ("open", "unopened"):
                    cursor = None
                    while True:
                        try:
                            res = markets_api.get_markets(limit=200, status=status_filter, series_ticker=s_ticker, cursor=cursor)
                            markets_found = getattr(res, "markets", None) or []

                            for m in markets_found:
                                yes_inst = self.provider._build_instrument(m, "YES")
                                no_inst = self.provider._build_instrument(m, "NO")

                                if not self.provider.find(yes_inst.id):
                                    self.provider.add(yes_inst)
                                    self.provider.add(no_inst)
                                    new_instruments.extend([yes_inst, no_inst])

                            cursor = getattr(res, "cursor", None)
                            if not cursor:
                                break

                            time.sleep(0.15)  # Rate limit per page
                        except ApiException as e:
                            if e.status == 429:
                                time.sleep(2)
                            else:
                                raise e

                    time.sleep(0.15)  # Rate limit between status filters
                time.sleep(0.15)  # Rate limit between series

            if new_instruments:
                self.log.info(f"Discovered {len(new_instruments)} new instruments. Scheduling subscriptions...")
                asyncio.run_coroutine_threadsafe(self._subscribe_all(new_instruments), loop)
            else:
                self.log.info("No new active high temp markets found.")

            self._discovery_consecutive_failures = 0

        except Exception as e:
            self._discovery_consecutive_failures += 1
            if self._discovery_consecutive_failures >= 3:
                self.log.error(
                    f"Market discovery failed {self._discovery_consecutive_failures} consecutive times — "
                    f"escalation needed: {e}"
                )
            else:
                self.log.error(f"Error during market discovery: {e}")

    async def _subscribe_all(self, new_instruments):
        for inst in new_instruments:
            if inst.id not in self.subscribed_ids:
                # Publish instrument to cache so StreamingFeatherWriter can persist ticks
                self.cache.add_instrument(inst)
                self.subscribe_quote_ticks(inst.id)
                self.subscribed_ids.add(inst.id)

def main():
    api_key_id = os.environ.get("KALSHI_API_KEY_ID")
    private_key_path = os.environ.get("KALSHI_PRIVATE_KEY_PATH")

    data_cfg = KalshiDataClientConfig(
        api_key_id=api_key_id,
        private_key_path=private_key_path,
        environment="production",
    )

    provider = KalshiInstrumentProvider(
        api_key_id=api_key_id,
        private_key_path=private_key_path,
        rest_host=data_cfg.rest_url,
        load_all=False,  # Strategy discovers markets itself
    )

    # Pre-seed the factory singleton so it reuses this provider instance
    kalshi.factories._SHARED_PROVIDER = provider

    config = TradingNodeConfig(
        trader_id="KALSHI-COLLECTOR",
        data_clients={
            "KALSHI": data_cfg,
        },
        streaming=StreamingConfig(
            catalog_path="kalshi_data_catalog",
        )
    )

    node = TradingNode(config=config)

    node.add_data_client_factory("KALSHI", KalshiLiveDataClientFactory)
    node.build()

    strategy = KalshiDiscoveryStrategy(KalshiDiscoveryConfig(), provider)
    node.trader.add_strategy(strategy)

    print("Starting Data Collection Node. Press Ctrl+C to stop.")
    try:
        node.run()
    except KeyboardInterrupt:
        print("Stopping TradingNode...")
        node.stop()
    finally:
        node.dispose()

if __name__ == "__main__":
    main()
