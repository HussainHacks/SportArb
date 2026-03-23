"""
Tests for risk filters.
"""

import pytest
from datetime import datetime, timezone, timedelta

from backend.normalization.models import NormalizedOdds, ArbOpportunity, MarketKey, MarketStatus
from backend.engine.risk_filters import (
    filter_stale_odds,
    filter_palpable_error,
    filter_minimum_profit,
    filter_suspended_markets,
    filter_iowa_college_props,
    filter_mismatched_lines,
    apply_all_filters,
)


def make_arb(
    event_name="Team A @ Team B",
    market_key=MarketKey.H2H,
    profit_percent=3.0,
    arb_percent=97.0,
    legs=None,
    odds_timestamp=None,
) -> ArbOpportunity:
    """Helper to create an ArbOpportunity for testing."""
    if odds_timestamp is None:
        odds_timestamp = datetime.now(timezone.utc)

    if legs is None:
        legs = [
            NormalizedOdds(
                event_id="evt1",
                sport_key="basketball_nba",
                event_name=event_name,
                commence_time=datetime(2025, 1, 1, tzinfo=timezone.utc),
                market_key=market_key,
                selection="Team A",
                odds=2.15,
                operator="fanduel",
                operator_display="FanDuel",
                timestamp=odds_timestamp,
                status=MarketStatus.ACTIVE,
            ),
            NormalizedOdds(
                event_id="evt1",
                sport_key="basketball_nba",
                event_name=event_name,
                commence_time=datetime(2025, 1, 1, tzinfo=timezone.utc),
                market_key=market_key,
                selection="Team B",
                odds=2.05,
                operator="draftkings",
                operator_display="DraftKings",
                timestamp=odds_timestamp,
                status=MarketStatus.ACTIVE,
            ),
        ]

    return ArbOpportunity(
        event_id="evt1",
        event_name=event_name,
        sport_key="basketball_nba",
        market_key=market_key,
        legs=legs,
        arb_percent=arb_percent,
        profit_percent=profit_percent,
        stakes={"fanduel": 488.10, "draftkings": 511.90},
        total_stake=1000.0,
        guaranteed_payout=1030.0,
    )


class TestFilterStaleOdds:
    def test_fresh_odds_pass(self):
        arb = make_arb(odds_timestamp=datetime.now(timezone.utc))
        assert filter_stale_odds(arb).passed

    def test_stale_odds_fail(self):
        old_time = datetime.now(timezone.utc) - timedelta(seconds=120)
        arb = make_arb(odds_timestamp=old_time)
        result = filter_stale_odds(arb)
        assert not result.passed
        assert "Stale odds" in result.reason


class TestFilterPalpableError:
    def test_normal_profit_passes(self):
        arb = make_arb(profit_percent=3.0)
        assert filter_palpable_error(arb).passed

    def test_extreme_profit_fails(self):
        arb = make_arb(profit_percent=25.0)
        result = filter_palpable_error(arb)
        assert not result.passed
        assert "palpable error" in result.reason.lower()


class TestFilterMinimumProfit:
    def test_above_min_passes(self):
        arb = make_arb(profit_percent=1.0)
        assert filter_minimum_profit(arb).passed

    def test_below_min_fails(self):
        arb = make_arb(profit_percent=0.1)
        result = filter_minimum_profit(arb)
        assert not result.passed


class TestFilterSuspendedMarkets:
    def test_active_market_passes(self):
        arb = make_arb()
        assert filter_suspended_markets(arb).passed

    def test_suspended_market_fails(self):
        legs = [
            NormalizedOdds(
                event_id="evt1",
                sport_key="basketball_nba",
                event_name="Team A @ Team B",
                commence_time=datetime(2025, 1, 1, tzinfo=timezone.utc),
                market_key=MarketKey.H2H,
                selection="Team A",
                odds=2.15,
                operator="fanduel",
                timestamp=datetime.now(timezone.utc),
                status=MarketStatus.SUSPENDED,
            ),
            NormalizedOdds(
                event_id="evt1",
                sport_key="basketball_nba",
                event_name="Team A @ Team B",
                commence_time=datetime(2025, 1, 1, tzinfo=timezone.utc),
                market_key=MarketKey.H2H,
                selection="Team B",
                odds=2.05,
                operator="draftkings",
                timestamp=datetime.now(timezone.utc),
                status=MarketStatus.ACTIVE,
            ),
        ]
        arb = make_arb(legs=legs)
        result = filter_suspended_markets(arb)
        assert not result.passed


class TestFilterIowaCollegeProps:
    def test_non_prop_passes(self):
        arb = make_arb(event_name="Iowa Hawkeyes @ Nebraska Huskers")
        # Not a player prop, so should pass
        assert filter_iowa_college_props(arb).passed

    def test_iowa_player_prop_fails(self):
        legs = [
            NormalizedOdds(
                event_id="evt1",
                sport_key="basketball_ncaab",
                event_name="Iowa Hawkeyes @ Nebraska Huskers",
                commence_time=datetime(2025, 1, 1, tzinfo=timezone.utc),
                market_key=MarketKey.H2H,
                selection="Player Pts Over",
                odds=1.90,
                operator="fanduel",
                timestamp=datetime.now(timezone.utc),
                status=MarketStatus.ACTIVE,
                is_player_prop=True,  # This is the critical flag
            ),
            NormalizedOdds(
                event_id="evt1",
                sport_key="basketball_ncaab",
                event_name="Iowa Hawkeyes @ Nebraska Huskers",
                commence_time=datetime(2025, 1, 1, tzinfo=timezone.utc),
                market_key=MarketKey.H2H,
                selection="Player Pts Under",
                odds=1.90,
                operator="draftkings",
                timestamp=datetime.now(timezone.utc),
                status=MarketStatus.ACTIVE,
                is_player_prop=True,
            ),
        ]
        arb = make_arb(event_name="Iowa Hawkeyes @ Nebraska Huskers", legs=legs)
        result = filter_iowa_college_props(arb)
        assert not result.passed
        assert "iowa" in result.reason.lower()

    def test_non_iowa_college_prop_passes(self):
        legs = [
            NormalizedOdds(
                event_id="evt1",
                sport_key="basketball_ncaab",
                event_name="Duke Blue Devils @ UNC Tar Heels",
                commence_time=datetime(2025, 1, 1, tzinfo=timezone.utc),
                market_key=MarketKey.H2H,
                selection="Player Pts Over",
                odds=1.90,
                operator="fanduel",
                timestamp=datetime.now(timezone.utc),
                status=MarketStatus.ACTIVE,
                is_player_prop=True,
            ),
            NormalizedOdds(
                event_id="evt1",
                sport_key="basketball_ncaab",
                event_name="Duke Blue Devils @ UNC Tar Heels",
                commence_time=datetime(2025, 1, 1, tzinfo=timezone.utc),
                market_key=MarketKey.H2H,
                selection="Player Pts Under",
                odds=1.90,
                operator="draftkings",
                timestamp=datetime.now(timezone.utc),
                status=MarketStatus.ACTIVE,
                is_player_prop=True,
            ),
        ]
        arb = make_arb(event_name="Duke Blue Devils @ UNC Tar Heels", legs=legs)
        result = filter_iowa_college_props(arb)
        assert result.passed


class TestFilterMismatchedLines:
    def test_no_mismatch_for_h2h(self):
        arb = make_arb(market_key=MarketKey.H2H)
        assert filter_mismatched_lines(arb).passed

    def test_matching_spread_passes(self):
        legs = [
            NormalizedOdds(
                event_id="evt1", sport_key="basketball_nba", event_name="A @ B",
                commence_time=datetime(2025, 1, 1, tzinfo=timezone.utc),
                market_key=MarketKey.SPREADS, selection="Team A",
                point=3.5, odds=1.95, operator="fanduel",
                timestamp=datetime.now(timezone.utc), status=MarketStatus.ACTIVE,
            ),
            NormalizedOdds(
                event_id="evt1", sport_key="basketball_nba", event_name="A @ B",
                commence_time=datetime(2025, 1, 1, tzinfo=timezone.utc),
                market_key=MarketKey.SPREADS, selection="Team B",
                point=-3.5, odds=1.95, operator="draftkings",
                timestamp=datetime.now(timezone.utc), status=MarketStatus.ACTIVE,
            ),
        ]
        arb = make_arb(market_key=MarketKey.SPREADS, legs=legs)
        assert filter_mismatched_lines(arb).passed

    def test_mismatched_spread_fails(self):
        legs = [
            NormalizedOdds(
                event_id="evt1", sport_key="basketball_nba", event_name="A @ B",
                commence_time=datetime(2025, 1, 1, tzinfo=timezone.utc),
                market_key=MarketKey.SPREADS, selection="Team A",
                point=3.5, odds=1.95, operator="fanduel",
                timestamp=datetime.now(timezone.utc), status=MarketStatus.ACTIVE,
            ),
            NormalizedOdds(
                event_id="evt1", sport_key="basketball_nba", event_name="A @ B",
                commence_time=datetime(2025, 1, 1, tzinfo=timezone.utc),
                market_key=MarketKey.SPREADS, selection="Team B",
                point=-4.0, odds=1.95, operator="draftkings",
                timestamp=datetime.now(timezone.utc), status=MarketStatus.ACTIVE,
            ),
        ]
        arb = make_arb(market_key=MarketKey.SPREADS, legs=legs)
        result = filter_mismatched_lines(arb)
        assert not result.passed


class TestApplyAllFilters:
    def test_valid_arb_passes(self):
        arb = make_arb(profit_percent=3.0)
        result, _ = apply_all_filters([arb])
        assert len(result) == 1

    def test_stale_arb_filtered(self):
        old = datetime.now(timezone.utc) - timedelta(seconds=120)
        arb = make_arb(profit_percent=3.0, odds_timestamp=old)
        result, _ = apply_all_filters([arb])
        assert len(result) == 0

    def test_multiple_arbs_mixed(self):
        good = make_arb(profit_percent=3.0)
        bad = make_arb(profit_percent=25.0)  # Palp
        result, _ = apply_all_filters([good, bad])
        assert len(result) == 1
