"""Kalshi adapter factories.

Correction #18: Do NOT use lru_cache with credential dict keys.
Use module-level singleton variable instead.
Correction #26: Factories pass `name` — constructors must accept it.
"""
from nautilus_trader.live.factories import LiveDataClientFactory, LiveExecClientFactory

from kalshi.config import KalshiDataClientConfig, KalshiExecClientConfig
from kalshi.providers import KalshiInstrumentProvider

# Correction #18: Module-level singleton, not lru_cache
_SHARED_PROVIDER: KalshiInstrumentProvider | None = None


def get_kalshi_instrument_provider(
    api_key_id: str,
    private_key_path: str,
    rest_host: str,
) -> KalshiInstrumentProvider:
    """Singleton instrument provider shared between data and exec clients."""
    global _SHARED_PROVIDER
    if _SHARED_PROVIDER is None:
        _SHARED_PROVIDER = KalshiInstrumentProvider(
            api_key_id=api_key_id,
            private_key_path=private_key_path,
            rest_host=rest_host,
        )
    return _SHARED_PROVIDER


class KalshiLiveDataClientFactory(LiveDataClientFactory):
    @staticmethod
    def create(loop, name, config, msgbus, cache, clock):
        from kalshi.data import KalshiDataClient  # deferred to avoid circular import

        provider = get_kalshi_instrument_provider(
            config.api_key_id, config.private_key_path, config.rest_url,
        )
        return KalshiDataClient(
            loop=loop,
            name=name,  # Correction #26
            config=config,
            instrument_provider=provider,
            msgbus=msgbus,
            cache=cache,
            clock=clock,
        )


class KalshiLiveExecClientFactory(LiveExecClientFactory):
    @staticmethod
    def create(loop, name, config, msgbus, cache, clock):
        from kalshi.execution import KalshiExecutionClient  # deferred to avoid circular import

        provider = get_kalshi_instrument_provider(
            config.api_key_id, config.private_key_path, config.rest_url,
        )
        return KalshiExecutionClient(
            loop=loop,
            name=name,  # Correction #26
            config=config,
            instrument_provider=provider,
            msgbus=msgbus,
            cache=cache,
            clock=clock,
        )
