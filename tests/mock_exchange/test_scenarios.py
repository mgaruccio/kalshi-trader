"""Invariant tests for scenario definitions.

These tests verify structural correctness of scenarios — valid price ranges,
no arbitrage (YES+NO bids can't exceed 100c), and unique names.
They do NOT run the exchange; that happens in the confidence test runner (Task 8).
"""
import pytest

from tests.mock_exchange.scenarios import ALL_SCENARIOS, BookUpdate, Scenario


class TestScenarioInvariants:
    def test_all_scenario_names_unique(self):
        names = [s.name for s in ALL_SCENARIOS]
        assert len(names) == len(set(names)), f"Duplicate scenario names: {names}"

    def test_all_scenarios_have_nonempty_name_and_description(self):
        for s in ALL_SCENARIOS:
            assert s.name, f"Scenario missing name: {s!r}"
            assert s.description, f"Scenario {s.name!r} missing description"

    @pytest.mark.parametrize("scenario", ALL_SCENARIOS, ids=lambda s: s.name)
    def test_initial_prices_in_valid_range(self, scenario: Scenario):
        assert 1 <= scenario.initial_no_bid_cents <= 99, (
            f"{scenario.name}: initial_no_bid_cents={scenario.initial_no_bid_cents} out of range"
        )
        assert 1 <= scenario.initial_yes_bid_cents <= 99, (
            f"{scenario.name}: initial_yes_bid_cents={scenario.initial_yes_bid_cents} out of range"
        )

    @pytest.mark.parametrize("scenario", ALL_SCENARIOS, ids=lambda s: s.name)
    def test_initial_prices_no_arbitrage(self, scenario: Scenario):
        """YES bid + NO bid must be <= 100c (no risk-free profit)."""
        total = scenario.initial_yes_bid_cents + scenario.initial_no_bid_cents
        assert total <= 100, (
            f"{scenario.name}: YES+NO bids sum to {total}c — arbitrage opportunity"
        )

    @pytest.mark.parametrize("scenario", ALL_SCENARIOS, ids=lambda s: s.name)
    def test_all_book_updates_in_valid_range(self, scenario: Scenario):
        for i, upd in enumerate(scenario.updates):
            assert 1 <= upd.no_bid_cents <= 99, (
                f"{scenario.name} update[{i}]: no_bid_cents={upd.no_bid_cents} out of range"
            )
            assert 1 <= upd.yes_bid_cents <= 99, (
                f"{scenario.name} update[{i}]: yes_bid_cents={upd.yes_bid_cents} out of range"
            )

    @pytest.mark.parametrize("scenario", ALL_SCENARIOS, ids=lambda s: s.name)
    def test_all_book_updates_no_arbitrage(self, scenario: Scenario):
        for i, upd in enumerate(scenario.updates):
            total = upd.yes_bid_cents + upd.no_bid_cents
            assert total <= 100, (
                f"{scenario.name} update[{i}]: YES+NO bids sum to {total}c"
            )

    @pytest.mark.parametrize("scenario", ALL_SCENARIOS, ids=lambda s: s.name)
    def test_all_book_updates_have_positive_delay(self, scenario: Scenario):
        for i, upd in enumerate(scenario.updates):
            assert upd.delay_ms > 0, (
                f"{scenario.name} update[{i}]: delay_ms must be > 0"
            )

    @pytest.mark.parametrize("scenario", ALL_SCENARIOS, ids=lambda s: s.name)
    def test_all_book_updates_have_positive_sizes(self, scenario: Scenario):
        for i, upd in enumerate(scenario.updates):
            assert upd.no_bid_size > 0, (
                f"{scenario.name} update[{i}]: no_bid_size must be > 0"
            )
            assert upd.yes_bid_size > 0, (
                f"{scenario.name} update[{i}]: yes_bid_size must be > 0"
            )

    @pytest.mark.parametrize("scenario", ALL_SCENARIOS, ids=lambda s: s.name)
    def test_limit_fill_price_present_when_expected(self, scenario: Scenario):
        if scenario.expect_limit_fill:
            assert scenario.expect_limit_fill_price_cents is not None, (
                f"{scenario.name}: expect_limit_fill=True but no expect_limit_fill_price_cents"
            )
            assert 1 <= scenario.expect_limit_fill_price_cents <= 99, (
                f"{scenario.name}: expect_limit_fill_price_cents out of range"
            )

    @pytest.mark.parametrize("scenario", ALL_SCENARIOS, ids=lambda s: s.name)
    def test_limit_fill_price_absent_when_not_expected(self, scenario: Scenario):
        if not scenario.expect_limit_fill:
            assert scenario.expect_limit_fill_price_cents is None, (
                f"{scenario.name}: expect_limit_fill=False but expect_limit_fill_price_cents is set"
            )

    @pytest.mark.parametrize("scenario", ALL_SCENARIOS, ids=lambda s: s.name)
    def test_cancel_count_non_negative(self, scenario: Scenario):
        assert scenario.expect_cancel_count >= 0, (
            f"{scenario.name}: expect_cancel_count must be >= 0"
        )

    @pytest.mark.parametrize("scenario", ALL_SCENARIOS, ids=lambda s: s.name)
    def test_cancel_count_bounded_by_update_count(self, scenario: Scenario):
        """Strategy can't cancel more times than there are book updates."""
        assert scenario.expect_cancel_count <= len(scenario.updates), (
            f"{scenario.name}: expect_cancel_count={scenario.expect_cancel_count} "
            f"exceeds update count={len(scenario.updates)}"
        )


class TestScenarioSemantics:
    """Higher-level checks that the 4 named scenarios encode the right narrative."""

    def test_steady_fill_expects_limit_fill_at_34c(self):
        from tests.mock_exchange.scenarios import STEADY_FILL
        assert STEADY_FILL.expect_limit_fill is True
        assert STEADY_FILL.expect_limit_fill_price_cents == 34
        assert STEADY_FILL.expect_cancel_count == 0
        # YES bid must reach 66c in final update for NO ask to hit 34c
        final_yes = STEADY_FILL.updates[-1].yes_bid_cents
        assert final_yes == 66, f"Expected YES bid 66 in final update, got {final_yes}"

    def test_chase_up_expects_5_cancels_and_limit_fill(self):
        from tests.mock_exchange.scenarios import CHASE_UP
        assert CHASE_UP.expect_cancel_count == 5
        assert CHASE_UP.expect_limit_fill is True
        # NO bid must walk up (strictly increasing for the first 5 updates)
        no_bids = [CHASE_UP.initial_no_bid_cents] + [u.no_bid_cents for u in CHASE_UP.updates]
        assert no_bids[:6] == sorted(no_bids[:6]), "NO bid should walk up in first 5 steps"

    def test_market_order_immediate_has_no_updates(self):
        from tests.mock_exchange.scenarios import MARKET_ORDER_IMMEDIATE
        assert MARKET_ORDER_IMMEDIATE.updates == ()
        assert MARKET_ORDER_IMMEDIATE.expect_market_fill is True
        assert MARKET_ORDER_IMMEDIATE.expect_limit_fill is False

    def test_no_fill_timeout_no_ask_never_low_enough(self):
        from tests.mock_exchange.scenarios import NO_FILL_TIMEOUT
        assert NO_FILL_TIMEOUT.expect_limit_fill is False
        # Verify NO ask (= 100 - YES bid) never drops to 34c in any update
        for upd in NO_FILL_TIMEOUT.updates:
            no_ask = 100 - upd.yes_bid_cents
            assert no_ask > 40, (
                f"NO ask={no_ask}c in update — scenario should keep NO ask > 40c"
            )

    def test_all_four_scenarios_present(self):
        from tests.mock_exchange.scenarios import ALL_SCENARIOS
        names = {s.name for s in ALL_SCENARIOS}
        assert names == {
            "steady_fill", "chase_up", "market_order_immediate", "no_fill_timeout"
        }
