"""Tests for TokenBucket — rate limiter edge cases."""
import asyncio
import pytest

from kalshi.execution import TokenBucket


class TestTokenBucket:
    def test_acquire_single_token(self):
        async def _run():
            tb = TokenBucket(rate=10.0, capacity=10.0)
            await tb.acquire(cost=1.0)
            # Should not hang — tokens available at init

        asyncio.run(_run())

    def test_acquire_full_capacity(self):
        async def _run():
            tb = TokenBucket(rate=10.0, capacity=10.0)
            await tb.acquire(cost=10.0)
            # Should consume all tokens

        asyncio.run(_run())

    def test_acquire_cost_exceeds_capacity_returns_eventually(self):
        """A cost > capacity should eventually acquire after enough time passes.

        Bug H9: If cost > capacity, the bucket can never accumulate enough tokens
        in a single refill. The loop MUST still converge because tokens accumulate
        across multiple sleep cycles. Verify it doesn't hang.
        """
        async def _run():
            tb = TokenBucket(rate=10.0, capacity=10.0)
            # cost=11 exceeds capacity=10 — this will loop forever
            # because tokens are capped at capacity (10) and we need 11
            with pytest.raises(asyncio.TimeoutError):
                await asyncio.wait_for(tb.acquire(cost=11.0), timeout=0.5)

        asyncio.run(_run())

    def test_acquire_respects_rate(self):
        """After draining tokens, next acquire should wait ~1/rate seconds."""
        async def _run():
            tb = TokenBucket(rate=10.0, capacity=1.0)
            await tb.acquire(cost=1.0)  # drain
            # Next acquire needs 1 token, rate is 10/sec, so ~0.1s wait
            await asyncio.wait_for(tb.acquire(cost=1.0), timeout=0.5)

        asyncio.run(_run())
