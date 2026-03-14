"""WeatherMakerStrategy — forecast-filtered passive market maker for KXHIGH contracts."""
from __future__ import annotations

from nautilus_trader.config import StrategyConfig

from kalshi.signals import SignalScore


class WeatherMakerConfig(StrategyConfig, frozen=True):
    """Configuration for the WeatherMakerStrategy."""

    # Filter
    confidence_threshold: float = 0.95
    min_models: int = 2
    max_model_spread: float = 0.15      # max spread between per-model probabilities

    # Quote ladder
    ladder_depth: int = 3
    ladder_spacing: int = 1             # cents between levels
    level_quantity: int = 10            # contracts per level
    reprice_threshold: int = 2          # cents — min bid movement to trigger reprice
    max_entry_cents: int = 96           # hard cap on anchor price

    # Risk
    market_cap_pct: float = 0.20        # max fraction of account per contract
    city_cap_pct: float = 0.33          # max fraction of account per city

    # Exit
    exit_price_cents: int = 97          # close position when bid reaches this

    # Time-of-day gating (ET)
    entry_phase_start_et: str = "10:30"
    entry_phase_end_et: str = "15:00"
    tomorrow_min_age_minutes: int = 30  # delay for tomorrow contract entry

    # Circuit breaker
    max_drawdown_pct: float = 0.15
    halt_file_path: str = "/tmp/kalshi-halt"


def should_quote(
    config: WeatherMakerConfig,
    score: SignalScore,
    drift_cities: set[str],
) -> tuple[str, bool]:
    """Decide if a contract should be quoted and on which side.

    Returns (side, passes) where side is "yes" or "no" and passes is True if
    all filter conditions are met.
    """
    # Determine winning side
    if score.no_p_win >= score.yes_p_win:
        side = "no"
        p_win = score.no_p_win
        anchor_cents = score.yes_bid  # NO buyer bids yes_bid price in practice
    else:
        side = "yes"
        p_win = score.yes_p_win
        anchor_cents = score.yes_bid

    # Must be open market
    if score.status and score.status != "open":
        return side, False

    # Check model count
    if score.n_models < config.min_models:
        return side, False

    # Check drift — pause only
    if score.city in drift_cities:
        return side, False

    # Check confidence threshold
    if p_win < config.confidence_threshold:
        return side, False

    # Check model agreement (spread among non-zero model probabilities)
    model_scores = [v for v in (score.emos_no, score.ngboost_no, score.drn_no) if v > 0.0]
    if len(model_scores) >= 2:
        spread = max(model_scores) - min(model_scores)
        if spread > config.max_model_spread:
            return side, False

    # Check hard cap on entry price
    if anchor_cents > config.max_entry_cents:
        return side, False

    return side, True


def check_risk_caps(
    market_exposure_cents: int,
    city_exposure_cents: int,
    quantity: int,
    price_cents: int,
    account_balance_cents: int,
    market_cap_pct: float,
    city_cap_pct: float,
) -> int:
    """Return max allowed quantity given risk caps.

    Caller derives market_exposure_cents and city_exposure_cents from
    cache.positions() and cache.orders_open() — no ExposureTracker needed.

    Returns a value between 0 and quantity (inclusive).
    """
    if account_balance_cents <= 0 or price_cents <= 0:
        return 0

    market_cap = int(account_balance_cents * market_cap_pct)
    city_cap = int(account_balance_cents * city_cap_pct)

    market_remaining = max(0, market_cap - market_exposure_cents)
    city_remaining = max(0, city_cap - city_exposure_cents)

    max_by_market = market_remaining // price_cents
    max_by_city = city_remaining // price_cents

    return min(quantity, max_by_market, max_by_city)


def compute_ladder(
    anchor_bid_cents: int,
    depth: int,
    spacing: int,
    qty: int,
) -> list[tuple[int, int]]:
    """Compute a descending quote ladder from the anchor bid.

    Returns up to `depth` (price_cents, quantity) tuples, each spaced
    `spacing` cents apart, starting at the anchor. Levels below 1c excluded.
    """
    levels: list[tuple[int, int]] = []
    for i in range(depth):
        price = anchor_bid_cents - (i * spacing)
        if price < 1:
            break
        levels.append((price, qty))
    return levels
