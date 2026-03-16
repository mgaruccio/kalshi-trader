"""Kalshi sandbox execution client factory for dry-run mode.

Produces a vanilla SandboxExecutionClient with BestPriceFillModel (matching
backtest behavior). Dynamic instrument registration is handled by the vanilla
sandbox — its on_data() calls exchange.update_instrument(), which delegates to
add_instrument() for new instruments (SimulatedExchange Cython source).
"""

from __future__ import annotations

from nautilus_trader.adapters.sandbox.execution import SandboxExecutionClient
from nautilus_trader.adapters.sandbox.factory import SandboxLiveExecClientFactory
from nautilus_trader.backtest.models import BestPriceFillModel


class KalshiSandboxExecClientFactory(SandboxLiveExecClientFactory):
    """Factory that creates a SandboxExecutionClient with BestPriceFillModel."""

    @staticmethod
    def create(loop, name, config, portfolio, msgbus, cache, clock):
        client = SandboxExecutionClient(
            loop=loop,
            clock=clock,
            portfolio=portfolio,
            msgbus=msgbus,
            cache=cache,
            config=config,
        )
        client.exchange.set_fill_model(BestPriceFillModel())
        return client


# TradingNode injects `portfolio` kwarg only when
# factory.__name__ == "SandboxLiveExecClientFactory"
# (see nautilus_trader/live/node_builder.py line 246).
KalshiSandboxExecClientFactory.__name__ = "SandboxLiveExecClientFactory"
