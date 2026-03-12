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
        self._ladder_deployed: set = set()
        self._ticks_since_refresh: int = 0
        # Counters
        self._signals_received: int = 0
        self._alerts_received: int = 0
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
        """Old config fields (min_p_win, max_cost_cents, trade_size) must not exist."""
        cfg = WeatherStrategyConfig()
        assert not hasattr(cfg, "min_p_win")
        assert not hasattr(cfg, "max_cost_cents")
        assert not hasattr(cfg, "trade_size")


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
        # Set clock to today (contract settles today)
        strategy.clock.utc_now.return_value = datetime(2026, 3, 15, 10, 0, tzinfo=timezone.utc)

        quote_key = "KXHIGHCHI-26MAR15-T55-NO.KALSHI"
        strategy._latest_quotes[quote_key] = _mock_quote(bid_cents=85, ask_cents=87)
        strategy.cache.instrument.return_value = _mock_instrument()

        with patch("weather_strategy.parse_ticker") as mock_parse:
            mock_parse.return_value = {"settlement_date": "2026-03-15", "threshold": 55, "series": "KXHIGHCHI"}
            strategy._evaluate_entry(_make_signal(p_win=0.97))

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

        strategy.clock.utc_now.return_value = datetime(2026, 3, 15, 10, 0, tzinfo=timezone.utc)
        quote_key = "KXHIGHCHI-26MAR15-T55-NO.KALSHI"
        strategy._latest_quotes[quote_key] = _mock_quote(85, 87)
        strategy.cache.instrument.return_value = _mock_instrument()

        with patch("weather_strategy.parse_ticker") as mock_parse:
            mock_parse.return_value = {"settlement_date": "2026-03-15", "threshold": 55, "series": "KXHIGHCHI"}
            strategy._evaluate_entry(_make_signal(p_win=0.97))

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

        # Should fall through to Phase 2 ladder (not Phase 1 spread)
        assert "KXHIGHCHI-26MAR15-T55" not in strategy._open_spread_placed
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

        # Should use Phase 2 instead
        assert "KXHIGHCHI-26MAR15-T55" not in strategy._open_spread_placed
        strategy.submit_order.assert_called()

    def test_phase1_today_contract_skips_to_phase2(self):
        """Today contract should go directly to Phase 2, not Phase 1."""
        strategy = _make_strategy(
            open_spread_enabled=True,
            stable_min_p_win=0.95,
        )
        # Clock says today is 2026-03-15 — same as settlement date
        strategy.clock.utc_now.return_value = datetime(2026, 3, 15, 10, 0, tzinfo=timezone.utc)
        quote_key = "KXHIGHCHI-26MAR15-T55-NO.KALSHI"
        strategy._latest_quotes[quote_key] = _mock_quote(85, 87)
        strategy.cache.instrument.return_value = _mock_instrument()

        with patch("weather_strategy.parse_ticker") as mock_parse:
            mock_parse.return_value = {"settlement_date": "2026-03-15", "threshold": 55, "series": "KXHIGHCHI"}
            strategy._evaluate_entry(_make_signal(p_win=0.97))

        assert "KXHIGHCHI-26MAR15-T55" not in strategy._open_spread_placed
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
        strategy.clock.utc_now.return_value = datetime(2026, 3, 15, 10, 0, tzinfo=timezone.utc)
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
        strategy.clock.utc_now.return_value = datetime(2026, 3, 15, 10, 0, tzinfo=timezone.utc)
        strategy.cache.instrument.return_value = _mock_instrument()
        quote_key = "KXHIGHCHI-26MAR15-T55-NO.KALSHI"
        strategy._latest_quotes[quote_key] = _mock_quote(85, 87)

        # Simulate 4 contracts already in a pending buy order for this instrument
        # instrument_id.value must equal the string the production code compares against
        mock_open_order = MagicMock()
        mock_open_order.instrument_id.value = "KXHIGHCHI-26MAR15-T55-NO.KALSHI"
        mock_open_order.side = OrderSide.BUY
        mock_open_order.quantity.as_double.return_value = 4.0
        strategy.cache.orders_open.return_value = [mock_open_order]

        with patch("weather_strategy.parse_ticker") as mock_parse:
            mock_parse.return_value = {"settlement_date": "2026-03-15", "threshold": 55, "series": "KXHIGHCHI"}
            strategy._evaluate_entry(_make_signal(p_win=0.97))

        # With 4 pending and max 6, only 2 contracts of capacity remain.
        # stable_size=5 per level but capacity=2, so only 1 ladder level fits (size=2).
        orders_placed = strategy.submit_order.call_count
        assert orders_placed <= 2  # capped by remaining capacity


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
        # Simulate ticks having arrived so refresh doesn't skip
        strategy._ticks_since_refresh = 1
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
        strategy._ticks_since_refresh = 1

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
