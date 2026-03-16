"""Custom NT data types for signal server integration."""
from nautilus_trader.core.data import Data
from nautilus_trader.model.custom import customdataclass


@customdataclass
class SignalScore(Data):
    """Score for a single KXHIGH contract from the signal server ensemble."""

    ticker: str = ""
    city: str = ""
    threshold: float = 0.0
    direction: str = ""       # "above" | "below"
    no_p_win: float = 0.0     # ensemble NO probability
    yes_p_win: float = 0.0    # ensemble YES probability
    no_margin: float = 0.0    # forecast distance from threshold (positive = safe for NO)
    n_models: int = 0         # 1-3, quality indicator
    emos_no: float = 0.0      # EMOS model NO probability
    ngboost_no: float = 0.0   # NGBoost model NO probability
    drn_no: float = 0.0       # DRN model NO probability
    yes_bid: int = 0          # current market YES bid in cents
    yes_ask: int = 0          # current market YES ask in cents
    status: str = ""          # market status: "open", "unopened", "closed", "settled"
    nws_max: float = 0.0      # NWS forecast max temperature (0.0 = unavailable)


@customdataclass
class ForecastDrift(Data):
    """Alert when forecast shifts significantly for a city."""

    city: str = ""
    date: str = ""
    message: str = ""
