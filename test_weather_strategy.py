"""Tests for WeatherStrategy (phased market-making).

Run: pytest test_weather_strategy.py --noconftest -v

Bind WeatherStrategy's methods onto a pure Python stand-in to bypass
NT's Cython read-only descriptors (log, clock, cache, etc.).
"""
import pytest
from datetime import date, datetime, timezone
from unittest.mock import MagicMock, call, patch

from nautilus_trader.model.enums import OrderSide

from data_types import ModelSignal, DangerAlert
from weather_strategy import WeatherStrategy, WeatherStrategyConfig


def _make_signal(
    ticker="KXHIGHCHI-26MAR15-T55",
    side="no",
    p_win=0.97,
    ts_event=0,
):
    return ModelSignal(
        city="chicago",
        ticker=ticker,
        side=side,
        p_win=p_win,
        model_scores={},
        features_snapshot={},
        ts_event=ts_event,
        ts_init=0,
    )


def _make_alert(ticker="KXHIGHCHI-26MAR15-T55", level="CRITICAL"):
    return DangerAlert(
        ticker=ticker,
        city="chicago",
        alert_level=level,
        rule_name="obs_approaching_threshold",
        reason="Test danger",
        features={"obs_temp": 54.5},
        ts_event=0,
        ts_init=0,
    )


class _TestableWeatherStrategy:
    """Pure Python stand-in for WeatherStrategy with mockable NT attributes.

    Binds actual methods from WeatherStrategy onto a plain Python object to
    bypass NautilusTrader's Cython read-only descriptors.
    """

    def __init__(self, config=None):
        self._cfg = config or WeatherStrategyConfig()
        # Quote tracking
        self._latest_quotes: dict = {}
        # Position tracking
        self._positions_info: dict = {}
        self._danger_exited: set = set()
        # Phase 1 state
        self._open_spread_placed: set = set()
        self._open_spread_orders: dict = {}
        self._first_tick_time: dict = {}
        # Phase 2 state
        self._ladder_orders: dict = {}
        self._resting_sells: dict = {}
        self._eligible_signals: dict = {}
        self._last_ladder_bid: dict = {}
        # Counters
        self._signals_received: int = 0
        self._alerts_received: int = 0
        self._spread_orders_placed: int = 0
        self._stable_orders_placed: int = 0
        self._feature_actor = None
        # NT mocks
        self.log = MagicMock()
        self.cache = MagicMock()
        self.order_factory = MagicMock()
        self.submit_order = MagicMock()
        self.cancel_order = MagicMock()
        self.clock = MagicMock()
        self.id = "test-strategy"

        # Default: clock.utc_now() returns a naive UTC datetime
        self.clock.utc_now.return_value = datetime(2026, 3, 11, 10, 0, 0, tzinfo=timezone.utc)

        # Default order_factory.limit() returns a mock order with a client_order_id
        def _make_mock_order(**kwargs):
            o = MagicMock()
            o.client_order_id = MagicMock()
            o.side = kwargs.get("order_side")
            o.quantity = kwargs.get("quantity")
            return o
        self.order_factory.limit.side_effect = _make_mock_order

        # Default: cache.orders_open() returns empty list
        self.cache.orders_open.return_value = []

    # Bind actual methods from WeatherStrategy
    on_data = WeatherStrategy.on_data
    on_quote_tick = WeatherStrategy.on_quote_tick
    on_order_filled = WeatherStrategy.on_order_filled
    _quote_key = WeatherStrategy._quote_key
    _evaluate_entry = WeatherStrategy._evaluate_entry
    _evaluate_exit = WeatherStrategy._evaluate_exit
    _deploy_open_spread = WeatherStrategy._deploy_open_spread
    _make_spread_cancel_callback = WeatherStrategy._make_spread_cancel_callback
    _cancel_spread_orders = WeatherStrategy._cancel_spread_orders
    _deploy_ladder = WeatherStrategy._deploy_ladder
    _count_pending_buys = WeatherStrategy._count_pending_buys
    _cancel_ladder_orders = WeatherStrategy._cancel_ladder_orders
    _cancel_all_buys_for_ticker = WeatherStrategy._cancel_all_buys_for_ticker
    _cancel_resting_sells_for_ticker = WeatherStrategy._cancel_resting_sells_for_ticker
    _cleanup_ticker_state = WeatherStrategy._cleanup_ticker_state
    _total_capital_at_risk = WeatherStrategy._total_capital_at_risk
    _cancel_all_resting_buys = WeatherStrategy._cancel_all_resting_buys
    _place_resting_sell = WeatherStrategy._place_resting_sell
    _on_refresh = WeatherStrategy._on_refresh
    _is_backoff_window = WeatherStrategy._is_backoff_window
    _utc_now = WeatherStrategy._utc_now
    _today = WeatherStrategy._today
    set_feature_actor = WeatherStrategy.set_feature_actor
    _sync_positions_to_actor = WeatherStrategy._sync_positions_to_actor

    @property
    def signals_received(self) -> int:
        return self._signals_received

    @property
    def alerts_received(self) -> int:
        return self._alerts_received


def _make_strategy(**config_kwargs) -> _TestableWeatherStrategy:
    config = WeatherStrategyConfig(**config_kwargs)
    return _TestableWeatherStrategy(config)


def _mock_instrument():
    """Return a mock instrument whose make_price/make_qty return usable mocks."""
    inst = MagicMock()
    inst.make_price.side_effect = lambda v: MagicMock(name=f"Price({v})")
    inst.make_qty.side_effect = lambda v: MagicMock(name=f"Qty({v})")
    return inst


def _mock_quote(bid_cents: int, ask_cents: int):
    q = MagicMock()
    q.bid_price.as_double.return_value = bid_cents / 100.0
    q.ask_price.as_double.return_value = ask_cents / 100.0
    return q


# ---------------------------------------------------------------------------
# Config Tests
# ---------------------------------------------------------------------------

class TestWeatherStrategyConfig:
    def test_defaults(self):
        cfg = WeatherStrategyConfig()
        assert cfg.open_spread_enabled is True
        assert cfg.open_spread_prices_cents == (45, 50, 55)
        assert cfg.open_spread_size == 3
        assert cfg.open_spread_min_p_win == 0.90
        assert cfg.open_spread_window_minutes == 30
        assert cfg.stable_min_p_win == 0.95
        assert cfg.stable_ladder_offsets_cents == (0, 1, 3, 5, 10)
        assert cfg.stable_size == 3
        assert cfg.sell_target_cents == 97
        assert cfg.refresh_interval_minutes == 5
        assert cfg.backoff_hour_utc == 7
        assert cfg.resume_hour_utc == 14
        assert cfg.max_position_per_ticker == 20
        assert cfg.danger_exit_enabled is True

    def test_custom_values(self):
        cfg = WeatherStrategyConfig(
            open_spread_prices_cents=(40, 45, 50),
            stable_min_p_win=0.90,
            sell_target_cents=95,
        )
        assert cfg.open_spread_prices_cents == (40, 45, 50)
        assert cfg.stable_min_p_win == 0.90
        assert cfg.sell_target_cents == 95

    def test_no_legacy_fields(self):
        """Old config fields (min_p_win, trade_size) must not exist."""
        cfg = WeatherStrategyConfig()
        assert not hasattr(cfg, "min_p_win")
        assert not hasattr(cfg, "trade_size")

    def test_max_cost_default(self):
        """max_cost_cents should default to 92."""
        cfg = WeatherStrategyConfig()
        assert cfg.max_cost_cents == 92

    def test_max_total_deployed_default(self):
        """max_total_deployed_cents should default to 0 (disabled)."""
        cfg = WeatherStrategyConfig()
        assert cfg.max_total_deployed_cents == 0


# ---------------------------------------------------------------------------
# NO-only filter (defense-in-depth)
# ---------------------------------------------------------------------------

class TestNoOnlyFilter:
    def test_yes_signal_ignored(self):
        strategy = _make_strategy()
        strategy._evaluate_entry(_make_signal(side="yes"))
        strategy.submit_order.assert_not_called()

    def test_no_signal_proceeds(self):
        """NO signal with valid quote and instrument proceeds to order placement."""
        strategy = _make_strategy(open_spread_enabled=False)
        # Set clock to today (contract settles today), outside backoff window
        strategy.clock.utc_now.return_value = datetime(2026, 3, 15, 15, 0, tzinfo=timezone.utc)

        quote_key = "KXHIGHCHI-26MAR15-T55-NO.KALSHI"
        strategy._latest_quotes[quote_key] = _mock_quote(bid_cents=85, ask_cents=87)
        strategy.cache.instrument.return_value = _mock_instrument()

        with patch("weather_strategy.parse_ticker") as mock_parse:
            mock_parse.return_value = {"settlement_date": "2026-03-15", "threshold": 55, "series": "KXHIGHCHI"}
            strategy._evaluate_entry(_make_signal(p_win=0.97))

        strategy._on_refresh()
        strategy.submit_order.assert_called()


# ---------------------------------------------------------------------------
# Danger-exited guard
# ---------------------------------------------------------------------------

class TestDangerExitedGuard:
    def test_danger_exited_blocks_entry(self):
        strategy = _make_strategy()
        strategy._danger_exited.add("KXHIGHCHI-26MAR15-T55")
        strategy._evaluate_entry(_make_signal())
        strategy.submit_order.assert_not_called()

    def test_danger_exited_does_not_block_other_tickers(self):
        strategy = _make_strategy(open_spread_enabled=False)
        strategy._danger_exited.add("KXHIGHCHI-26MAR15-T60")  # different ticker

        strategy.clock.utc_now.return_value = datetime(2026, 3, 15, 15, 0, tzinfo=timezone.utc)
        quote_key = "KXHIGHCHI-26MAR15-T55-NO.KALSHI"
        strategy._latest_quotes[quote_key] = _mock_quote(85, 87)
        strategy.cache.instrument.return_value = _mock_instrument()

        with patch("weather_strategy.parse_ticker") as mock_parse:
            mock_parse.return_value = {"settlement_date": "2026-03-15", "threshold": 55, "series": "KXHIGHCHI"}
            strategy._evaluate_entry(_make_signal(p_win=0.97))

        strategy._on_refresh()
        strategy.submit_order.assert_called()


# ---------------------------------------------------------------------------
# Phase 1 — Open Spread
# ---------------------------------------------------------------------------

class TestPhase1OpenSpread:
    def _setup_tomorrow_strategy(self, **kwargs):
        """Strategy configured for tomorrow contract testing."""
        defaults = dict(
            open_spread_enabled=True,
            open_spread_prices_cents=(45, 50, 55),
            open_spread_size=3,
            open_spread_min_p_win=0.90,
            open_spread_window_minutes=30,
        )
        defaults.update(kwargs)
        strategy = _make_strategy(**defaults)
        # Clock says today is 2026-03-14 → contract 2026-03-15 is tomorrow
        strategy.clock.utc_now.return_value = datetime(2026, 3, 14, 12, 0, tzinfo=timezone.utc)
        strategy.cache.instrument.return_value = _mock_instrument()
        return strategy

    def test_phase1_places_spread_for_tomorrow_contract(self):
        """Tomorrow contract within window should place 3 spread orders."""
        strategy = self._setup_tomorrow_strategy()

        with patch("weather_strategy.parse_ticker") as mock_parse:
            mock_parse.return_value = {
                "settlement_date": "2026-03-15",
                "threshold": 55,
                "series": "KXHIGHCHI",
            }
            strategy._evaluate_entry(_make_signal(
                ticker="KXHIGHCHI-26MAR15-T55",
                p_win=0.95,
                ts_event=1_000_000_000,
            ))

        assert strategy.submit_order.call_count == 3
        assert "KXHIGHCHI-26MAR15-T55" in strategy._open_spread_placed

    def test_phase1_spread_prices_match_config(self):
        """Each spread order should be at the configured price levels."""
        strategy = self._setup_tomorrow_strategy(open_spread_prices_cents=(40, 50, 60))
        mock_inst = _mock_instrument()
        strategy.cache.instrument.return_value = mock_inst

        with patch("weather_strategy.parse_ticker") as mock_parse:
            mock_parse.return_value = {"settlement_date": "2026-03-15", "threshold": 55, "series": "KXHIGHCHI"}
            strategy._evaluate_entry(_make_signal(p_win=0.95, ts_event=1_000_000_000))

        # make_price should be called with 0.40, 0.50, 0.60
        calls = [c.args[0] for c in mock_inst.make_price.call_args_list]
        assert 0.40 in calls
        assert 0.50 in calls
        assert 0.60 in calls

    def test_phase1_placed_only_once_per_ticker(self):
        """Second signal for same ticker should not re-place spread."""
        strategy = self._setup_tomorrow_strategy()

        with patch("weather_strategy.parse_ticker") as mock_parse:
            mock_parse.return_value = {"settlement_date": "2026-03-15", "threshold": 55, "series": "KXHIGHCHI"}
            signal = _make_signal(p_win=0.95, ts_event=1_000_000_000)
            strategy._evaluate_entry(signal)
            count_after_first = strategy.submit_order.call_count

            strategy._evaluate_entry(signal)
            count_after_second = strategy.submit_order.call_count

        # Second call should not place more spread orders (ticker already in _open_spread_placed)
        assert count_after_second == count_after_first
        # Guard fired — ticker should NOT have been stored as eligible for Phase 2
        assert signal.ticker not in strategy._eligible_signals

    def test_phase1_below_min_p_win_skips_spread(self):
        """Signal below open_spread_min_p_win should not place spread."""
        strategy = self._setup_tomorrow_strategy(open_spread_min_p_win=0.95)

        with patch("weather_strategy.parse_ticker") as mock_parse:
            mock_parse.return_value = {"settlement_date": "2026-03-15", "threshold": 55, "series": "KXHIGHCHI"}
            strategy._evaluate_entry(_make_signal(p_win=0.88, ts_event=1_000_000_000))

        # Falls through to Phase 2 but p_win < stable_min_p_win too, so no orders
        strategy.submit_order.assert_not_called()

    def test_phase1_window_expired_falls_through_to_phase2(self):
        """Tomorrow contract after window expires should use Phase 2 ladder."""
        strategy = self._setup_tomorrow_strategy(
            open_spread_window_minutes=30,
            stable_min_p_win=0.95,
        )
        quote_key = "KXHIGHCHI-26MAR15-T55-NO.KALSHI"
        strategy._latest_quotes[quote_key] = _mock_quote(85, 87)

        window_ns = 30 * 60 * 1_000_000_000
        # First tick sets _first_tick_time
        first_ts = 1_000_000_000
        strategy._first_tick_time["KXHIGHCHI-26MAR15-T55"] = first_ts
        # Signal arrives after window
        late_ts = first_ts + window_ns + 1

        with patch("weather_strategy.parse_ticker") as mock_parse:
            mock_parse.return_value = {"settlement_date": "2026-03-15", "threshold": 55, "series": "KXHIGHCHI"}
            strategy._evaluate_entry(_make_signal(p_win=0.97, ts_event=late_ts))

        # Should fall through to Phase 2 (signal stored, not Phase 1 spread)
        assert "KXHIGHCHI-26MAR15-T55" not in strategy._open_spread_placed
        # Advance clock outside backoff window for refresh to deploy
        strategy.clock.utc_now.return_value = datetime(2026, 3, 15, 15, 0, tzinfo=timezone.utc)
        strategy._on_refresh()
        strategy.submit_order.assert_called()

    def test_phase1_sets_cancel_timer(self):
        """Phase 1 deployment should set a one-shot cancel timer."""
        strategy = self._setup_tomorrow_strategy()
        strategy.clock.set_time_alert_ns = MagicMock()

        with patch("weather_strategy.parse_ticker") as mock_parse:
            mock_parse.return_value = {"settlement_date": "2026-03-15", "threshold": 55, "series": "KXHIGHCHI"}
            strategy._evaluate_entry(_make_signal(p_win=0.95, ts_event=1_000_000_000))

        strategy.clock.set_time_alert_ns.assert_called_once()
        call_kwargs = strategy.clock.set_time_alert_ns.call_args
        # Timer name should reference the ticker
        assert "spread_cancel" in call_kwargs.kwargs.get("name", "") or \
               "spread_cancel" in str(call_kwargs)

    def test_phase1_disabled(self):
        """With open_spread_enabled=False, tomorrow contract skips Phase 1."""
        strategy = self._setup_tomorrow_strategy(open_spread_enabled=False)
        quote_key = "KXHIGHCHI-26MAR15-T55-NO.KALSHI"
        strategy._latest_quotes[quote_key] = _mock_quote(85, 87)

        with patch("weather_strategy.parse_ticker") as mock_parse:
            mock_parse.return_value = {"settlement_date": "2026-03-15", "threshold": 55, "series": "KXHIGHCHI"}
            strategy._evaluate_entry(_make_signal(p_win=0.97, ts_event=1_000_000_000))

        # Should use Phase 2 instead (signal stored, deploy on refresh)
        assert "KXHIGHCHI-26MAR15-T55" not in strategy._open_spread_placed
        # Advance clock outside backoff window for refresh to deploy
        strategy.clock.utc_now.return_value = datetime(2026, 3, 15, 15, 0, tzinfo=timezone.utc)
        strategy._on_refresh()
        strategy.submit_order.assert_called()

    def test_phase1_today_contract_skips_to_phase2(self):
        """Today contract should go directly to Phase 2, not Phase 1."""
        strategy = _make_strategy(
            open_spread_enabled=True,
            stable_min_p_win=0.95,
        )
        # Clock says today is 2026-03-15 — same as settlement date, outside backoff
        strategy.clock.utc_now.return_value = datetime(2026, 3, 15, 15, 0, tzinfo=timezone.utc)
        quote_key = "KXHIGHCHI-26MAR15-T55-NO.KALSHI"
        strategy._latest_quotes[quote_key] = _mock_quote(85, 87)
        strategy.cache.instrument.return_value = _mock_instrument()

        with patch("weather_strategy.parse_ticker") as mock_parse:
            mock_parse.return_value = {"settlement_date": "2026-03-15", "threshold": 55, "series": "KXHIGHCHI"}
            strategy._evaluate_entry(_make_signal(p_win=0.97))

        assert "KXHIGHCHI-26MAR15-T55" not in strategy._open_spread_placed
        strategy._on_refresh()
        strategy.submit_order.assert_called()

    def test_phase1_stores_order_ids(self):
        """Phase 1 should store order IDs for later cancellation."""
        strategy = self._setup_tomorrow_strategy()
        strategy.clock.set_time_alert_ns = MagicMock()

        with patch("weather_strategy.parse_ticker") as mock_parse:
            mock_parse.return_value = {"settlement_date": "2026-03-15", "threshold": 55, "series": "KXHIGHCHI"}
            strategy._evaluate_entry(_make_signal(p_win=0.95, ts_event=1_000_000_000))

        ticker = "KXHIGHCHI-26MAR15-T55"
        assert ticker in strategy._open_spread_orders
        assert len(strategy._open_spread_orders[ticker]) == 3  # 3 price levels


# ---------------------------------------------------------------------------
# Phase 2 — Stable Ladder
# ---------------------------------------------------------------------------

class TestPhase2StableLadder:
    def _setup_today_strategy(self, **kwargs):
        defaults = dict(
            open_spread_enabled=False,
            stable_min_p_win=0.95,
            stable_ladder_offsets_cents=(0, 2, 5),
            stable_size=2,
        )
        defaults.update(kwargs)
        strategy = _make_strategy(**defaults)
        # 15:00 UTC is outside backoff window [7, 14) — needed for _on_refresh tests
        strategy.clock.utc_now.return_value = datetime(2026, 3, 15, 15, 0, tzinfo=timezone.utc)
        strategy.cache.instrument.return_value = _mock_instrument()
        return strategy

    def test_phase2_places_ladder_orders(self):
        """Phase 2 should place one order per offset level."""
        strategy = self._setup_today_strategy()
        quote_key = "KXHIGHCHI-26MAR15-T55-NO.KALSHI"
        strategy._latest_quotes[quote_key] = _mock_quote(bid_cents=80, ask_cents=82)

        with patch("weather_strategy.parse_ticker") as mock_parse:
            mock_parse.return_value = {"settlement_date": "2026-03-15", "threshold": 55, "series": "KXHIGHCHI"}
            strategy._evaluate_entry(_make_signal(p_win=0.97))

        strategy._on_refresh()
        # 3 offsets → 3 orders
        assert strategy.submit_order.call_count == 3

    def test_phase2_ladder_prices_below_bid(self):
        """Ladder prices should be bid minus each offset."""
        strategy = self._setup_today_strategy(stable_ladder_offsets_cents=(0, 3, 7))
        mock_inst = _mock_instrument()
        strategy.cache.instrument.return_value = mock_inst
        quote_key = "KXHIGHCHI-26MAR15-T55-NO.KALSHI"
        strategy._latest_quotes[quote_key] = _mock_quote(bid_cents=80, ask_cents=82)

        with patch("weather_strategy.parse_ticker") as mock_parse:
            mock_parse.return_value = {"settlement_date": "2026-03-15", "threshold": 55, "series": "KXHIGHCHI"}
            strategy._evaluate_entry(_make_signal(p_win=0.97))

        strategy._on_refresh()
        prices_called = [c.args[0] for c in mock_inst.make_price.call_args_list]
        assert 0.80 in prices_called   # bid - 0
        assert 0.77 in prices_called   # bid - 3c
        assert 0.73 in prices_called   # bid - 7c

    def test_phase2_ladder_clamps_to_1_cent(self):
        """Ladder price floor is 1c regardless of offset."""
        strategy = self._setup_today_strategy(stable_ladder_offsets_cents=(0, 5, 100))
        mock_inst = _mock_instrument()
        strategy.cache.instrument.return_value = mock_inst
        quote_key = "KXHIGHCHI-26MAR15-T55-NO.KALSHI"
        strategy._latest_quotes[quote_key] = _mock_quote(bid_cents=3, ask_cents=5)

        with patch("weather_strategy.parse_ticker") as mock_parse:
            mock_parse.return_value = {"settlement_date": "2026-03-15", "threshold": 55, "series": "KXHIGHCHI"}
            strategy._evaluate_entry(_make_signal(p_win=0.97))

        strategy._on_refresh()
        prices_called = [c.args[0] for c in mock_inst.make_price.call_args_list]
        # All prices should be >= 0.01
        assert all(p >= 0.01 for p in prices_called)

    def test_phase2_below_stable_min_p_win_skips(self):
        """Signal below stable_min_p_win should not enter."""
        strategy = self._setup_today_strategy(stable_min_p_win=0.95)
        quote_key = "KXHIGHCHI-26MAR15-T55-NO.KALSHI"
        strategy._latest_quotes[quote_key] = _mock_quote(85, 87)

        with patch("weather_strategy.parse_ticker") as mock_parse:
            mock_parse.return_value = {"settlement_date": "2026-03-15", "threshold": 55, "series": "KXHIGHCHI"}
            strategy._evaluate_entry(_make_signal(p_win=0.88))

        strategy.submit_order.assert_not_called()

    def test_phase2_no_quote_skips(self):
        """No quote → no order placed."""
        strategy = self._setup_today_strategy()
        # No quote set

        with patch("weather_strategy.parse_ticker") as mock_parse:
            mock_parse.return_value = {"settlement_date": "2026-03-15", "threshold": 55, "series": "KXHIGHCHI"}
            strategy._evaluate_entry(_make_signal(p_win=0.97))

        strategy.submit_order.assert_not_called()

    def test_phase2_instrument_not_in_cache(self):
        """No instrument → no order placed."""
        strategy = self._setup_today_strategy()
        quote_key = "KXHIGHCHI-26MAR15-T55-NO.KALSHI"
        strategy._latest_quotes[quote_key] = _mock_quote(85, 87)
        strategy.cache.instrument.return_value = None  # not in cache

        with patch("weather_strategy.parse_ticker") as mock_parse:
            mock_parse.return_value = {"settlement_date": "2026-03-15", "threshold": 55, "series": "KXHIGHCHI"}
            strategy._evaluate_entry(_make_signal(p_win=0.97))

        strategy.submit_order.assert_not_called()

    def test_phase2_max_position_blocks_entry(self):
        """Should not enter when at max position."""
        strategy = self._setup_today_strategy()
        strategy._positions_info["KXHIGHCHI-26MAR15-T55"] = {
            "side": "no", "contracts": 20
        }
        quote_key = "KXHIGHCHI-26MAR15-T55-NO.KALSHI"
        strategy._latest_quotes[quote_key] = _mock_quote(85, 87)

        with patch("weather_strategy.parse_ticker") as mock_parse:
            mock_parse.return_value = {"settlement_date": "2026-03-15", "threshold": 55, "series": "KXHIGHCHI"}
            strategy._evaluate_entry(_make_signal(p_win=0.97))

        strategy.submit_order.assert_not_called()

    def test_phase2_stores_eligible_signal(self):
        """Qualifying signal should be stored in _eligible_signals for refresh."""
        strategy = self._setup_today_strategy()
        quote_key = "KXHIGHCHI-26MAR15-T55-NO.KALSHI"
        strategy._latest_quotes[quote_key] = _mock_quote(85, 87)

        with patch("weather_strategy.parse_ticker") as mock_parse:
            mock_parse.return_value = {"settlement_date": "2026-03-15", "threshold": 55, "series": "KXHIGHCHI"}
            strategy._evaluate_entry(_make_signal(p_win=0.97))

        assert "KXHIGHCHI-26MAR15-T55" in strategy._eligible_signals

    def test_phase2_stores_ladder_order_ids(self):
        """Ladder orders should be tracked for cancellation."""
        strategy = self._setup_today_strategy(stable_ladder_offsets_cents=(0, 2))
        quote_key = "KXHIGHCHI-26MAR15-T55-NO.KALSHI"
        strategy._latest_quotes[quote_key] = _mock_quote(85, 87)

        with patch("weather_strategy.parse_ticker") as mock_parse:
            mock_parse.return_value = {"settlement_date": "2026-03-15", "threshold": 55, "series": "KXHIGHCHI"}
            strategy._evaluate_entry(_make_signal(p_win=0.97))

        strategy._on_refresh()
        assert "KXHIGHCHI-26MAR15-T55" in strategy._ladder_orders
        assert len(strategy._ladder_orders["KXHIGHCHI-26MAR15-T55"]) == 2

    def test_phase2_capacity_respects_pending_buys(self):
        """Pending buy orders count toward max_position_per_ticker."""
        strategy = _make_strategy(
            open_spread_enabled=False,
            stable_min_p_win=0.95,
            stable_ladder_offsets_cents=(0, 2, 5),
            stable_size=5,
            max_position_per_ticker=6,
        )
        strategy.clock.utc_now.return_value = datetime(2026, 3, 15, 15, 0, tzinfo=timezone.utc)
        strategy.cache.instrument.return_value = _mock_instrument()
        quote_key = "KXHIGHCHI-26MAR15-T55-NO.KALSHI"
        strategy._latest_quotes[quote_key] = _mock_quote(85, 87)

        # Simulate 4 contracts already in a pending buy order for this instrument
        # instrument_id.value must equal the string the production code compares against
        mock_open_order = MagicMock()
        mock_open_order.instrument_id.value = "KXHIGHCHI-26MAR15-T55-NO.KALSHI"
        mock_open_order.side = OrderSide.BUY
        mock_open_order.leaves_qty.as_double.return_value = 4.0
        strategy.cache.orders_open.return_value = [mock_open_order]

        with patch("weather_strategy.parse_ticker") as mock_parse:
            mock_parse.return_value = {"settlement_date": "2026-03-15", "threshold": 55, "series": "KXHIGHCHI"}
            strategy._evaluate_entry(_make_signal(p_win=0.97))

        strategy._on_refresh()
        # With 4 pending and max 6, only 2 contracts of capacity remain.
        # stable_size=5 per level but capacity=2, so only 1 ladder level fits (size=2).
        orders_placed = strategy.submit_order.call_count
        assert orders_placed <= 2  # capped by remaining capacity


class TestCostCeiling:
    def _setup_strategy(self, **kwargs):
        defaults = dict(
            open_spread_enabled=False,
            stable_min_p_win=0.95,
            stable_ladder_offsets_cents=(0, 2, 5),
            stable_size=2,
            max_cost_cents=92,
        )
        defaults.update(kwargs)
        strategy = _make_strategy(**defaults)
        strategy.clock.utc_now.return_value = datetime(2026, 3, 15, 15, 0, tzinfo=timezone.utc)
        strategy.cache.instrument.return_value = _mock_instrument()
        return strategy

    def test_ladder_skips_rungs_above_max_cost(self):
        """Ladder rungs priced above max_cost_cents should be skipped."""
        strategy = self._setup_strategy(max_cost_cents=92)
        quote_key = "KXHIGHCHI-26MAR15-T55-NO.KALSHI"
        strategy._latest_quotes[quote_key] = _mock_quote(bid_cents=95, ask_cents=97)

        with patch("weather_strategy.parse_ticker") as mock_parse:
            mock_parse.return_value = {"settlement_date": "2026-03-15", "threshold": 55, "series": "KXHIGHCHI"}
            strategy._evaluate_entry(_make_signal(p_win=0.97))

        strategy._on_refresh()
        # bid=95, offsets=(0,2,5) → prices 95,93,90
        # 95 > 92 → skip, 93 > 92 → skip, 90 <= 92 → place
        assert strategy.submit_order.call_count == 1
        mock_inst = strategy.cache.instrument.return_value
        prices_called = [c.args[0] for c in mock_inst.make_price.call_args_list]
        assert 0.90 in prices_called

    def test_ladder_all_rungs_above_max_cost_no_orders(self):
        """When all ladder rungs exceed max_cost, no orders should be placed."""
        strategy = self._setup_strategy(max_cost_cents=92, stable_ladder_offsets_cents=(0, 1, 2))
        quote_key = "KXHIGHCHI-26MAR15-T55-NO.KALSHI"
        strategy._latest_quotes[quote_key] = _mock_quote(bid_cents=97, ask_cents=99)

        with patch("weather_strategy.parse_ticker") as mock_parse:
            mock_parse.return_value = {"settlement_date": "2026-03-15", "threshold": 55, "series": "KXHIGHCHI"}
            strategy._evaluate_entry(_make_signal(p_win=0.97))

        strategy._on_refresh()
        # bid=97, offsets=(0,1,2) → prices 97,96,95 — all > 92
        strategy.submit_order.assert_not_called()

    def test_ladder_below_max_cost_places_normally(self):
        """When bid is well below max_cost, all rungs should place."""
        strategy = self._setup_strategy(max_cost_cents=92)
        quote_key = "KXHIGHCHI-26MAR15-T55-NO.KALSHI"
        strategy._latest_quotes[quote_key] = _mock_quote(bid_cents=80, ask_cents=82)

        with patch("weather_strategy.parse_ticker") as mock_parse:
            mock_parse.return_value = {"settlement_date": "2026-03-15", "threshold": 55, "series": "KXHIGHCHI"}
            strategy._evaluate_entry(_make_signal(p_win=0.97))

        strategy._on_refresh()
        # bid=80, offsets=(0,2,5) → prices 80,78,75 — all <= 92
        assert strategy.submit_order.call_count == 3


# ---------------------------------------------------------------------------
# On Order Filled — Resting Sell
# ---------------------------------------------------------------------------

class TestOnOrderFilled:
    def _make_fill_event(self, inst_str="KXHIGHCHI-26MAR15-T55-NO", qty=3, side=OrderSide.BUY):
        event = MagicMock()
        event.instrument_id = MagicMock()
        event.instrument_id.symbol.value = inst_str
        event.last_qty.as_double.return_value = float(qty)
        event.order_side = side
        return event

    def test_buy_fill_places_resting_sell(self):
        """BUY fill should immediately place a resting GTC sell at sell_target_cents."""
        strategy = _make_strategy(sell_target_cents=97)
        mock_inst = _mock_instrument()
        strategy.cache.instrument.return_value = mock_inst

        event = self._make_fill_event(qty=3, side=OrderSide.BUY)
        strategy.on_order_filled(event)

        # Should place exactly 1 sell order (the resting sell)
        strategy.submit_order.assert_called_once()
        # The sell should be at 97c
        mock_inst.make_price.assert_called_with(0.97)

    def test_buy_fill_increments_position(self):
        """BUY fill should increment position contracts."""
        strategy = _make_strategy()
        strategy.cache.instrument.return_value = _mock_instrument()

        event = self._make_fill_event(qty=5, side=OrderSide.BUY)
        strategy.on_order_filled(event)

        ticker = "KXHIGHCHI-26MAR15-T55"
        assert strategy._positions_info[ticker]["contracts"] == 5

    def test_sell_fill_decrements_position(self):
        """SELL fill should decrement position contracts."""
        strategy = _make_strategy()
        strategy.cache.instrument.return_value = _mock_instrument()
        ticker = "KXHIGHCHI-26MAR15-T55"
        strategy._positions_info[ticker] = {"side": "no", "contracts": 5, "city": "", "threshold": 0}

        event = self._make_fill_event(qty=3, side=OrderSide.SELL)
        strategy.on_order_filled(event)

        assert strategy._positions_info[ticker]["contracts"] == 2

    def test_sell_fill_no_resting_sell(self):
        """SELL fill should NOT place another sell order."""
        strategy = _make_strategy()
        strategy.cache.instrument.return_value = _mock_instrument()
        ticker = "KXHIGHCHI-26MAR15-T55"
        strategy._positions_info[ticker] = {"side": "no", "contracts": 3, "city": "", "threshold": 0}

        event = self._make_fill_event(qty=3, side=OrderSide.SELL)
        strategy.on_order_filled(event)

        strategy.submit_order.assert_not_called()

    def test_position_closed_removes_info(self):
        """When position reaches 0 contracts, entry is removed from _positions_info."""
        strategy = _make_strategy()
        strategy.cache.instrument.return_value = _mock_instrument()
        ticker = "KXHIGHCHI-26MAR15-T55"
        strategy._positions_info[ticker] = {"side": "no", "contracts": 3, "city": "", "threshold": 0}

        event = self._make_fill_event(qty=3, side=OrderSide.SELL)
        strategy.on_order_filled(event)

        assert ticker not in strategy._positions_info

    def test_buy_fill_tracks_resting_sell_id(self):
        """BUY fill should record resting sell order ID in _resting_sells."""
        strategy = _make_strategy()
        strategy.cache.instrument.return_value = _mock_instrument()

        event = self._make_fill_event(qty=2, side=OrderSide.BUY)
        strategy.on_order_filled(event)

        ticker = "KXHIGHCHI-26MAR15-T55"
        assert ticker in strategy._resting_sells
        assert len(strategy._resting_sells[ticker]) == 1

    def test_buy_fill_syncs_to_actor(self):
        """on_order_filled should sync positions to FeatureActor."""
        strategy = _make_strategy()
        strategy.cache.instrument.return_value = _mock_instrument()
        mock_actor = MagicMock()
        strategy._feature_actor = mock_actor

        event = self._make_fill_event(qty=1, side=OrderSide.BUY)
        strategy.on_order_filled(event)

        mock_actor.update_positions.assert_called_once()

    def test_unknown_instrument_format_logged(self):
        """Unknown instrument format should log warning and not crash."""
        strategy = _make_strategy()
        event = MagicMock()
        event.instrument_id.symbol.value = "UNKNOWN_FORMAT"
        event.last_qty.as_double.return_value = 1.0
        event.order_side = OrderSide.BUY

        strategy.on_order_filled(event)  # should not raise
        strategy.submit_order.assert_not_called()


# ---------------------------------------------------------------------------
# Periodic Refresh
# ---------------------------------------------------------------------------

class TestPeriodicRefresh:
    def _make_refresh_strategy(self, **kwargs):
        defaults = dict(
            open_spread_enabled=False,
            stable_min_p_win=0.95,
            stable_ladder_offsets_cents=(0, 2),
            stable_size=2,
            backoff_hour_utc=7,
            resume_hour_utc=14,
        )
        defaults.update(kwargs)
        strategy = _make_strategy(**defaults)
        # Active trading hour: 15 UTC (outside back-off window [7, 14))
        strategy.clock.utc_now.return_value = datetime(2026, 3, 15, 15, 0, tzinfo=timezone.utc)
        strategy.cache.instrument.return_value = _mock_instrument()
        return strategy

    def test_refresh_redeploys_ladder_for_eligible_signals(self):
        """_on_refresh() should redeploy ladder for stored eligible signals."""
        strategy = self._make_refresh_strategy()

        signal = _make_signal(p_win=0.97)
        strategy._eligible_signals["KXHIGHCHI-26MAR15-T55"] = signal
        quote_key = "KXHIGHCHI-26MAR15-T55-NO.KALSHI"
        strategy._latest_quotes[quote_key] = _mock_quote(85, 87)

        strategy._on_refresh()

        # Should have placed ladder orders
        strategy.submit_order.assert_called()

    def test_refresh_cancels_old_ladder_before_redeploying(self):
        """_on_refresh() should cancel existing ladder orders before placing new ones."""
        strategy = self._make_refresh_strategy()

        # Set up existing ladder order in cache
        old_order_id = MagicMock()
        old_order_id.__eq__ = lambda self, other: self is other
        ticker = "KXHIGHCHI-26MAR15-T55"
        strategy._ladder_orders[ticker] = [old_order_id]

        mock_open_order = MagicMock()
        mock_open_order.client_order_id = old_order_id
        mock_open_order.side = OrderSide.BUY
        strategy.cache.orders_open.return_value = [mock_open_order]

        signal = _make_signal(p_win=0.97)
        strategy._eligible_signals[ticker] = signal
        quote_key = "KXHIGHCHI-26MAR15-T55-NO.KALSHI"
        strategy._latest_quotes[quote_key] = _mock_quote(85, 87)

        strategy._on_refresh()

        # cancel_order should have been called for old order
        strategy.cancel_order.assert_called_with(mock_open_order)

    def test_refresh_skips_danger_exited_tickers(self):
        """_on_refresh() should not redeploy for danger-exited tickers."""
        strategy = self._make_refresh_strategy()
        ticker = "KXHIGHCHI-26MAR15-T55"
        strategy._danger_exited.add(ticker)
        strategy._eligible_signals[ticker] = _make_signal(p_win=0.97)
        strategy._latest_quotes["KXHIGHCHI-26MAR15-T55-NO.KALSHI"] = _mock_quote(85, 87)

        strategy._on_refresh()
        strategy.submit_order.assert_not_called()

    def test_refresh_clears_ladder_orders_dict(self):
        """After cancellation, old order IDs should be cancelled and new ones deployed."""
        strategy = self._make_refresh_strategy()
        ticker = "KXHIGHCHI-26MAR15-T55"

        # Set up two old order IDs in ladder_orders
        old_id_1 = MagicMock()
        old_id_2 = MagicMock()
        strategy._ladder_orders[ticker] = [old_id_1, old_id_2]

        # Make cache.orders_open return open orders matching the old IDs
        mock_order_1 = MagicMock()
        mock_order_1.client_order_id = old_id_1
        mock_order_1.side = OrderSide.BUY
        mock_order_2 = MagicMock()
        mock_order_2.client_order_id = old_id_2
        mock_order_2.side = OrderSide.BUY
        strategy.cache.orders_open.return_value = [mock_order_1, mock_order_2]

        strategy._eligible_signals[ticker] = _make_signal(p_win=0.97)
        quote_key = "KXHIGHCHI-26MAR15-T55-NO.KALSHI"
        strategy._latest_quotes[quote_key] = _mock_quote(85, 87)

        strategy._on_refresh()

        # Old orders should have been cancelled
        strategy.cancel_order.assert_any_call(mock_order_1)
        strategy.cancel_order.assert_any_call(mock_order_2)

        # New orders should have been deployed (ladder redeployment)
        strategy.submit_order.assert_called()

        # The ladder_orders dict should contain only the new order IDs (not old ones)
        new_order_ids = set(strategy._ladder_orders.get(ticker, []))
        assert old_id_1 not in new_order_ids
        assert old_id_2 not in new_order_ids


# ---------------------------------------------------------------------------
# Back-off Window
# ---------------------------------------------------------------------------

class TestBackoffWindow:
    def test_backoff_cancels_all_buys(self):
        """During back-off window, _on_refresh should cancel all BUY orders."""
        strategy = _make_strategy(backoff_hour_utc=7, resume_hour_utc=14)
        # 08 UTC = in back-off window [7, 14)
        strategy.clock.utc_now.return_value = datetime(2026, 3, 15, 8, 0, tzinfo=timezone.utc)

        mock_buy_order = MagicMock()
        mock_buy_order.side = OrderSide.BUY
        mock_sell_order = MagicMock()
        mock_sell_order.side = OrderSide.SELL
        strategy.cache.orders_open.return_value = [mock_buy_order, mock_sell_order]

        strategy._on_refresh()

        # Only buy order should be cancelled
        strategy.cancel_order.assert_called_once_with(mock_buy_order)

    def test_backoff_does_not_cancel_sells(self):
        """Back-off should preserve resting sell orders."""
        strategy = _make_strategy(backoff_hour_utc=7, resume_hour_utc=14)
        strategy.clock.utc_now.return_value = datetime(2026, 3, 15, 8, 0, tzinfo=timezone.utc)

        mock_sell_order = MagicMock()
        mock_sell_order.side = OrderSide.SELL
        strategy.cache.orders_open.return_value = [mock_sell_order]

        strategy._on_refresh()
        strategy.cancel_order.assert_not_called()

    def test_backoff_skips_ladder_redeployment(self):
        """During back-off, no new ladder orders should be placed."""
        strategy = _make_strategy(backoff_hour_utc=7, resume_hour_utc=14)
        strategy.clock.utc_now.return_value = datetime(2026, 3, 15, 8, 0, tzinfo=timezone.utc)
        strategy.cache.orders_open.return_value = []
        strategy._eligible_signals["T55"] = _make_signal(p_win=0.97)

        strategy._on_refresh()
        strategy.submit_order.assert_not_called()

    def test_active_window_deploys_ladder(self):
        """Outside back-off window, refresh should deploy ladders."""
        strategy = _make_strategy(
            backoff_hour_utc=7,
            resume_hour_utc=14,
            open_spread_enabled=False,
            stable_ladder_offsets_cents=(0,),
            stable_size=2,
        )
        # 15 UTC = outside back-off window
        strategy.clock.utc_now.return_value = datetime(2026, 3, 15, 15, 0, tzinfo=timezone.utc)
        strategy.cache.instrument.return_value = _mock_instrument()
        ticker = "KXHIGHCHI-26MAR15-T55"
        strategy._eligible_signals[ticker] = _make_signal(ticker=ticker, p_win=0.97)
        strategy._latest_quotes["KXHIGHCHI-26MAR15-T55-NO.KALSHI"] = _mock_quote(85, 87)

        strategy._on_refresh()
        strategy.submit_order.assert_called()

    def test_is_backoff_window_boundaries(self):
        """_is_backoff_window returns True only within [backoff_hour, resume_hour)."""
        strategy = _make_strategy(backoff_hour_utc=7, resume_hour_utc=14)

        strategy.clock.utc_now.return_value = datetime(2026, 3, 15, 7, 0, tzinfo=timezone.utc)
        assert strategy._is_backoff_window() is True

        strategy.clock.utc_now.return_value = datetime(2026, 3, 15, 13, 59, tzinfo=timezone.utc)
        assert strategy._is_backoff_window() is True

        strategy.clock.utc_now.return_value = datetime(2026, 3, 15, 14, 0, tzinfo=timezone.utc)
        assert strategy._is_backoff_window() is False

        strategy.clock.utc_now.return_value = datetime(2026, 3, 15, 6, 59, tzinfo=timezone.utc)
        assert strategy._is_backoff_window() is False

    def test_backoff_clears_tracked_order_lists(self):
        """Back-off cancel should clear _open_spread_orders and _ladder_orders."""
        strategy = _make_strategy(backoff_hour_utc=7, resume_hour_utc=14)
        strategy.clock.utc_now.return_value = datetime(2026, 3, 15, 8, 0, tzinfo=timezone.utc)
        strategy.cache.orders_open.return_value = []

        strategy._open_spread_orders["T55"] = [MagicMock()]
        strategy._ladder_orders["T55"] = [MagicMock()]

        strategy._on_refresh()

        assert len(strategy._open_spread_orders) == 0
        assert len(strategy._ladder_orders) == 0


# ---------------------------------------------------------------------------
# Global Rebalance (replaces idempotent refresh)
# ---------------------------------------------------------------------------

class TestGlobalRebalance:
    def _make_refresh_strategy(self, **kwargs):
        defaults = dict(
            open_spread_enabled=False,
            stable_min_p_win=0.95,
            stable_ladder_offsets_cents=(0, 2),
            stable_size=2,
            backoff_hour_utc=7,
            resume_hour_utc=14,
        )
        defaults.update(kwargs)
        strategy = _make_strategy(**defaults)
        strategy.clock.utc_now.return_value = datetime(2026, 3, 15, 15, 0, tzinfo=timezone.utc)
        strategy.cache.instrument.return_value = _mock_instrument()
        return strategy

    def test_refresh_always_redeploys(self):
        """Refresh always cancels and redeploys, even when bid is unchanged."""
        strategy = self._make_refresh_strategy()
        ticker = "KXHIGHCHI-26MAR15-T55"
        strategy._eligible_signals[ticker] = _make_signal(p_win=0.97)
        strategy._latest_quotes["KXHIGHCHI-26MAR15-T55-NO.KALSHI"] = _mock_quote(85, 87)
        strategy._last_ladder_bid[ticker] = 85

        strategy._on_refresh()

        strategy.submit_order.assert_called()

    def test_refresh_redeploys_when_bid_changes(self):
        """Refresh should cancel and redeploy when bid has moved."""
        strategy = self._make_refresh_strategy()
        ticker = "KXHIGHCHI-26MAR15-T55"
        strategy._eligible_signals[ticker] = _make_signal(p_win=0.97)
        strategy._latest_quotes["KXHIGHCHI-26MAR15-T55-NO.KALSHI"] = _mock_quote(88, 90)
        strategy._last_ladder_bid[ticker] = 85  # old bid was 85, now 88

        strategy._on_refresh()

        strategy.submit_order.assert_called()
        assert strategy._last_ladder_bid[ticker] == 88

    def test_cheapest_first(self):
        """On refresh, cheapest contracts get laddered before expensive ones."""
        strategy = self._make_refresh_strategy(
            stable_ladder_offsets_cents=(0,),
            stable_size=2,
            max_cost_cents=95,
            max_total_deployed_cents=5000,
        )

        # Two signals: SFO cheap (80c), NY expensive (90c)
        strategy._eligible_signals["KXHIGHSFO-26MAR15-T71"] = _make_signal(
            ticker="KXHIGHSFO-26MAR15-T71", p_win=0.97)
        strategy._eligible_signals["KXHIGHNY-26MAR15-T52"] = _make_signal(
            ticker="KXHIGHNY-26MAR15-T52", p_win=0.97)
        strategy._latest_quotes["KXHIGHSFO-26MAR15-T71-NO.KALSHI"] = _mock_quote(80, 82)
        strategy._latest_quotes["KXHIGHNY-26MAR15-T52-NO.KALSHI"] = _mock_quote(90, 92)

        strategy._on_refresh()

        # Both should get ladders (budget permits)
        assert "KXHIGHSFO-26MAR15-T71" in strategy._ladder_orders
        assert "KXHIGHNY-26MAR15-T52" in strategy._ladder_orders

    def test_budget_exhaustion_skips_expensive(self):
        """When budget fits only cheapest contracts, expensive ones are skipped."""
        strategy = self._make_refresh_strategy(
            stable_ladder_offsets_cents=(0,),
            stable_size=1,
            max_cost_cents=95,
            max_total_deployed_cents=160,
        )

        # Two signals: SFO=80c, NY=90c
        strategy._eligible_signals["KXHIGHSFO-26MAR15-T71"] = _make_signal(
            ticker="KXHIGHSFO-26MAR15-T71", p_win=0.97)
        strategy._eligible_signals["KXHIGHNY-26MAR15-T52"] = _make_signal(
            ticker="KXHIGHNY-26MAR15-T52", p_win=0.97)
        strategy._latest_quotes["KXHIGHSFO-26MAR15-T71-NO.KALSHI"] = _mock_quote(80, 82)
        strategy._latest_quotes["KXHIGHNY-26MAR15-T52-NO.KALSHI"] = _mock_quote(90, 92)

        # Mock _total_capital_at_risk to simulate budget being consumed:
        # First call (SFO deploy): 0c at risk → deploys 80c
        # Second call (NY deploy): 80c at risk → 80c remaining < 90c → skip
        call_count = [0]
        def mock_at_risk():
            call_count[0] += 1
            if call_count[0] == 1:
                return 0
            return 80
        strategy._total_capital_at_risk = mock_at_risk

        strategy._on_refresh()

        # SFO deployed (cheapest first), NY skipped (budget exhausted)
        assert "KXHIGHSFO-26MAR15-T71" in strategy._ladder_orders
        assert "KXHIGHNY-26MAR15-T52" not in strategy._ladder_orders

    def test_evaluate_entry_stores_only(self):
        """_evaluate_entry stores signal but does not place orders."""
        strategy = self._make_refresh_strategy()
        quote_key = "KXHIGHCHI-26MAR15-T55-NO.KALSHI"
        strategy._latest_quotes[quote_key] = _mock_quote(85, 87)

        with patch("weather_strategy.parse_ticker") as mock_parse:
            mock_parse.return_value = {"settlement_date": "2026-03-15", "threshold": 55, "series": "KXHIGHCHI"}
            strategy._evaluate_entry(_make_signal(p_win=0.97))

        # Signal stored, but no orders placed until refresh
        assert "KXHIGHCHI-26MAR15-T55" in strategy._eligible_signals
        strategy.submit_order.assert_not_called()

    def test_backoff_clears_last_ladder_bid(self):
        """Back-off should clear _last_ladder_bid so ladders redeploy after resume."""
        strategy = self._make_refresh_strategy()
        ticker = "KXHIGHCHI-26MAR15-T55"
        strategy._last_ladder_bid[ticker] = 85
        strategy._ladder_orders[ticker] = [MagicMock()]

        # Enter back-off
        strategy.clock.utc_now.return_value = datetime(2026, 3, 15, 8, 0, tzinfo=timezone.utc)
        strategy.cache.orders_open.return_value = []
        strategy._on_refresh()

        assert len(strategy._last_ladder_bid) == 0

    def test_danger_exit_clears_last_ladder_bid(self):
        """Danger exit should remove ticker from _last_ladder_bid."""
        strategy = self._make_refresh_strategy()
        ticker = "KXHIGHCHI-26MAR15-T55"
        strategy._last_ladder_bid[ticker] = 85
        strategy._positions_info[ticker] = {
            "side": "no", "threshold": 55.0, "city": "chicago", "contracts": 5
        }
        strategy.cache.instrument.return_value = _mock_instrument()
        strategy._latest_quotes["KXHIGHCHI-26MAR15-T55-NO.KALSHI"] = _mock_quote(88, 90)
        strategy.cache.orders_open.return_value = []

        alert = _make_alert(ticker=ticker, level="CRITICAL")
        strategy._evaluate_exit(alert)

        assert ticker not in strategy._last_ladder_bid


# ---------------------------------------------------------------------------
# Danger Exit (unchanged behavior)
# ---------------------------------------------------------------------------

class TestDangerExit:
    def test_critical_alert_triggers_sell(self):
        """CRITICAL DangerAlert should trigger sell and add to _danger_exited."""
        strategy = _make_strategy()
        ticker = "KXHIGHCHI-26MAR15-T55"
        strategy._positions_info[ticker] = {
            "side": "no", "threshold": 55.0, "city": "chicago", "contracts": 5
        }
        mock_inst = _mock_instrument()
        strategy.cache.instrument.return_value = mock_inst
        strategy._latest_quotes["KXHIGHCHI-26MAR15-T55-NO.KALSHI"] = _mock_quote(88, 90)

        strategy._evaluate_exit(_make_alert(ticker=ticker, level="CRITICAL"))

        strategy.submit_order.assert_called_once()
        assert ticker in strategy._danger_exited

    def test_critical_alert_cancels_resting_buys(self):
        """CRITICAL exit should cancel all resting buy orders for the ticker."""
        strategy = _make_strategy()
        ticker = "KXHIGHCHI-26MAR15-T55"
        strategy._positions_info[ticker] = {"side": "no", "contracts": 3, "city": "", "threshold": 0}

        old_order_id = MagicMock()
        strategy._ladder_orders[ticker] = [old_order_id]

        mock_open_order = MagicMock()
        mock_open_order.client_order_id = old_order_id
        mock_open_order.side = OrderSide.BUY
        strategy.cache.orders_open.return_value = [mock_open_order]
        strategy.cache.instrument.return_value = _mock_instrument()

        strategy._evaluate_exit(_make_alert(ticker=ticker, level="CRITICAL"))

        strategy.cancel_order.assert_called_with(mock_open_order)

    def test_non_critical_alert_no_sell(self):
        """Non-CRITICAL alerts log but do not sell."""
        strategy = _make_strategy()
        ticker = "KXHIGHCHI-26MAR15-T55"
        strategy._positions_info[ticker] = {"side": "no", "contracts": 5, "city": ""}

        strategy._evaluate_exit(_make_alert(ticker=ticker, level="CAUTION"))
        strategy.submit_order.assert_not_called()

    def test_danger_exit_no_retry(self):
        """Already danger-exited tickers should not be re-sold."""
        strategy = _make_strategy()
        ticker = "KXHIGHCHI-26MAR15-T55"
        strategy._danger_exited.add(ticker)
        strategy._positions_info[ticker] = {"side": "no", "contracts": 5}

        strategy._evaluate_exit(_make_alert(ticker=ticker, level="CRITICAL"))
        strategy.submit_order.assert_not_called()

    def test_danger_exit_no_position_no_sell(self):
        """No position → no sell."""
        strategy = _make_strategy()
        strategy._evaluate_exit(_make_alert(level="CRITICAL"))
        strategy.submit_order.assert_not_called()

    def test_danger_exit_disabled(self):
        """With danger_exit_enabled=False, no sell on CRITICAL."""
        strategy = _make_strategy(danger_exit_enabled=False)
        ticker = "KXHIGHCHI-26MAR15-T55"
        strategy._positions_info[ticker] = {"side": "no", "contracts": 5}

        strategy._evaluate_exit(_make_alert(ticker=ticker, level="CRITICAL"))
        strategy.submit_order.assert_not_called()

    def test_danger_exit_fallback_price(self):
        """With no quote, sell at 1c fallback."""
        strategy = _make_strategy()
        ticker = "KXHIGHCHI-26MAR15-T55"
        strategy._positions_info[ticker] = {"side": "no", "contracts": 3, "city": "", "threshold": 0}
        mock_inst = _mock_instrument()
        strategy.cache.instrument.return_value = mock_inst

        strategy._evaluate_exit(_make_alert(ticker=ticker, level="CRITICAL"))

        strategy.submit_order.assert_called_once()
        mock_inst.make_price.assert_called_with(0.01)

    def test_danger_exit_marks_before_sell(self):
        """_danger_exited is populated BEFORE submitting the sell order."""
        ticker = "KXHIGHCHI-26MAR15-T55"
        marked_before = {}

        strategy = _make_strategy()
        strategy._positions_info[ticker] = {"side": "no", "contracts": 2, "city": "", "threshold": 0}
        strategy.cache.instrument.return_value = _mock_instrument()

        original_submit = strategy.submit_order

        def _check_submit(order):
            marked_before["result"] = ticker in strategy._danger_exited
            original_submit(order)

        strategy.submit_order = _check_submit
        strategy._evaluate_exit(_make_alert(ticker=ticker, level="CRITICAL"))
        assert marked_before.get("result") is True


# ---------------------------------------------------------------------------
# on_data routing
# ---------------------------------------------------------------------------

class TestOnDataRouting:
    def test_routes_model_signal(self):
        strategy = _make_strategy()
        signal = _make_signal(p_win=0.80)  # low p_win, won't order but will count
        strategy.on_data(signal)
        assert strategy.signals_received == 1

    def test_routes_danger_alert(self):
        strategy = _make_strategy()
        alert = _make_alert(level="WATCH")
        strategy.on_data(alert)
        assert strategy.alerts_received == 1

    def test_both_counters_independent(self):
        strategy = _make_strategy()
        strategy.on_data(_make_signal(p_win=0.50))
        strategy.on_data(_make_signal(p_win=0.50))
        strategy.on_data(_make_alert(level="WATCH"))
        assert strategy.signals_received == 2
        assert strategy.alerts_received == 1


# ---------------------------------------------------------------------------
# on_quote_tick
# ---------------------------------------------------------------------------

class TestOnQuoteTick:
    def test_stores_quote(self):
        strategy = _make_strategy()
        mock_tick = MagicMock()
        mock_tick.instrument_id.value = "KXHIGHCHI-26MAR15-T55-NO.KALSHI"
        strategy.on_quote_tick(mock_tick)
        assert strategy._latest_quotes["KXHIGHCHI-26MAR15-T55-NO.KALSHI"] is mock_tick

    def test_overrides_old_quote(self):
        strategy = _make_strategy()
        old_tick = MagicMock()
        old_tick.instrument_id.value = "KXHIGHCHI-26MAR15-T55-NO.KALSHI"
        new_tick = MagicMock()
        new_tick.instrument_id.value = "KXHIGHCHI-26MAR15-T55-NO.KALSHI"
        strategy.on_quote_tick(old_tick)
        strategy.on_quote_tick(new_tick)
        assert strategy._latest_quotes["KXHIGHCHI-26MAR15-T55-NO.KALSHI"] is new_tick

    def test_no_profit_target_check(self):
        """on_quote_tick should NOT call any profit target logic (removed)."""
        strategy = _make_strategy()
        mock_tick = MagicMock()
        mock_tick.instrument_id.value = "KXHIGHCHI-26MAR15-T55-NO.KALSHI"
        strategy.on_quote_tick(mock_tick)
        # No submit_order call from quote tick alone
        strategy.submit_order.assert_not_called()


# ---------------------------------------------------------------------------
# Feature actor sync
# ---------------------------------------------------------------------------

class TestFeatureActorSync:
    def test_set_feature_actor(self):
        strategy = _make_strategy()
        mock_actor = MagicMock()
        strategy.set_feature_actor(mock_actor)
        assert strategy._feature_actor is mock_actor

    def test_sync_pushes_positions(self):
        strategy = _make_strategy()
        mock_actor = MagicMock()
        strategy._feature_actor = mock_actor
        strategy._positions_info = {"T55": {"side": "no"}}
        strategy._sync_positions_to_actor()
        mock_actor.update_positions.assert_called_once_with(strategy._positions_info)

    def test_sync_no_actor_noop(self):
        strategy = _make_strategy()
        strategy._feature_actor = None
        strategy._sync_positions_to_actor()  # should not raise


# ---------------------------------------------------------------------------
# Properties
# ---------------------------------------------------------------------------

class TestProperties:
    def test_initial_values(self):
        strategy = _make_strategy()
        assert strategy.signals_received == 0
        assert strategy.alerts_received == 0

    def test_incremented_values(self):
        strategy = _make_strategy()
        strategy._signals_received = 7
        strategy._alerts_received = 2
        assert strategy.signals_received == 7
        assert strategy.alerts_received == 2


# ---------------------------------------------------------------------------
# parse_ticker failure path
# ---------------------------------------------------------------------------

class TestParseTickerFailure:
    def test_unparseable_ticker_skipped(self):
        """If parse_ticker returns None, _evaluate_entry should skip and not crash."""
        strategy = _make_strategy()

        with patch("weather_strategy.parse_ticker", return_value=None):
            strategy._evaluate_entry(_make_signal(ticker="GARBAGE-TICKER", p_win=0.99))

        strategy.submit_order.assert_not_called()

    def test_missing_settlement_date_skipped(self):
        """If parsed dict lacks settlement_date, entry is skipped."""
        strategy = _make_strategy()

        with patch("weather_strategy.parse_ticker", return_value={"threshold": 55, "series": "KXHIGHCHI"}):
            strategy._evaluate_entry(_make_signal(p_win=0.99))

        strategy.submit_order.assert_not_called()


# ---------------------------------------------------------------------------
# Fix 1: Danger exit cancels resting sells
# ---------------------------------------------------------------------------

class TestDangerExitCancelsRestingSells:
    def test_danger_exit_cancels_resting_sells(self):
        """CRITICAL exit should cancel resting sell orders before submitting emergency sell."""
        strategy = _make_strategy()
        ticker = "KXHIGHCHI-26MAR15-T55"
        strategy._positions_info[ticker] = {
            "side": "no", "threshold": 55.0, "city": "chicago", "contracts": 5,
        }
        mock_inst = _mock_instrument()
        strategy.cache.instrument.return_value = mock_inst
        strategy._latest_quotes["KXHIGHCHI-26MAR15-T55-NO.KALSHI"] = _mock_quote(88, 90)

        # Simulate a resting sell order from a prior buy fill
        resting_sell_id = MagicMock()
        strategy._resting_sells[ticker] = [resting_sell_id]

        mock_resting_sell = MagicMock()
        mock_resting_sell.client_order_id = resting_sell_id
        mock_resting_sell.side = OrderSide.SELL
        strategy.cache.orders_open.return_value = [mock_resting_sell]

        strategy._evaluate_exit(_make_alert(ticker=ticker, level="CRITICAL"))

        # Resting sell should have been cancelled
        strategy.cancel_order.assert_any_call(mock_resting_sell)
        # Emergency sell should still be submitted
        strategy.submit_order.assert_called_once()
        # Resting sells dict should be cleaned up
        assert ticker not in strategy._resting_sells


# ---------------------------------------------------------------------------
# Fix 3: Phase 1→2 fall-through guard
# ---------------------------------------------------------------------------

class TestPhase1Phase2Guard:
    def _setup_tomorrow_strategy(self, **kwargs):
        defaults = dict(
            open_spread_enabled=True,
            open_spread_prices_cents=(45, 50, 55),
            open_spread_size=3,
            open_spread_min_p_win=0.90,
            open_spread_window_minutes=30,
            stable_min_p_win=0.95,
        )
        defaults.update(kwargs)
        strategy = _make_strategy(**defaults)
        strategy.clock.utc_now.return_value = datetime(2026, 3, 14, 12, 0, tzinfo=timezone.utc)
        strategy.cache.instrument.return_value = _mock_instrument()
        strategy.clock.set_time_alert_ns = MagicMock()
        strategy.clock.timestamp_ns.return_value = 1_000_000_000
        return strategy

    def test_phase1_active_blocks_phase2(self):
        """Second signal during active Phase 1 window should NOT deploy Phase 2 ladder."""
        strategy = self._setup_tomorrow_strategy()
        quote_key = "KXHIGHCHI-26MAR15-T55-NO.KALSHI"
        strategy._latest_quotes[quote_key] = _mock_quote(85, 87)

        with patch("weather_strategy.parse_ticker") as mock_parse:
            mock_parse.return_value = {
                "settlement_date": "2026-03-15", "threshold": 55, "series": "KXHIGHCHI",
            }
            # First signal: deploys Phase 1 spread
            strategy._evaluate_entry(_make_signal(p_win=0.95, ts_event=1_000_000_000))
            first_count = strategy.submit_order.call_count
            assert first_count == 3  # 3 spread levels

            # Second signal: Phase 1 still active (timer hasn't fired)
            strategy._evaluate_entry(_make_signal(p_win=0.97, ts_event=2_000_000_000))
            second_count = strategy.submit_order.call_count

        # No additional orders — guard blocked Phase 2
        assert second_count == first_count
        # Ticker should NOT be in eligible_signals (Phase 2 never reached)
        assert "KXHIGHCHI-26MAR15-T55" not in strategy._eligible_signals

    def test_phase1_expired_allows_phase2(self):
        """After timer fires and clears spread orders, signal should proceed to Phase 2."""
        strategy = self._setup_tomorrow_strategy()
        quote_key = "KXHIGHCHI-26MAR15-T55-NO.KALSHI"
        strategy._latest_quotes[quote_key] = _mock_quote(85, 87)
        ticker = "KXHIGHCHI-26MAR15-T55"

        with patch("weather_strategy.parse_ticker") as mock_parse:
            mock_parse.return_value = {
                "settlement_date": "2026-03-15", "threshold": 55, "series": "KXHIGHCHI",
            }
            # First signal: deploys Phase 1
            strategy._evaluate_entry(_make_signal(p_win=0.95, ts_event=1_000_000_000))

            # Simulate timer firing: cancels spread orders (pops from _open_spread_orders)
            strategy._cancel_spread_orders(ticker)
            assert ticker not in strategy._open_spread_orders

            # Third signal after window: should proceed to Phase 2
            window_ns = 30 * 60 * 1_000_000_000
            late_ts = 1_000_000_000 + window_ns + 1
            strategy._evaluate_entry(_make_signal(p_win=0.97, ts_event=late_ts))

        # Phase 2 should have deployed (eligible signal stored)
        assert ticker in strategy._eligible_signals


# ---------------------------------------------------------------------------
# Fix 7: State cleanup on position close
# ---------------------------------------------------------------------------

class TestStateCleanupOnClose:
    def _make_fill_event(self, inst_str="KXHIGHCHI-26MAR15-T55-NO", qty=3, side=OrderSide.BUY):
        event = MagicMock()
        event.instrument_id = MagicMock()
        event.instrument_id.symbol.value = inst_str
        event.last_qty.as_double.return_value = float(qty)
        event.order_side = side
        return event

    def test_position_close_cleans_up_state(self):
        """Selling to 0 contracts should remove all tracking state for the ticker."""
        strategy = _make_strategy()
        strategy.cache.instrument.return_value = _mock_instrument()
        ticker = "KXHIGHCHI-26MAR15-T55"

        # Set up state that should be cleaned
        strategy._positions_info[ticker] = {"side": "no", "contracts": 3, "city": "", "threshold": 0}
        strategy._resting_sells[ticker] = [MagicMock()]
        strategy._eligible_signals[ticker] = _make_signal()
        strategy._danger_exited.add(ticker)
        strategy._open_spread_placed.add(ticker)
        strategy._first_tick_time[ticker] = 1_000_000_000
        strategy._last_ladder_bid[ticker] = 85

        # Sell all 3 contracts
        event = self._make_fill_event(qty=3, side=OrderSide.SELL)
        strategy.on_order_filled(event)

        # Everything should be cleaned up
        assert ticker not in strategy._positions_info
        assert ticker not in strategy._resting_sells
        assert ticker not in strategy._eligible_signals
        assert ticker not in strategy._danger_exited
        assert ticker not in strategy._open_spread_placed
        assert ticker not in strategy._first_tick_time
        assert ticker not in strategy._last_ladder_bid


# ---------------------------------------------------------------------------
# Capital Cap (max_total_deployed_cents)
# ---------------------------------------------------------------------------

class TestCapitalCap:
    def test_capital_cap_default_disabled(self):
        """Default config (max_total_deployed_cents=0) doesn't block orders."""
        strategy = _make_strategy(
            open_spread_enabled=False,
            stable_min_p_win=0.95,
            stable_ladder_offsets_cents=(0,),
            stable_size=2,
            # max_total_deployed_cents defaults to 0
        )
        strategy.clock.utc_now.return_value = datetime(2026, 3, 15, 15, 0, tzinfo=timezone.utc)
        strategy.cache.instrument.return_value = _mock_instrument()
        quote_key = "KXHIGHCHI-26MAR15-T55-NO.KALSHI"
        strategy._latest_quotes[quote_key] = _mock_quote(85, 87)

        with patch("weather_strategy.parse_ticker") as mock_parse:
            mock_parse.return_value = {"settlement_date": "2026-03-15", "threshold": 55, "series": "KXHIGHCHI"}
            strategy._evaluate_entry(_make_signal(p_win=0.97))

        strategy._on_refresh()
        strategy.submit_order.assert_called()

    def test_capital_cap_blocks_ladder_when_full(self):
        """With max_total_deployed_cents=200, positions using 200c+ blocks ladder."""
        strategy = _make_strategy(
            open_spread_enabled=False,
            stable_min_p_win=0.95,
            stable_ladder_offsets_cents=(0, 2),
            stable_size=2,
            max_cost_cents=92,
            max_total_deployed_cents=200,
        )
        strategy.clock.utc_now.return_value = datetime(2026, 3, 15, 15, 0, tzinfo=timezone.utc)
        strategy.cache.instrument.return_value = _mock_instrument()
        quote_key = "KXHIGHCHI-26MAR15-T55-NO.KALSHI"
        strategy._latest_quotes[quote_key] = _mock_quote(85, 87)

        # Existing positions that consume all budget: 3 contracts * 92c = 276c > 200c
        strategy._positions_info["KXHIGHCHI-26MAR14-T50"] = {
            "side": "no", "contracts": 3, "city": "chicago", "threshold": 50,
        }

        with patch("weather_strategy.parse_ticker") as mock_parse:
            mock_parse.return_value = {"settlement_date": "2026-03-15", "threshold": 55, "series": "KXHIGHCHI"}
            strategy._evaluate_entry(_make_signal(p_win=0.97))

        strategy._on_refresh()
        strategy.submit_order.assert_not_called()

    def test_capital_cap_blocks_spread_when_full(self):
        """With max_total_deployed_cents=200, positions using 200c+ blocks Phase 1 spread."""
        strategy = _make_strategy(
            open_spread_enabled=True,
            open_spread_prices_cents=(45, 50, 55),
            open_spread_size=3,
            open_spread_min_p_win=0.90,
            max_cost_cents=92,
            max_total_deployed_cents=200,
        )
        # Clock says today is 2026-03-14 → contract 2026-03-15 is tomorrow
        strategy.clock.utc_now.return_value = datetime(2026, 3, 14, 12, 0, tzinfo=timezone.utc)
        strategy.cache.instrument.return_value = _mock_instrument()
        strategy.clock.set_time_alert_ns = MagicMock()
        strategy.clock.timestamp_ns.return_value = 1_000_000_000

        # Existing positions consuming budget: 3 * 92 = 276c > 200c
        strategy._positions_info["KXHIGHCHI-26MAR14-T50"] = {
            "side": "no", "contracts": 3, "city": "chicago", "threshold": 50,
        }

        with patch("weather_strategy.parse_ticker") as mock_parse:
            mock_parse.return_value = {"settlement_date": "2026-03-15", "threshold": 55, "series": "KXHIGHCHI"}
            strategy._evaluate_entry(_make_signal(p_win=0.95, ts_event=1_000_000_000))

        strategy.submit_order.assert_not_called()

    def test_capital_cap_allows_within_budget(self):
        """With max_total_deployed_cents=500 and only 100c deployed, orders ARE placed."""
        strategy = _make_strategy(
            open_spread_enabled=False,
            stable_min_p_win=0.95,
            stable_ladder_offsets_cents=(0, 2),
            stable_size=2,
            max_cost_cents=92,
            max_total_deployed_cents=500,
        )
        strategy.clock.utc_now.return_value = datetime(2026, 3, 15, 15, 0, tzinfo=timezone.utc)
        strategy.cache.instrument.return_value = _mock_instrument()
        quote_key = "KXHIGHCHI-26MAR15-T55-NO.KALSHI"
        strategy._latest_quotes[quote_key] = _mock_quote(80, 82)

        # Only 1 contract at 92c = 92c deployed, well under 500c cap
        strategy._positions_info["KXHIGHCHI-26MAR14-T50"] = {
            "side": "no", "contracts": 1, "city": "chicago", "threshold": 50,
        }

        with patch("weather_strategy.parse_ticker") as mock_parse:
            mock_parse.return_value = {"settlement_date": "2026-03-15", "threshold": 55, "series": "KXHIGHCHI"}
            strategy._evaluate_entry(_make_signal(p_win=0.97))

        strategy._on_refresh()
        # Should have placed orders (2 offsets)
        assert strategy.submit_order.call_count == 2

    def test_capital_at_risk_counts_pending_buys(self):
        """_total_capital_at_risk sums pending BUY orders correctly."""
        strategy = _make_strategy(max_cost_cents=92, max_total_deployed_cents=1000)

        mock_order1 = MagicMock()
        mock_order1.side = OrderSide.BUY
        mock_order1.price.as_double.return_value = 0.85
        mock_order1.leaves_qty.as_double.return_value = 3.0

        mock_order2 = MagicMock()
        mock_order2.side = OrderSide.BUY
        mock_order2.price.as_double.return_value = 0.80
        mock_order2.leaves_qty.as_double.return_value = 2.0

        # SELL orders should be ignored
        mock_sell = MagicMock()
        mock_sell.side = OrderSide.SELL
        mock_sell.price.as_double.return_value = 0.97
        mock_sell.leaves_qty.as_double.return_value = 5.0

        strategy.cache.orders_open.return_value = [mock_order1, mock_order2, mock_sell]

        # No positions — only pending buys count
        result = strategy._total_capital_at_risk()
        # 85*3 + 80*2 = 255 + 160 = 415
        assert result == 415

    def test_capital_at_risk_counts_positions(self):
        """_total_capital_at_risk counts held positions at max_cost_cents."""
        strategy = _make_strategy(max_cost_cents=92, max_total_deployed_cents=1000)

        # No open orders
        strategy.cache.orders_open.return_value = []

        # Two held positions
        strategy._positions_info["KXHIGHCHI-26MAR15-T55"] = {
            "side": "no", "contracts": 5, "city": "chicago", "threshold": 55,
        }
        strategy._positions_info["KXHIGHCHI-26MAR15-T60"] = {
            "side": "no", "contracts": 3, "city": "chicago", "threshold": 60,
        }

        result = strategy._total_capital_at_risk()
        # 5*92 + 3*92 = 460 + 276 = 736
        assert result == 736

    def test_capital_at_risk_combined(self):
        """_total_capital_at_risk sums both pending orders and held positions."""
        strategy = _make_strategy(max_cost_cents=92, max_total_deployed_cents=2000)

        mock_order = MagicMock()
        mock_order.side = OrderSide.BUY
        mock_order.price.as_double.return_value = 0.80
        mock_order.leaves_qty.as_double.return_value = 2.0
        strategy.cache.orders_open.return_value = [mock_order]

        strategy._positions_info["KXHIGHCHI-26MAR15-T55"] = {
            "side": "no", "contracts": 3, "city": "chicago", "threshold": 55,
        }

        result = strategy._total_capital_at_risk()
        # Orders: 80*2 = 160. Positions: 3*92 = 276. Total = 436
        assert result == 436

    def test_capital_cap_ladder_partial_fill(self):
        """Capital cap truncates ladder mid-loop when budget runs out."""
        strategy = _make_strategy(
            open_spread_enabled=False,
            stable_min_p_win=0.95,
            stable_ladder_offsets_cents=(0, 2, 5),
            stable_size=2,
            max_cost_cents=92,
            max_total_deployed_cents=300,
        )
        strategy.clock.utc_now.return_value = datetime(2026, 3, 15, 15, 0, tzinfo=timezone.utc)
        strategy.cache.instrument.return_value = _mock_instrument()
        quote_key = "KXHIGHCHI-26MAR15-T55-NO.KALSHI"
        strategy._latest_quotes[quote_key] = _mock_quote(80, 82)

        # No pending orders, no positions → 0c at risk
        # Budget = 300c. Ladder offsets: 80*2=160c, 78*2=156c, 75*2=150c
        # First rung: 160c → remaining 140c. Second rung: 156c > 140c → break.
        with patch("weather_strategy.parse_ticker") as mock_parse:
            mock_parse.return_value = {"settlement_date": "2026-03-15", "threshold": 55, "series": "KXHIGHCHI"}
            strategy._evaluate_entry(_make_signal(p_win=0.97))

        strategy._on_refresh()
        # Only 1 rung should place (160c fits in 300c, 156c doesn't fit in 140c)
        assert strategy.submit_order.call_count == 1
