"""Backtest harness for WeatherMakerStrategy."""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from pathlib import Path

from nautilus_trader.backtest.engine import BacktestEngine, BacktestEngineConfig
from nautilus_trader.backtest.models import BestPriceFillModel
from nautilus_trader.model.currencies import USD
from nautilus_trader.model.data import DataType
from nautilus_trader.model.enums import AccountType, AssetClass, OmsType
from nautilus_trader.model.identifiers import ClientId, Symbol, TraderId
from nautilus_trader.model.instruments import BinaryOption
from nautilus_trader.model.objects import Currency, Money, Price, Quantity
from nautilus_trader.persistence.catalog import ParquetDataCatalog

from kalshi.common.constants import KALSHI_VENUE
from kalshi.signals import SignalScore
from kalshi.strategy import WeatherMakerConfig, WeatherMakerStrategy


def _to_binary_option(cat_inst) -> BinaryOption:
    """Convert a catalog instrument (CurrencyPair) to BinaryOption for backtesting.

    The collector stores instruments as CurrencyPair (NT default for unknown types).
    The backtest engine with a CASH account requires BinaryOption.
    """
    symbol_str = cat_inst.id.symbol.value  # e.g. "KXHIGHNY-26MAR15-T54-YES"
    side = "Yes" if symbol_str.endswith("-YES") else "No"

    return BinaryOption(
        instrument_id=cat_inst.id,
        raw_symbol=Symbol(symbol_str),
        asset_class=AssetClass.ALTERNATIVE,
        currency=Currency.from_str("USD"),
        price_precision=2,
        size_precision=0,
        price_increment=Price.from_str("0.01"),
        size_increment=Quantity.from_int(1),
        activation_ns=0,
        expiration_ns=0,
        ts_event=cat_inst.ts_event,
        ts_init=cat_inst.ts_init,
        maker_fee=Decimal("0"),
        taker_fee=Decimal("0"),
        outcome=side,
    )


def build_backtest_engine(
    starting_balance_usd: int = 10_000,
    trader_id: str = "BACKTESTER-001",
) -> BacktestEngine:
    """Build a BacktestEngine configured for Kalshi KXHIGH backtesting.

    Uses OmsType.HEDGING to match live adapter (YES/NO are separate instruments).
    Returns the engine ready for add_instrument(), add_data(), add_strategy().
    """
    config = BacktestEngineConfig(
        trader_id=TraderId(trader_id),
    )
    engine = BacktestEngine(config=config)

    engine.add_venue(
        venue=KALSHI_VENUE,
        oms_type=OmsType.HEDGING,   # Enhancement #11: match live adapter (not NETTING)
        account_type=AccountType.CASH,
        base_currency=USD,
        starting_balances=[Money(starting_balance_usd, USD)],
        fill_model=BestPriceFillModel(),  # Fill limit orders resting inside the spread
    )

    return engine


def load_catalog_data(
    engine: BacktestEngine,
    catalog_path: str | Path,
    instrument_ids: list[str] | None = None,
    start: datetime | None = None,
    end: datetime | None = None,
) -> None:
    """Load QuoteTick data from ParquetDataCatalog into the engine.

    Enhancement #19: start/end datetime params for predicate pushdown to
    avoid loading the full 23GB+ catalog.

    Args:
        engine: BacktestEngine to load data into.
        catalog_path: Path to the ParquetDataCatalog directory.
        instrument_ids: Optional list of instrument ID strings to filter. Loads all if None.
        start: Optional earliest timestamp to load (predicate pushdown).
        end: Optional latest timestamp to load (predicate pushdown).
    """
    catalog = ParquetDataCatalog(str(catalog_path))

    catalog_instruments = catalog.instruments()
    if instrument_ids:
        catalog_instruments = [i for i in catalog_instruments if str(i.id) in instrument_ids]

    # The collector saves instruments as CurrencyPair, but the backtest engine
    # with a CASH account requires BinaryOption. Re-create as BinaryOption.
    instruments = []
    for cat_inst in catalog_instruments:
        inst = _to_binary_option(cat_inst)
        engine.add_instrument(inst)
        instruments.append(inst)

    if not instruments:
        return

    # Always filter ticks to instruments we have definitions for.
    # Without this, ticks for unregistered instruments cause
    # "No matching engine found" RuntimeError in SimulatedExchange.
    quotes = catalog.quote_ticks(
        instrument_ids=[i.id for i in instruments],
        start=start,
        end=end,
    )
    if quotes:
        engine.add_data(quotes, sort=False)


def load_signal_data(
    engine: BacktestEngine,
    scores: list[SignalScore],
) -> None:
    """Load historical SignalScore data into the engine.

    Wraps each SignalScore in CustomData so the DataEngine recognizes it
    during replay (raw Data subclasses fall through the type dispatch).
    Call engine.sort_data() after loading both catalog and signal data.
    """
    if scores:
        from nautilus_trader.model.data import CustomData
        data_type = DataType(SignalScore)
        wrapped = [CustomData(data_type=data_type, data=s) for s in scores]
        engine.add_data(wrapped, client_id=ClientId("SIGNAL"), sort=False)


def run_full_backtest(
    catalog_path: str | Path,
    scores: list[SignalScore],
    strategy_config: WeatherMakerConfig | None = None,
    starting_balance_usd: int = 10_000,
    start: datetime | None = None,
    end: datetime | None = None,
    instrument_ids: list[str] | None = None,
) -> tuple[BacktestEngine, WeatherMakerStrategy]:
    """Orchestrate a complete backtest run.

    Builds the engine, loads catalog and signal data, sorts, adds the strategy,
    runs, and returns the engine and strategy for results extraction.

    Args:
        catalog_path: Path to the ParquetDataCatalog directory.
        scores: Historical SignalScore events (from fetch_backfill / parse).
        strategy_config: Optional WeatherMakerConfig; uses defaults if None.
        starting_balance_usd: Starting account balance in USD.
        start: Optional earliest timestamp for catalog predicate pushdown.
        end: Optional latest timestamp for catalog predicate pushdown.
        instrument_ids: Optional list of instrument ID strings to filter catalog.

    Returns:
        (engine, strategy) — engine after run(), strategy with diagnostic counters.
    """
    engine = build_backtest_engine(starting_balance_usd=starting_balance_usd)

    load_catalog_data(engine, catalog_path, instrument_ids=instrument_ids, start=start, end=end)
    load_signal_data(engine, scores)
    engine.sort_data()

    config = strategy_config or WeatherMakerConfig()
    strategy = WeatherMakerStrategy(config=config)
    engine.add_strategy(strategy)

    engine.run()

    return engine, strategy
