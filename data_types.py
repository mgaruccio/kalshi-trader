"""CustomData types for the climate data pipeline.

Three NT Data subclasses that flow through the NautilusTrader message bus:
- ClimateEvent: raw climate observation from a data source
- ModelSignal: periodic ML ensemble output for entry decisions
- DangerAlert: reactive exit trigger from climate-specific rules

NT 1.224+ pattern: Cython Data base class requires _ts_event/_ts_init
stored as private attrs with @property accessors. No super().__init__().
"""
from nautilus_trader.core.data import Data


class ClimateEvent(Data):
    """Raw climate observation published by each data source.

    ts_event = when data BECAME AVAILABLE (point-in-time correctness).
    In backtest: replayed from parquet. In live: set to now when polled.
    """

    def __init__(
        self,
        source: str,
        city: str,
        features: dict[str, float],
        ts_event: int,
        ts_init: int,
    ):
        self.source = source
        self.city = city
        self.features = features
        self._ts_event = ts_event
        self._ts_init = ts_init

    @property
    def ts_event(self) -> int:
        return self._ts_event

    @property
    def ts_init(self) -> int:
        return self._ts_init

    def __repr__(self) -> str:
        return (
            f"ClimateEvent(source={self.source!r}, city={self.city!r}, "
            f"features={len(self.features)} keys)"
        )


class ModelSignal(Data):
    """Periodic ML ensemble output for entry decisions.

    Emitted by FeatureActor on each model cycle timer. Contains the
    ensemble probability, per-model scores, and full feature snapshot.
    """

    def __init__(
        self,
        city: str,
        ticker: str,
        side: str,
        p_win: float,
        model_scores: dict[str, float],
        features_snapshot: dict[str, float],
        ts_event: int,
        ts_init: int,
    ):
        self.city = city
        self.ticker = ticker
        self.side = side
        self.p_win = p_win
        self.model_scores = model_scores
        self.features_snapshot = features_snapshot
        self._ts_event = ts_event
        self._ts_init = ts_init

    @property
    def ts_event(self) -> int:
        return self._ts_event

    @property
    def ts_init(self) -> int:
        return self._ts_init

    def __repr__(self) -> str:
        return (
            f"ModelSignal(city={self.city!r}, ticker={self.ticker!r}, "
            f"p_win={self.p_win:.3f})"
        )


class DangerAlert(Data):
    """Reactive exit trigger from climate-specific rules.

    Emitted immediately when a city-specific climate rule fires.
    Not periodic -- fires on each relevant ClimateEvent ingestion.
    """

    def __init__(
        self,
        ticker: str,
        city: str,
        alert_level: str,
        rule_name: str,
        reason: str,
        features: dict[str, float],
        ts_event: int,
        ts_init: int,
    ):
        self.ticker = ticker
        self.city = city
        self.alert_level = alert_level
        self.rule_name = rule_name
        self.reason = reason
        self.features = features
        self._ts_event = ts_event
        self._ts_init = ts_init

    @property
    def ts_event(self) -> int:
        return self._ts_event

    @property
    def ts_init(self) -> int:
        return self._ts_init

    def __repr__(self) -> str:
        return (
            f"DangerAlert(ticker={self.ticker!r}, level={self.alert_level}, "
            f"rule={self.rule_name!r})"
        )
