"""Integration tests against Kalshi demo API.

Requires valid demo credentials in .env:
  KALSHI_API_KEY_ID, KALSHI_PRIVATE_KEY_PATH

Run with: uv run pytest tests/test_integration.py --noconftest -v -s --timeout=30
"""
import os
import pytest

pytestmark = pytest.mark.skipif(
    not os.environ.get("KALSHI_API_KEY_ID"),
    reason="KALSHI_API_KEY_ID not set",
)


@pytest.fixture(scope="module")
def credentials():
    from dotenv import load_dotenv
    load_dotenv()
    return {
        "api_key_id": os.environ["KALSHI_API_KEY_ID"],
        "private_key_path": os.environ["KALSHI_PRIVATE_KEY_PATH"],
        "rest_host": os.environ.get(
            "KALSHI_REST_HOST",
            "https://demo-api.kalshi.co/trade-api/v2",
        ),
    }


class TestAuth:
    def test_fetch_balance(self, credentials):
        import kalshi_python
        import json
        from kalshi_python.api.portfolio_api import PortfolioApi

        config = kalshi_python.Configuration()
        config.host = credentials["rest_host"]
        client = kalshi_python.KalshiClient(config)
        client.set_kalshi_auth(
            key_id=credentials["api_key_id"],
            private_key_path=credentials["private_key_path"],
        )
        portfolio = PortfolioApi(client)
        resp = portfolio.get_balance_with_http_info()
        data = json.loads(resp.raw_data)
        assert "balance" in data
        assert isinstance(data["balance"], int)


class TestInstrumentProvider:
    def test_load_markets(self, credentials):
        import asyncio
        from nautilus_trader.model.instruments import BinaryOption
        from kalshi.providers import KalshiInstrumentProvider

        provider = KalshiInstrumentProvider(
            api_key_id=credentials["api_key_id"],
            private_key_path=credentials["private_key_path"],
            rest_host=credentials["rest_host"],
        )
        asyncio.run(provider.load_all_async())
        instruments = provider.list_all()
        assert len(instruments) > 0
        for inst in instruments[:5]:
            assert isinstance(inst, BinaryOption)
            sym = inst.id.symbol.value
            assert sym.endswith("-YES") or sym.endswith("-NO"), f"Bad symbol: {sym}"

    def test_instruments_have_valid_observation_dates(self, credentials):
        """Instruments with date-bearing tickers should have observation_date in info."""
        import asyncio
        import re
        from kalshi.providers import KalshiInstrumentProvider

        provider = KalshiInstrumentProvider(
            api_key_id=credentials["api_key_id"],
            private_key_path=credentials["private_key_path"],
            rest_host=credentials["rest_host"],
        )
        asyncio.run(provider.load_all_async())
        instruments = provider.list_all()
        date_pattern = re.compile(r"^\d{4}-\d{2}-\d{2}$")
        kxhigh_instruments = [
            i for i in instruments if "KXHIGH" in i.id.symbol.value
        ]
        for inst in kxhigh_instruments[:5]:
            obs_date = inst.info.get("observation_date")
            assert obs_date is not None, f"Missing observation_date for {inst.id}"
            assert date_pattern.match(obs_date), f"Bad date format: {obs_date}"

    def test_instruments_have_kalshi_ticker_in_info(self, credentials):
        import asyncio
        from kalshi.providers import KalshiInstrumentProvider

        provider = KalshiInstrumentProvider(
            api_key_id=credentials["api_key_id"],
            private_key_path=credentials["private_key_path"],
            rest_host=credentials["rest_host"],
        )
        asyncio.run(provider.load_all_async())
        instruments = provider.list_all()
        for inst in instruments[:10]:
            assert "kalshi_ticker" in inst.info, f"Missing kalshi_ticker for {inst.id}"
            ticker = inst.info["kalshi_ticker"]
            # Ticker should not have -YES or -NO suffix
            assert not ticker.endswith("-YES") and not ticker.endswith("-NO")
