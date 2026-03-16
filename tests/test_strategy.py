"""Tests for WeatherMakerStrategy filter layer, quote ladder, and risk caps."""
import pytest

from kalshi.signals import SignalScore
from kalshi.strategy import WeatherMakerConfig, should_quote, compute_ladder, check_risk_caps, compute_cap_multiplier
from kalshi.signal_actor import parse_score_msg


def _make_score(
    ticker: str = "KXHIGHNY-26MAR15-T54",
    city: str = "new_york",
    no_p_win: float = 0.98,
    yes_p_win: float = 0.02,
    n_models: int = 3,
    **kwargs,
) -> SignalScore:
    defaults = dict(
        threshold=54.0,
        direction="above",
        no_margin=7.0,
        emos_no=0.95,
        ngboost_no=0.99,
        drn_no=0.98,
        yes_bid=85,
        yes_ask=90,
        status="active",
        nws_max=0.0,
        ts_event=0,
        ts_init=0,
    )
    defaults.update(kwargs)
    return SignalScore(
        ticker=ticker,
        city=city,
        no_p_win=no_p_win,
        yes_p_win=yes_p_win,
        n_models=n_models,
        **defaults,
    )


class TestWeatherMakerConfig:
    def test_defaults(self):
        cfg = WeatherMakerConfig()
        assert cfg.confidence_threshold == 0.95
        assert cfg.min_models == 2
        assert cfg.max_model_spread == 0.15
        assert cfg.ladder_depth == 3
        assert cfg.ladder_spacing == 1
        assert cfg.level_quantity == 10
        assert cfg.reprice_threshold == 2
        assert cfg.market_cap_pct == 0.20
        assert cfg.city_cap_pct == 0.33
        assert cfg.exit_price_cents == 97
        assert cfg.max_drawdown_pct == 0.15
        assert cfg.halt_file_path == "/tmp/kalshi-halt"
        assert cfg.max_entry_cents == 96

    def test_custom_values(self):
        cfg = WeatherMakerConfig(confidence_threshold=0.99, min_models=3)
        assert cfg.confidence_threshold == 0.99
        assert cfg.min_models == 3

    def test_nws_config_defaults(self):
        cfg = WeatherMakerConfig()
        assert cfg.min_nws_margin == 2.0
        assert cfg.max_nws_model_divergence == 5.0
        assert cfg.nws_missing_cap_multiplier == 0.5
        assert cfg.low_price_threshold_cents == 75
        assert cfg.low_price_cap_multiplier == 0.5


class TestFilterLayer:
    def test_high_confidence_no_passes(self):
        cfg = WeatherMakerConfig(confidence_threshold=0.95, min_models=2)
        score = _make_score(no_p_win=0.98, n_models=3)
        side, passes = should_quote(cfg, score, drift_cities=set())
        assert passes is True
        assert side == "no"

    def test_high_confidence_yes_passes(self):
        cfg = WeatherMakerConfig(confidence_threshold=0.95, min_models=2)
        score = _make_score(
            no_p_win=0.02, yes_p_win=0.98, n_models=3,
            emos_no=0.02, ngboost_no=0.02, drn_no=0.02,
        )
        side, passes = should_quote(cfg, score, drift_cities=set())
        assert passes is True
        assert side == "yes"

    def test_below_threshold_fails(self):
        cfg = WeatherMakerConfig(confidence_threshold=0.95, min_models=2)
        score = _make_score(no_p_win=0.90, yes_p_win=0.10, n_models=3)
        _, passes = should_quote(cfg, score, drift_cities=set())
        assert passes is False

    def test_insufficient_models_fails(self):
        cfg = WeatherMakerConfig(confidence_threshold=0.95, min_models=2)
        score = _make_score(no_p_win=0.98, n_models=1)
        _, passes = should_quote(cfg, score, drift_cities=set())
        assert passes is False

    def test_drift_pause_excludes_city(self):
        cfg = WeatherMakerConfig(confidence_threshold=0.95, min_models=2)
        score = _make_score(no_p_win=0.98, n_models=3, city="new_york")
        _, passes = should_quote(cfg, score, drift_cities={"new_york"})
        assert passes is False

    def test_drift_does_not_affect_other_city(self):
        cfg = WeatherMakerConfig(confidence_threshold=0.95, min_models=2)
        score = _make_score(no_p_win=0.98, n_models=3, city="chicago")
        _, passes = should_quote(cfg, score, drift_cities={"new_york"})
        assert passes is True

    def test_neither_side_above_threshold(self):
        """Both sides below threshold — ambiguous market, don't quote."""
        cfg = WeatherMakerConfig(confidence_threshold=0.95, min_models=2)
        score = _make_score(no_p_win=0.55, yes_p_win=0.45, n_models=3)
        _, passes = should_quote(cfg, score, drift_cities=set())
        assert passes is False

    def test_status_not_open_fails(self):
        """Non-open contracts are not quoted."""
        cfg = WeatherMakerConfig(confidence_threshold=0.95, min_models=2)
        score = _make_score(no_p_win=0.98, n_models=3, status="closed")
        _, passes = should_quote(cfg, score, drift_cities=set())
        assert passes is False

    def test_status_open_passes(self):
        cfg = WeatherMakerConfig(confidence_threshold=0.95, min_models=2)
        score = _make_score(no_p_win=0.98, n_models=3, status="open")
        _, passes = should_quote(cfg, score, drift_cities=set())
        assert passes is True

    def test_active_status_passes(self):
        """Signal server returns 'active' — must be accepted by filter."""
        cfg = WeatherMakerConfig(confidence_threshold=0.95, min_models=2)
        score = _make_score(no_p_win=0.98, n_models=3, status="active")
        _, passes = should_quote(cfg, score, drift_cities=set())
        assert passes is True

    def test_closed_status_fails(self):
        """Closed contracts must be rejected by filter."""
        cfg = WeatherMakerConfig(confidence_threshold=0.95, min_models=2)
        score = _make_score(no_p_win=0.98, n_models=3, status="closed")
        _, passes = should_quote(cfg, score, drift_cities=set())
        assert passes is False

    def test_model_spread_too_large_fails(self):
        """If per-model probabilities diverge too much, skip."""
        cfg = WeatherMakerConfig(confidence_threshold=0.95, min_models=2, max_model_spread=0.15)
        # emos=0.98, drn=0.80 => spread=0.18 > 0.15
        score = _make_score(
            no_p_win=0.98, n_models=3,
            emos_no=0.98, ngboost_no=0.95, drn_no=0.80,
        )
        _, passes = should_quote(cfg, score, drift_cities=set())
        assert passes is False

    def test_model_spread_within_limit_passes(self):
        cfg = WeatherMakerConfig(confidence_threshold=0.95, min_models=2, max_model_spread=0.15)
        score = _make_score(
            no_p_win=0.98, n_models=3,
            emos_no=0.98, ngboost_no=0.96, drn_no=0.95,
        )
        _, passes = should_quote(cfg, score, drift_cities=set())
        assert passes is True

    def test_model_spread_ignores_zero_models(self):
        """Missing models (0.0) are excluded from spread calculation."""
        cfg = WeatherMakerConfig(confidence_threshold=0.95, min_models=1, max_model_spread=0.15)
        # Only emos present — spread among non-zero is 0
        score = _make_score(
            no_p_win=0.98, n_models=1,
            emos_no=0.98, ngboost_no=0.0, drn_no=0.0,
        )
        _, passes = should_quote(cfg, score, drift_cities=set())
        assert passes is True

    def test_anchor_above_max_entry_fails(self):
        """YES anchor above max_entry_cents is rejected."""
        cfg = WeatherMakerConfig(confidence_threshold=0.95, min_models=2, max_entry_cents=96)
        # yes_p_win is winning side, yes_bid=97 > 96
        score = _make_score(
            no_p_win=0.02, yes_p_win=0.98, n_models=3,
            emos_no=0.02, ngboost_no=0.02, drn_no=0.02,
            yes_bid=97, yes_ask=98,
        )
        _, passes = should_quote(cfg, score, drift_cities=set())
        assert passes is False


class TestComputeLadder:
    def test_basic_ladder(self):
        """3 levels, spacing 1c, anchor at 92c."""
        levels = compute_ladder(anchor_bid_cents=92, depth=3, spacing=1, qty=10)
        assert len(levels) == 3
        assert levels[0] == (92, 10)
        assert levels[1] == (91, 10)
        assert levels[2] == (90, 10)

    def test_anchor_at_minimum(self):
        """Anchor at 1c — only one level possible."""
        levels = compute_ladder(anchor_bid_cents=1, depth=3, spacing=1, qty=10)
        assert len(levels) == 1
        assert levels[0] == (1, 10)

    def test_anchor_near_minimum(self):
        """Anchor at 3c with depth 5 — truncates to valid levels only."""
        levels = compute_ladder(anchor_bid_cents=3, depth=5, spacing=1, qty=10)
        assert len(levels) == 3
        assert levels[-1] == (1, 10)

    def test_spacing_2(self):
        levels = compute_ladder(anchor_bid_cents=95, depth=3, spacing=2, qty=5)
        assert levels[0] == (95, 5)
        assert levels[1] == (93, 5)
        assert levels[2] == (91, 5)

    def test_zero_anchor_returns_empty(self):
        levels = compute_ladder(anchor_bid_cents=0, depth=3, spacing=1, qty=10)
        assert levels == []

    def test_anchor_at_99(self):
        """Anchor at max valid price."""
        levels = compute_ladder(anchor_bid_cents=99, depth=3, spacing=1, qty=10)
        assert levels[0] == (99, 10)
        assert len(levels) == 3


class TestCheckRiskCaps:
    """check_risk_caps takes pre-computed exposure values derived from NT cache."""

    def test_within_caps_full_quantity_allowed(self):
        allowed = check_risk_caps(
            market_exposure_cents=0,
            city_exposure_cents=0,
            quantity=10,
            price_cents=90,
            account_balance_cents=100_000,
            market_cap_pct=0.20,
            city_cap_pct=0.33,
        )
        assert allowed == 10

    def test_market_cap_reduces_quantity(self):
        # Market cap = 20,000. Exposed = 166*90 = 14,940. Remaining = 5,060.
        # floor(5060 / 90) = 56 contracts.
        allowed = check_risk_caps(
            market_exposure_cents=166 * 90,
            city_exposure_cents=166 * 90,
            quantity=100,
            price_cents=90,
            account_balance_cents=100_000,
            market_cap_pct=0.20,
            city_cap_pct=0.33,
        )
        assert allowed == 56

    def test_city_cap_reduces_quantity(self):
        # City cap = 33,000. Exposed = 18,000 (200*90). Remaining = 15,000.
        # floor(15000 / 90) = 166. Market cap = 20,000, market exposed = 0 → 222.
        # City cap is binding: allowed = 166.
        allowed = check_risk_caps(
            market_exposure_cents=0,
            city_exposure_cents=100 * 90 + 100 * 90,
            quantity=200,
            price_cents=90,
            account_balance_cents=100_000,
            market_cap_pct=0.20,
            city_cap_pct=0.33,
        )
        assert allowed == 166

    def test_zero_balance_allows_nothing(self):
        allowed = check_risk_caps(
            market_exposure_cents=0,
            city_exposure_cents=0,
            quantity=10,
            price_cents=90,
            account_balance_cents=0,
            market_cap_pct=0.20,
            city_cap_pct=0.33,
        )
        assert allowed == 0

    def test_market_cap_fully_consumed(self):
        # Market exposure already at cap — no room.
        market_cap = int(100_000 * 0.20)  # 20,000
        allowed = check_risk_caps(
            market_exposure_cents=market_cap,
            city_exposure_cents=market_cap,
            quantity=10,
            price_cents=90,
            account_balance_cents=100_000,
            market_cap_pct=0.20,
            city_cap_pct=0.33,
        )
        assert allowed == 0

    def test_city_cap_fully_consumed(self):
        city_cap = int(100_000 * 0.33)  # 33,000
        allowed = check_risk_caps(
            market_exposure_cents=0,
            city_exposure_cents=city_cap,
            quantity=10,
            price_cents=90,
            account_balance_cents=100_000,
            market_cap_pct=0.20,
            city_cap_pct=0.33,
        )
        assert allowed == 0

    def test_quantity_limited_by_stricter_cap(self):
        """Market cap is tighter than city cap — market cap applies."""
        # Market: 18,000 exposed of 20,000 cap → 2,000 remaining → 22 contracts at 90c
        # City: 0 exposed of 33,000 cap → 33,000 remaining → 366 contracts
        allowed = check_risk_caps(
            market_exposure_cents=18_000,
            city_exposure_cents=18_000,
            quantity=100,
            price_cents=90,
            account_balance_cents=100_000,
            market_cap_pct=0.20,
            city_cap_pct=0.33,
        )
        assert allowed == 22  # floor(2000/90) = 22


# ---------------------------------------------------------------------------
# Task 1: nws_max field on SignalScore + parse_score_msg
# ---------------------------------------------------------------------------

class TestSignalScoreNwsMax:
    def test_signal_score_has_nws_max(self):
        score = _make_score(nws_max=70.0)
        assert score.nws_max == 70.0

    def test_signal_score_nws_max_defaults_to_zero(self):
        score = _make_score()
        assert score.nws_max == 0.0


class TestParseScoreMsgNwsMax:
    _base_msg = dict(
        ticker="KXHIGHTDC-26MAR16-T69",
        city="washington_dc",
        threshold=69.0,
        direction="above",
        no_p_win=0.98,
        yes_p_win=0.02,
        no_margin=5.0,
        n_models=3,
        yes_bid=85,
    )

    def test_parse_score_msg_with_nws_max(self):
        msg = {**self._base_msg, "nws_max": 70.0}
        score = parse_score_msg(msg, ts_ns=0)
        assert score is not None
        assert score.nws_max == 70.0

    def test_parse_score_msg_nws_max_null(self):
        msg = {**self._base_msg, "nws_max": None}
        score = parse_score_msg(msg, ts_ns=0)
        assert score is not None
        assert score.nws_max == 0.0

    def test_parse_score_msg_nws_max_missing(self):
        msg = {**self._base_msg}
        score = parse_score_msg(msg, ts_ns=0)
        assert score is not None
        assert score.nws_max == 0.0


# ---------------------------------------------------------------------------
# Task 3: NWS hard filters in should_quote()
# ---------------------------------------------------------------------------

class TestNwsFiltersInShouldQuote:
    """NWS margin and divergence filters — only active when nws_max > 0."""

    def _cfg(self, **kwargs) -> WeatherMakerConfig:
        defaults = dict(
            confidence_threshold=0.95,
            min_models=2,
            min_nws_margin=2.0,
            max_nws_model_divergence=5.0,
        )
        defaults.update(kwargs)
        return WeatherMakerConfig(**defaults)

    def test_above_tight_nws_margin_fails(self):
        """above: nws_max=70.5, threshold=69, margin=1.5 < 2.0 → reject."""
        cfg = self._cfg()
        score = _make_score(
            no_p_win=0.98, n_models=3, direction="above",
            threshold=69.0, no_margin=5.0, nws_max=70.5,
        )
        _, passes = should_quote(cfg, score, drift_cities=set())
        assert passes is False

    def test_above_sufficient_nws_margin_passes(self):
        """above: nws_max=72, threshold=69, margin=3.0 >= 2.0 → allow."""
        cfg = self._cfg()
        score = _make_score(
            no_p_win=0.98, n_models=3, direction="above",
            threshold=69.0, no_margin=5.0, nws_max=72.0,
        )
        _, passes = should_quote(cfg, score, drift_cities=set())
        assert passes is True

    def test_below_tight_nws_margin_fails(self):
        """below: nws_max=67.5, threshold=69, margin=1.5 < 2.0 → reject."""
        cfg = self._cfg()
        score = _make_score(
            no_p_win=0.98, n_models=3, direction="below",
            threshold=69.0, no_margin=5.0, nws_max=67.5,
            emos_no=0.95, ngboost_no=0.99, drn_no=0.98,
        )
        _, passes = should_quote(cfg, score, drift_cities=set())
        assert passes is False

    def test_below_sufficient_nws_margin_passes(self):
        """below: nws_max=66, threshold=69, margin=3.0 >= 2.0 → allow."""
        cfg = self._cfg()
        score = _make_score(
            no_p_win=0.98, n_models=3, direction="below",
            threshold=69.0, no_margin=5.0, nws_max=66.0,
            emos_no=0.95, ngboost_no=0.99, drn_no=0.98,
        )
        _, passes = should_quote(cfg, score, drift_cities=set())
        assert passes is True

    def test_nws_model_divergence_rejects(self):
        """above: nws_max=80, consensus=74 (69+5), divergence=6 > 5.0 → reject."""
        cfg = self._cfg(max_nws_model_divergence=5.0)
        score = _make_score(
            no_p_win=0.98, n_models=3, direction="above",
            threshold=69.0, no_margin=5.0, nws_max=80.0,
        )
        _, passes = should_quote(cfg, score, drift_cities=set())
        assert passes is False

    def test_nws_model_divergence_passes(self):
        """above: nws_max=76, consensus=74 (69+5), divergence=2 <= 5.0 → allow."""
        cfg = self._cfg(max_nws_model_divergence=5.0)
        score = _make_score(
            no_p_win=0.98, n_models=3, direction="above",
            threshold=69.0, no_margin=5.0, nws_max=76.0,
        )
        _, passes = should_quote(cfg, score, drift_cities=set())
        assert passes is True

    def test_nws_zero_skips_filter(self):
        """nws_max=0.0 means unavailable — NWS filters must be skipped."""
        cfg = self._cfg(min_nws_margin=2.0)
        score = _make_score(
            no_p_win=0.98, n_models=3, direction="above",
            threshold=69.0, no_margin=5.0, nws_max=0.0,
        )
        _, passes = should_quote(cfg, score, drift_cities=set())
        assert passes is True

    def test_nws_margin_exact_boundary_passes(self):
        """above: nws_max=71, threshold=69, margin=2.0 == min_nws_margin → allow (>=)."""
        cfg = self._cfg(min_nws_margin=2.0)
        score = _make_score(
            no_p_win=0.98, n_models=3, direction="above",
            threshold=69.0, no_margin=5.0, nws_max=71.0,
        )
        _, passes = should_quote(cfg, score, drift_cities=set())
        assert passes is True


# ---------------------------------------------------------------------------
# Task 4: compute_cap_multiplier()
# ---------------------------------------------------------------------------

class TestComputeCapMultiplier:
    """compute_cap_multiplier(nws_max, anchor_cents, config) → float."""

    def _cfg(self, **kwargs) -> WeatherMakerConfig:
        defaults = dict(
            nws_missing_cap_multiplier=0.5,
            low_price_threshold_cents=75,
            low_price_cap_multiplier=0.5,
        )
        defaults.update(kwargs)
        return WeatherMakerConfig(**defaults)

    def test_full_caps_no_penalty(self):
        """nws_max present, price above threshold → multiplier == 1.0."""
        cfg = self._cfg()
        assert compute_cap_multiplier(nws_max=72.0, anchor_cents=85, config=cfg) == pytest.approx(1.0)

    def test_low_price_only(self):
        """nws_max present, price below threshold → 0.5."""
        cfg = self._cfg()
        assert compute_cap_multiplier(nws_max=72.0, anchor_cents=70, config=cfg) == pytest.approx(0.5)

    def test_nws_missing_only(self):
        """nws_max==0 (unavailable), price above threshold → 0.5."""
        cfg = self._cfg()
        assert compute_cap_multiplier(nws_max=0.0, anchor_cents=85, config=cfg) == pytest.approx(0.5)

    def test_both_penalties_stack(self):
        """nws_max==0 AND price below threshold → 0.5 * 0.5 = 0.25."""
        cfg = self._cfg()
        assert compute_cap_multiplier(nws_max=0.0, anchor_cents=70, config=cfg) == pytest.approx(0.25)

    def test_exact_boundary_no_penalty(self):
        """anchor_cents == low_price_threshold_cents → not below threshold → no penalty."""
        cfg = self._cfg(low_price_threshold_cents=75)
        assert compute_cap_multiplier(nws_max=72.0, anchor_cents=75, config=cfg) == pytest.approx(1.0)

    def test_custom_multipliers_stack(self):
        """Custom multipliers: nws=0.8, low_price=0.6, both → 0.48."""
        cfg = self._cfg(nws_missing_cap_multiplier=0.8, low_price_cap_multiplier=0.6)
        assert compute_cap_multiplier(nws_max=0.0, anchor_cents=70, config=cfg) == pytest.approx(0.48)
