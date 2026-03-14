"""Tests for kalshi/backtest_results.py — BacktestResults dataclass,
extract_results(), and format_report()."""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from kalshi.backtest_results import (
    BacktestResults,
    _city_from_instrument_id,
    _compute_contracts_per_city,
    _compute_max_drawdown_cents,
    extract_results,
    format_report,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_fill(
    order_side_str: str = "BUY",
    last_px: float = 0.85,
    last_qty: int = 10,
    instrument_id_str: str = "KXHIGHNY-26MAR15-T54-YES.KALSHI",
):
    """Create a mock fill object resembling NautilusTrader's OrderFilled."""
    from nautilus_trader.model.enums import OrderSide

    fill = MagicMock()
    fill.order_side = OrderSide.BUY if order_side_str == "BUY" else OrderSide.SELL
    fill.last_px = last_px
    fill.last_qty = last_qty
    fill.instrument_id = instrument_id_str
    return fill


def _make_order():
    return MagicMock()


def _make_position(realized_pnl_usd: float = 0.0):
    pos = MagicMock()
    pnl_mock = MagicMock()
    pnl_mock.as_double.return_value = realized_pnl_usd
    pos.realized_pnl = pnl_mock
    return pos


def _make_strategy(
    signals_received: int = 0,
    filter_passes: int = 0,
    filter_fails: int = 0,
    ladders_placed: int = 0,
    exits_attempted: int = 0,
    orders_submitted: int = 0,
):
    """Create a minimal strategy stub with diagnostic counters."""
    return SimpleNamespace(
        _signals_received=signals_received,
        _filter_passes=filter_passes,
        _filter_fails=filter_fails,
        _ladders_placed=ladders_placed,
        _exits_attempted=exits_attempted,
        _orders_submitted=orders_submitted,
    )


def _make_engine(
    fills=None,
    orders=None,
    positions=None,
    account_usd_balance: float = 10000.0,
):
    """Create a minimal mock BacktestEngine."""
    from kalshi.common.constants import KALSHI_VENUE
    from nautilus_trader.model.currencies import USD

    fills = fills or []
    orders = orders or []
    positions = positions or []

    cache = MagicMock()
    cache.fills.return_value = fills
    cache.orders.return_value = orders
    cache.positions.return_value = positions

    # instrument() returns None by default (heuristic fallback used)
    cache.instrument.return_value = None

    usd_balance_mock = MagicMock()
    usd_balance_mock.as_double.return_value = account_usd_balance
    account_mock = MagicMock()
    account_mock.balances.return_value = {USD: usd_balance_mock}

    portfolio = MagicMock()
    portfolio.account.return_value = account_mock

    engine = MagicMock()
    engine.cache = cache
    engine.portfolio = portfolio

    return engine


# ---------------------------------------------------------------------------
# BacktestResults dataclass
# ---------------------------------------------------------------------------

class TestBacktestResultsDataclass:
    def test_fields_accessible(self):
        r = BacktestResults(
            pnl_cents=100,
            fill_count=5,
            order_count=10,
            fill_rate=0.5,
            max_drawdown_cents=50,
            contracts_per_city={"ny": 20},
            avg_fill_price_cents=85.0,
            signals_received=20,
            filter_passes=8,
            filter_fails=12,
            ladders_placed=6,
            exits_attempted=2,
            orders_submitted=15,
            adjusted_pnl_cents=100.0,
        )
        assert r.pnl_cents == 100
        assert r.fill_rate == 0.5
        assert r.contracts_per_city == {"ny": 20}
        assert r.adjusted_pnl_cents == 100.0

    def test_zero_defaults_valid(self):
        r = BacktestResults(
            pnl_cents=0,
            fill_count=0,
            order_count=0,
            fill_rate=0.0,
            max_drawdown_cents=0,
            contracts_per_city={},
            avg_fill_price_cents=0.0,
            signals_received=0,
            filter_passes=0,
            filter_fails=0,
            ladders_placed=0,
            exits_attempted=0,
            orders_submitted=0,
            adjusted_pnl_cents=0.0,
        )
        assert r.fill_count == 0
        assert r.contracts_per_city == {}


# ---------------------------------------------------------------------------
# _compute_max_drawdown_cents
# ---------------------------------------------------------------------------

class TestComputeMaxDrawdown:
    def test_empty_fills_returns_zero(self):
        assert _compute_max_drawdown_cents([]) == 0

    def test_all_buys_drawdown_is_cumulative_cost(self):
        # Only buys: running PnL goes negative, peak stays 0
        fills = [
            _make_fill("BUY", 0.85, 10),  # -850
            _make_fill("BUY", 0.80, 10),  # -800 total: -1650
        ]
        dd = _compute_max_drawdown_cents(fills)
        assert dd == 1650  # peak=0, trough=-1650

    def test_sell_after_buy_reduces_drawdown(self):
        # Buy at 85c, sell at 90c — profit
        fills = [
            _make_fill("BUY", 0.85, 10),   # running = -850, peak=0, dd=850
            _make_fill("SELL", 0.90, 10),  # running = -850+900 = 50, peak=50, dd=0
        ]
        dd = _compute_max_drawdown_cents(fills)
        # Peak is 50 (after sell), trough is lowest which is -850, max dd is 850
        assert dd == 850

    def test_no_drawdown_when_all_profitable(self):
        # Sell first (proceeds), no buys -> running always increases
        fills = [
            _make_fill("SELL", 0.90, 5),   # running = 450
            _make_fill("SELL", 0.85, 5),   # running = 875
        ]
        dd = _compute_max_drawdown_cents(fills)
        assert dd == 0

    def test_drawdown_peak_trough_sequence(self):
        # Peak at 900, then drop to 400: drawdown = 500
        fills = [
            _make_fill("SELL", 0.90, 10),  # +900 running=900
            _make_fill("BUY", 0.50, 10),   # -500 running=400
        ]
        dd = _compute_max_drawdown_cents(fills)
        assert dd == 500


# ---------------------------------------------------------------------------
# _city_from_instrument_id
# ---------------------------------------------------------------------------

class TestCityFromInstrumentId:
    def test_extracts_city_from_kxhigh_ticker(self):
        cache = MagicMock()
        cache.instrument.return_value = None
        city = _city_from_instrument_id("KXHIGHNY-26MAR15-T54-YES.KALSHI", cache)
        assert city == "ny"

    def test_different_city_codes(self):
        cache = MagicMock()
        cache.instrument.return_value = None
        assert _city_from_instrument_id("KXHIGHCHI-26MAR15-T54-YES.KALSHI", cache) == "chi"
        assert _city_from_instrument_id("KXHIGHLA-26MAR15-T54-YES.KALSHI", cache) == "la"

    def test_unknown_ticker_format(self):
        cache = MagicMock()
        cache.instrument.return_value = None
        city = _city_from_instrument_id("SOMEOTHER-26MAR15.KALSHI", cache)
        assert city == "unknown"

    def test_uses_instrument_metadata_when_available(self):
        cache = MagicMock()
        instrument = MagicMock()
        instrument.info = {"city": "chicago"}
        cache.instrument.return_value = instrument
        city = _city_from_instrument_id("KXHIGHCHI-26MAR15-T54-YES.KALSHI", cache)
        assert city == "chicago"

    def test_falls_back_to_heuristic_when_no_city_in_info(self):
        cache = MagicMock()
        instrument = MagicMock()
        instrument.info = {"other_field": "value"}
        cache.instrument.return_value = instrument
        city = _city_from_instrument_id("KXHIGHNY-26MAR15-T54-YES.KALSHI", cache)
        assert city == "ny"


# ---------------------------------------------------------------------------
# _compute_contracts_per_city
# ---------------------------------------------------------------------------

class TestComputeContractsPerCity:
    def test_empty_fills(self):
        cache = MagicMock()
        cache.instrument.return_value = None
        assert _compute_contracts_per_city([], cache) == {}

    def test_counts_buy_fills_by_city(self):
        cache = MagicMock()
        cache.instrument.return_value = None
        fills = [
            _make_fill("BUY", 0.85, 5, "KXHIGHNY-26MAR15-T54-YES.KALSHI"),
            _make_fill("BUY", 0.80, 3, "KXHIGHNY-26MAR15-T54-YES.KALSHI"),
            _make_fill("BUY", 0.88, 7, "KXHIGHCHI-26MAR15-T54-YES.KALSHI"),
        ]
        result = _compute_contracts_per_city(fills, cache)
        assert result == {"ny": 8, "chi": 7}

    def test_sell_fills_excluded(self):
        cache = MagicMock()
        cache.instrument.return_value = None
        fills = [
            _make_fill("BUY", 0.85, 10, "KXHIGHNY-26MAR15-T54-YES.KALSHI"),
            _make_fill("SELL", 0.90, 10, "KXHIGHNY-26MAR15-T54-YES.KALSHI"),
        ]
        result = _compute_contracts_per_city(fills, cache)
        assert result == {"ny": 10}  # only BUY counted

    def test_multiple_cities(self):
        cache = MagicMock()
        cache.instrument.return_value = None
        fills = [
            _make_fill("BUY", 0.85, 5, "KXHIGHNY-26MAR15-T54-YES.KALSHI"),
            _make_fill("BUY", 0.80, 5, "KXHIGHLA-26MAR15-T54-YES.KALSHI"),
            _make_fill("BUY", 0.82, 5, "KXHIGHCHI-26MAR15-T54-YES.KALSHI"),
        ]
        result = _compute_contracts_per_city(fills, cache)
        assert result == {"ny": 5, "la": 5, "chi": 5}


# ---------------------------------------------------------------------------
# extract_results
# ---------------------------------------------------------------------------

class TestExtractResults:
    def test_empty_run(self):
        engine = _make_engine()
        strategy = _make_strategy()
        results = extract_results(engine, strategy)

        assert results.fill_count == 0
        assert results.order_count == 0
        assert results.fill_rate == 0.0
        assert results.avg_fill_price_cents == 0.0
        assert results.max_drawdown_cents == 0
        assert results.contracts_per_city == {}
        assert results.pnl_cents == 0
        assert results.adjusted_pnl_cents == 0.0

    def test_fill_rate_computed_correctly(self):
        fills = [_make_fill() for _ in range(3)]
        orders = [_make_order() for _ in range(10)]
        engine = _make_engine(fills=fills, orders=orders)
        strategy = _make_strategy()
        results = extract_results(engine, strategy)
        assert results.fill_count == 3
        assert results.order_count == 10
        assert abs(results.fill_rate - 0.3) < 1e-9

    def test_avg_fill_price_cents(self):
        # fills at 85c, 90c, 80c -> avg = 85c
        fills = [
            _make_fill("BUY", 0.85, 1),
            _make_fill("BUY", 0.90, 1),
            _make_fill("BUY", 0.80, 1),
        ]
        engine = _make_engine(fills=fills)
        strategy = _make_strategy()
        results = extract_results(engine, strategy)
        assert abs(results.avg_fill_price_cents - 85.0) < 0.1

    def test_pnl_from_positions(self):
        # Position with 5.00 USD realized PnL -> 500c
        positions = [_make_position(5.0)]
        engine = _make_engine(positions=positions)
        strategy = _make_strategy()
        results = extract_results(engine, strategy)
        assert results.pnl_cents == 500

    def test_multiple_positions_summed(self):
        positions = [_make_position(3.0), _make_position(-1.5), _make_position(2.0)]
        engine = _make_engine(positions=positions)
        strategy = _make_strategy()
        results = extract_results(engine, strategy)
        assert results.pnl_cents == 350  # 3.0 - 1.5 + 2.0 = 3.5 USD = 350c

    def test_strategy_counters_extracted(self):
        engine = _make_engine()
        strategy = _make_strategy(
            signals_received=50,
            filter_passes=20,
            filter_fails=30,
            ladders_placed=15,
            exits_attempted=3,
            orders_submitted=45,
        )
        results = extract_results(engine, strategy)
        assert results.signals_received == 50
        assert results.filter_passes == 20
        assert results.filter_fails == 30
        assert results.ladders_placed == 15
        assert results.exits_attempted == 3
        assert results.orders_submitted == 45

    def test_strategy_counters_default_to_zero_when_missing(self):
        """Gracefully handles strategy objects without diagnostic counters (pre-A2)."""
        engine = _make_engine()
        strategy = SimpleNamespace()  # no diagnostic counters
        results = extract_results(engine, strategy)
        assert results.signals_received == 0
        assert results.filter_passes == 0

    def test_adjusted_pnl_scales_by_fill_rate(self):
        # 3 fills, 10 orders -> fill_rate = 0.3; pnl = 500c; assumed = 0.5
        # adjusted = 500 * (0.3 / 0.5) = 300c
        positions = [_make_position(5.0)]  # 500c pnl
        fills = [_make_fill() for _ in range(3)]
        orders = [_make_order() for _ in range(10)]
        engine = _make_engine(fills=fills, orders=orders, positions=positions)
        strategy = _make_strategy()
        results = extract_results(engine, strategy, assumed_fill_rate=0.5)
        assert abs(results.adjusted_pnl_cents - 300.0) < 0.1

    def test_adjusted_pnl_zero_when_assumed_fill_rate_zero(self):
        positions = [_make_position(5.0)]
        engine = _make_engine(positions=positions)
        strategy = _make_strategy()
        results = extract_results(engine, strategy, assumed_fill_rate=0.0)
        assert results.adjusted_pnl_cents == 0.0

    def test_fill_rate_zero_when_no_orders(self):
        engine = _make_engine()
        strategy = _make_strategy()
        results = extract_results(engine, strategy)
        assert results.fill_rate == 0.0

    def test_contracts_per_city_from_buy_fills(self):
        fills = [
            _make_fill("BUY", 0.85, 5, "KXHIGHNY-26MAR15-T54-YES.KALSHI"),
            _make_fill("BUY", 0.80, 3, "KXHIGHCHI-26MAR15-T54-YES.KALSHI"),
        ]
        engine = _make_engine(fills=fills)
        strategy = _make_strategy()
        results = extract_results(engine, strategy)
        assert results.contracts_per_city == {"ny": 5, "chi": 3}


# ---------------------------------------------------------------------------
# format_report
# ---------------------------------------------------------------------------

class TestFormatReport:
    def _make_results(self, **overrides) -> BacktestResults:
        defaults = dict(
            pnl_cents=250,
            fill_count=10,
            order_count=20,
            fill_rate=0.5,
            max_drawdown_cents=100,
            contracts_per_city={"ny": 30, "chi": 10},
            avg_fill_price_cents=82.5,
            signals_received=40,
            filter_passes=15,
            filter_fails=25,
            ladders_placed=12,
            exits_attempted=3,
            orders_submitted=36,
            adjusted_pnl_cents=250.0,
        )
        defaults.update(overrides)
        return BacktestResults(**defaults)

    def test_returns_string(self):
        r = self._make_results()
        report = format_report(r)
        assert isinstance(report, str)

    def test_contains_pnl(self):
        r = self._make_results(pnl_cents=250)
        report = format_report(r)
        assert "+250" in report or "250" in report

    def test_contains_fill_rate(self):
        r = self._make_results(fill_rate=0.5)
        report = format_report(r)
        assert "50.0%" in report or "50%" in report

    def test_contains_signals_received(self):
        r = self._make_results(signals_received=40)
        report = format_report(r)
        assert "40" in report

    def test_contains_city_data(self):
        r = self._make_results(contracts_per_city={"ny": 30, "chi": 10})
        report = format_report(r)
        assert "ny" in report
        assert "30" in report
        assert "chi" in report
        assert "10" in report

    def test_empty_contracts_per_city(self):
        r = self._make_results(contracts_per_city={})
        report = format_report(r)
        assert "(none)" in report

    def test_contains_section_headers(self):
        r = self._make_results()
        report = format_report(r)
        assert "BACKTEST RESULTS" in report
        assert "PnL" in report
        assert "Fill Statistics" in report
        assert "Risk" in report
        assert "Strategy Diagnostics" in report

    def test_contains_max_drawdown(self):
        r = self._make_results(max_drawdown_cents=100)
        report = format_report(r)
        assert "100" in report

    def test_negative_pnl_formatted(self):
        r = self._make_results(pnl_cents=-500, adjusted_pnl_cents=-300.0)
        report = format_report(r)
        assert "-500" in report

    def test_contains_orders_submitted(self):
        r = self._make_results(orders_submitted=36)
        report = format_report(r)
        assert "36" in report

    def test_contains_avg_fill_price(self):
        r = self._make_results(avg_fill_price_cents=82.5)
        report = format_report(r)
        assert "82.5" in report
