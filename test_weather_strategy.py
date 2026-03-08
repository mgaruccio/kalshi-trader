"""Tests for WeatherStrategy.

Run: pytest test_weather_strategy.py --noconftest -v

Bind WeatherStrategy's methods onto a pure Python stand-in to bypass
NT's Cython read-only descriptors (log, clock, cache, etc.).
"""
import pytest
from unittest.mock import MagicMock

from data_types import ModelSignal, DangerAlert
from weather_strategy import WeatherStrategy, WeatherStrategyConfig


class _TestableWeatherStrategy:
    """Pure Python stand-in for WeatherStrategy with mockable NT attributes."""

    def __init__(self, config=None):
        self._cfg = config or WeatherStrategyConfig()
        self._latest_quotes: dict = {}
        self._positions_info: dict = {}
        self._danger_exited: set = set()
        self._feature_actor = None
        self._signals_received: int = 0
        self._alerts_received: int = 0
        self.log = MagicMock()
        self.cache = MagicMock()
        self.order_factory = MagicMock()
        self.submit_order = MagicMock()

    # Bind actual methods from WeatherStrategy
    on_data = WeatherStrategy.on_data
    _evaluate_entry = WeatherStrategy._evaluate_entry
    _evaluate_exit = WeatherStrategy._evaluate_exit
    _check_profit_targets = WeatherStrategy._check_profit_targets
    set_feature_actor = WeatherStrategy.set_feature_actor
    _sync_positions_to_actor = WeatherStrategy._sync_positions_to_actor

    @property
    def signals_received(self) -> int:
        return self._signals_received

    @property
    def alerts_received(self) -> int:
        return self._alerts_received


class TestWeatherStrategyLogic:
    """Test WeatherStrategy core logic without full NT Strategy lifecycle."""

    def _make_strategy(self, **config_kwargs) -> _TestableWeatherStrategy:
        defaults = dict(min_p_win=0.95, max_cost_cents=92, sell_target_cents=97)
        defaults.update(config_kwargs)
        config = WeatherStrategyConfig(**defaults)
        return _TestableWeatherStrategy(config)

    def test_signal_below_threshold_ignored(self):
        """ModelSignal with p_win below min_p_win should not trigger entry."""
        strategy = self._make_strategy()

        signal = ModelSignal(
            city="chicago", ticker="T55", side="no",
            p_win=0.90,  # below 0.95 threshold
            model_scores={}, features_snapshot={},
            ts_event=0, ts_init=0,
        )
        strategy._evaluate_entry(signal)
        strategy.submit_order.assert_not_called()

    def test_signal_above_threshold_with_quote(self):
        """ModelSignal with p_win above threshold should evaluate for entry."""
        strategy = self._make_strategy()

        # Mock a quote
        mock_tick = MagicMock()
        mock_tick.ask_price.as_double.return_value = 0.90  # 90c
        mock_tick.bid_price.as_double.return_value = 0.88
        strategy._latest_quotes["T55-NO.KALSHI"] = mock_tick

        # Mock instrument
        mock_inst = MagicMock()
        strategy.cache.instrument.return_value = mock_inst

        signal = ModelSignal(
            city="chicago", ticker="T55", side="no",
            p_win=0.97,  # above threshold
            model_scores={}, features_snapshot={},
            ts_event=0, ts_init=0,
        )
        strategy._evaluate_entry(signal)
        strategy.submit_order.assert_called_once()

    def test_signal_too_expensive(self):
        """Should not enter when ask > max_cost_cents."""
        strategy = self._make_strategy()

        mock_tick = MagicMock()
        mock_tick.ask_price.as_double.return_value = 0.95  # 95c > 92c max
        strategy._latest_quotes["T55-NO.KALSHI"] = mock_tick

        signal = ModelSignal(
            city="chicago", ticker="T55", side="no",
            p_win=0.97, model_scores={}, features_snapshot={},
            ts_event=0, ts_init=0,
        )
        strategy._evaluate_entry(signal)
        strategy.submit_order.assert_not_called()

    def test_signal_no_quote_skipped(self):
        """Should skip entry when no quote is available."""
        strategy = self._make_strategy()

        signal = ModelSignal(
            city="chicago", ticker="T55", side="no",
            p_win=0.97, model_scores={}, features_snapshot={},
            ts_event=0, ts_init=0,
        )
        strategy._evaluate_entry(signal)
        strategy.submit_order.assert_not_called()

    def test_signal_max_position_blocks_entry(self):
        """Should not enter when at max position."""
        strategy = self._make_strategy()
        strategy._positions_info = {
            "T55": {"side": "no", "city": "chicago", "contracts": 20}
        }

        mock_tick = MagicMock()
        mock_tick.ask_price.as_double.return_value = 0.90
        strategy._latest_quotes["T55-NO.KALSHI"] = mock_tick

        signal = ModelSignal(
            city="chicago", ticker="T55", side="no",
            p_win=0.97, model_scores={}, features_snapshot={},
            ts_event=0, ts_init=0,
        )
        strategy._evaluate_entry(signal)
        strategy.submit_order.assert_not_called()

    def test_danger_exit_critical(self):
        """CRITICAL DangerAlert should trigger sell."""
        strategy = self._make_strategy()
        strategy._positions_info = {
            "T55": {"side": "no", "threshold": 55.0, "city": "chicago", "contracts": 5}
        }

        mock_inst = MagicMock()
        strategy.cache.instrument.return_value = mock_inst

        mock_tick = MagicMock()
        strategy._latest_quotes["T55-NO.KALSHI"] = mock_tick

        alert = DangerAlert(
            ticker="T55", city="chicago",
            alert_level="CRITICAL",
            rule_name="obs_approaching_threshold",
            reason="Obs 54.5F within 1F of T55",
            features={"obs_temp": 54.5},
            ts_event=0, ts_init=0,
        )
        strategy._evaluate_exit(alert)
        strategy.submit_order.assert_called_once()
        assert "T55" in strategy._danger_exited

    def test_danger_exit_not_critical_no_sell(self):
        """Non-CRITICAL alerts should log but not sell."""
        strategy = self._make_strategy()
        strategy._positions_info = {"T55": {"side": "no", "city": "chicago"}}

        alert = DangerAlert(
            ticker="T55", city="chicago",
            alert_level="CAUTION",
            rule_name="sst_contrast",
            reason="test",
            features={},
            ts_event=0, ts_init=0,
        )
        strategy._evaluate_exit(alert)
        strategy.submit_order.assert_not_called()

    def test_danger_exit_no_retry(self):
        """Should not re-sell a ticker already danger-exited."""
        strategy = self._make_strategy()
        strategy._danger_exited.add("T55")
        strategy._positions_info = {"T55": {"side": "no", "city": "chicago"}}

        alert = DangerAlert(
            ticker="T55", city="chicago", alert_level="CRITICAL",
            rule_name="obs", reason="test", features={},
            ts_event=0, ts_init=0,
        )
        strategy._evaluate_exit(alert)
        strategy.submit_order.assert_not_called()

    def test_danger_exited_blocks_entry(self):
        """Should not re-enter a danger-exited ticker."""
        strategy = self._make_strategy()
        strategy._danger_exited.add("T55")

        signal = ModelSignal(
            city="chicago", ticker="T55", side="no",
            p_win=0.99, model_scores={}, features_snapshot={},
            ts_event=0, ts_init=0,
        )
        strategy._evaluate_entry(signal)
        strategy.submit_order.assert_not_called()

    def test_danger_exit_disabled(self):
        """No sell when danger_exit_enabled is False."""
        strategy = self._make_strategy(danger_exit_enabled=False)
        strategy._positions_info = {
            "T55": {"side": "no", "city": "chicago", "contracts": 5}
        }

        alert = DangerAlert(
            ticker="T55", city="chicago", alert_level="CRITICAL",
            rule_name="obs", reason="test", features={},
            ts_event=0, ts_init=0,
        )
        strategy._evaluate_exit(alert)
        strategy.submit_order.assert_not_called()

    def test_danger_exit_no_position_no_sell(self):
        """No sell when we don't hold the ticker."""
        strategy = self._make_strategy()
        strategy._positions_info = {}  # no position for T55

        alert = DangerAlert(
            ticker="T55", city="chicago", alert_level="CRITICAL",
            rule_name="obs", reason="test", features={},
            ts_event=0, ts_init=0,
        )
        strategy._evaluate_exit(alert)
        strategy.submit_order.assert_not_called()

    def test_danger_exit_fallback_price(self):
        """Should use fallback price when no quote is available."""
        strategy = self._make_strategy()
        strategy._positions_info = {
            "T55": {"side": "no", "threshold": 55.0, "city": "chicago", "contracts": 3}
        }

        mock_inst = MagicMock()
        strategy.cache.instrument.return_value = mock_inst
        # No quote in _latest_quotes

        alert = DangerAlert(
            ticker="T55", city="chicago", alert_level="CRITICAL",
            rule_name="obs", reason="test", features={},
            ts_event=0, ts_init=0,
        )
        strategy._evaluate_exit(alert)
        strategy.submit_order.assert_called_once()
        # Should have used instrument.make_price(0.01) as fallback
        mock_inst.make_price.assert_called_with(0.01)

    def test_on_data_routes_signals(self):
        """on_data should route to correct handler and count."""
        strategy = self._make_strategy()

        signal = ModelSignal("chi", "T55", "no", 0.90, {}, {}, 0, 0)
        strategy.on_data(signal)
        assert strategy.signals_received == 1

        alert = DangerAlert("T55", "chi", "WATCH", "test", "r", {}, 0, 0)
        strategy.on_data(alert)
        assert strategy.alerts_received == 1

    def test_set_feature_actor(self):
        """set_feature_actor should wire the reference."""
        strategy = self._make_strategy()
        mock_actor = MagicMock()
        strategy.set_feature_actor(mock_actor)
        assert strategy._feature_actor is mock_actor

    def test_sync_positions_to_actor(self):
        """_sync_positions_to_actor should push positions to the actor."""
        strategy = self._make_strategy()
        mock_actor = MagicMock()
        strategy._feature_actor = mock_actor
        strategy._positions_info = {"T55": {"side": "no", "city": "chicago"}}

        strategy._sync_positions_to_actor()
        mock_actor.update_positions.assert_called_once_with(strategy._positions_info)

    def test_sync_positions_no_actor(self):
        """_sync_positions_to_actor should be a no-op without an actor."""
        strategy = self._make_strategy()
        strategy._feature_actor = None
        # Should not raise
        strategy._sync_positions_to_actor()

    def test_properties(self):
        """Property accessors should reflect internal state."""
        strategy = self._make_strategy()
        assert strategy.signals_received == 0
        assert strategy.alerts_received == 0

        strategy._signals_received = 5
        strategy._alerts_received = 3
        assert strategy.signals_received == 5
        assert strategy.alerts_received == 3

    def test_entry_instrument_not_in_cache(self):
        """Should not crash when instrument is not in cache."""
        strategy = self._make_strategy()

        mock_tick = MagicMock()
        mock_tick.ask_price.as_double.return_value = 0.90
        strategy._latest_quotes["T55-NO.KALSHI"] = mock_tick

        strategy.cache.instrument.return_value = None  # not in cache

        signal = ModelSignal(
            city="chicago", ticker="T55", side="no",
            p_win=0.97, model_scores={}, features_snapshot={},
            ts_event=0, ts_init=0,
        )
        strategy._evaluate_entry(signal)
        strategy.submit_order.assert_not_called()
