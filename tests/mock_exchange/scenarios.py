"""Scenario definitions for mock exchange confidence tests.

Each Scenario describes:
  - Initial orderbook state (no_bid_cents, yes_bid_cents)
  - A sequence of timed BookUpdates (price script)
  - Expected outcomes (fill type, limit fill price, cancel count)

The NO ask price is always derived as: NO ask = 100 - max(YES bids).
A limit BUY NO order fills when the NO ask drops to or below the limit price.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class BookUpdate:
    """Single orderbook price update in a scenario's price script."""
    delay_ms: int       # milliseconds after the previous update (or scenario start)
    no_bid_cents: int   # new best NO bid in cents (1-99)
    yes_bid_cents: int  # new best YES bid in cents (1-99)
    no_bid_size: int = 100
    yes_bid_size: int = 100


@dataclass(frozen=True)
class Scenario:
    """End-to-end scenario for a single market."""
    name: str
    description: str
    initial_no_bid_cents: int
    initial_yes_bid_cents: int
    updates: tuple[BookUpdate, ...]
    expect_market_fill: bool = True
    expect_limit_fill: bool = False
    expect_limit_fill_price_cents: int | None = None
    expect_cancel_count: int = 0


# ---------------------------------------------------------------------------
# Scenario 1: Steady limit fill
# YES bid walks from 58c to 66c → NO ask drops from 42c to 34c → limit fills.
# ---------------------------------------------------------------------------
STEADY_FILL = Scenario(
    name="steady_fill",
    description=(
        "NO bid sits at 35c then eases to 34c as YES bid rises 58→60→63→66c. "
        "NO ask = 100 - YES bid drops 42→40→37→34c. "
        "Limit BUY NO at 34c fills when NO ask reaches 34c."
    ),
    initial_no_bid_cents=35,
    initial_yes_bid_cents=58,
    updates=(
        BookUpdate(delay_ms=200, no_bid_cents=35, yes_bid_cents=60),
        BookUpdate(delay_ms=200, no_bid_cents=35, yes_bid_cents=63),
        # NO bid drops to 34c so YES+NO=100c (no crossed book); NO ask still=34c.
        BookUpdate(delay_ms=200, no_bid_cents=34, yes_bid_cents=66),
    ),
    expect_market_fill=True,
    expect_limit_fill=True,
    expect_limit_fill_price_cents=34,
)

# ---------------------------------------------------------------------------
# Scenario 2: Chase up — strategy must cancel and repost as bid rises
# NO bid walks 30→35c; strategy places limit 1c below NO ask each step,
# then the YES bid jumps to 66c → fill.
# ---------------------------------------------------------------------------
CHASE_UP = Scenario(
    name="chase_up",
    description=(
        "NO bid walks 30→31→32→33→34→35c. Strategy chases by canceling and "
        "reposting limit below the NO ask each step (5 cancels). "
        "Then YES bid rises to 66c → NO ask=34c → fill."
    ),
    initial_no_bid_cents=30,
    initial_yes_bid_cents=58,
    updates=(
        BookUpdate(delay_ms=200, no_bid_cents=31, yes_bid_cents=58),
        BookUpdate(delay_ms=200, no_bid_cents=32, yes_bid_cents=58),
        BookUpdate(delay_ms=200, no_bid_cents=33, yes_bid_cents=58),
        BookUpdate(delay_ms=200, no_bid_cents=34, yes_bid_cents=58),
        BookUpdate(delay_ms=200, no_bid_cents=35, yes_bid_cents=58),
        BookUpdate(delay_ms=200, no_bid_cents=34, yes_bid_cents=66),
    ),
    expect_market_fill=True,
    expect_limit_fill=True,
    expect_limit_fill_price_cents=34,
    expect_cancel_count=5,
)

# ---------------------------------------------------------------------------
# Scenario 3: Market order fills immediately — no book changes needed
# ---------------------------------------------------------------------------
MARKET_ORDER_IMMEDIATE = Scenario(
    name="market_order_immediate",
    description=(
        "Initial NO bid=35c, YES bid=58c. No orderbook updates. "
        "A market BUY NO order fills immediately at the current NO ask (42c). "
        "No limit order is placed."
    ),
    initial_no_bid_cents=35,
    initial_yes_bid_cents=58,
    updates=(),
    expect_market_fill=True,
    expect_limit_fill=False,
)

# ---------------------------------------------------------------------------
# Scenario 4: Limit order never fills — NO ask stays above 40c throughout
# ---------------------------------------------------------------------------
NO_FILL_TIMEOUT = Scenario(
    name="no_fill_timeout",
    description=(
        "NO bid walks 35→36→37→38c; YES bid drifts 58→58→57→56c. "
        "NO ask = 100 - YES bid stays at 44→44→43→44c — never drops to limit (34c). "
        "Limit order is canceled 3 times (once per update after initial placement), "
        "no fill."
    ),
    initial_no_bid_cents=35,
    initial_yes_bid_cents=58,
    updates=(
        BookUpdate(delay_ms=200, no_bid_cents=36, yes_bid_cents=58),
        BookUpdate(delay_ms=200, no_bid_cents=37, yes_bid_cents=57),
        BookUpdate(delay_ms=200, no_bid_cents=38, yes_bid_cents=56),
    ),
    expect_market_fill=True,
    expect_limit_fill=False,
    expect_cancel_count=3,
)

ALL_SCENARIOS: list[Scenario] = [
    STEADY_FILL,
    CHASE_UP,
    MARKET_ORDER_IMMEDIATE,
    NO_FILL_TIMEOUT,
]
