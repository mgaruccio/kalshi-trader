"""Tests for KalshiSandboxExecClientFactory."""
import asyncio

import pytest

from nautilus_trader.adapters.sandbox.config import SandboxExecutionClientConfig
from nautilus_trader.backtest.models import BestPriceFillModel
from nautilus_trader.cache.cache import Cache
from nautilus_trader.common.component import LiveClock, MessageBus, TestClock
from nautilus_trader.model.identifiers import TraderId
from nautilus_trader.portfolio import Portfolio

from kalshi.sandbox import KalshiSandboxExecClientFactory


class TestKalshiSandboxFactory:
    def test_factory_creates_client_with_best_price_fill_model(self):
        """Factory creates a SandboxExecutionClient with BestPriceFillModel."""
        loop = asyncio.new_event_loop()
        try:
            trader_id = TraderId("TESTER-001")
            clock = LiveClock()
            msgbus = MessageBus(trader_id=trader_id, clock=clock)
            cache = Cache(database=None)
            portfolio = Portfolio(msgbus=msgbus, cache=cache, clock=clock)

            config = SandboxExecutionClientConfig(
                venue="KALSHI",
                oms_type="HEDGING",
                account_type="CASH",
                base_currency="USD",
                starting_balances=["20 USD"],
                use_position_ids=True,
            )
            client = KalshiSandboxExecClientFactory.create(
                loop=loop,
                name="KALSHI",
                config=config,
                portfolio=portfolio,
                msgbus=msgbus,
                cache=cache,
                clock=clock,
            )
            assert isinstance(client.exchange.fill_model, BestPriceFillModel)
        finally:
            loop.close()

    def test_factory_name_matches_sandbox_for_portfolio_injection(self):
        """Factory __name__ must be 'SandboxLiveExecClientFactory' for TradingNode."""
        assert KalshiSandboxExecClientFactory.__name__ == "SandboxLiveExecClientFactory"
