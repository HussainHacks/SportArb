"""
Tests for the arbitrage detector.
"""

import pytest
from datetime import datetime, timezone

from backend.normalization.models import NormalizedOdds, MarketKey, MarketStatus
from backend.engine.arb_detector import (
    implied_probability,
    find_2way_arbs,
    find_3way_arbs,
    detect_arbs,
    _get_top_n_odds_by_selection,
)


def make_odds(
    event_id="evt1",
    event_name="Team A @ Team B",
    sport_key="basketball_nba",
    market_key=MarketKey.H2H,
    selection="Team A",
    odds=2.0,
    operator="draftkings",
    operator_display="DraftKings",
    point=None,
) -> NormalizedOdds:
    """Helper to create NormalizedOdds for testing."""
    return NormalizedOdds(
        event_id=event_id,
        sport_key=sport_key,
        event_name=event_name,
        commence_time=datetime(2025, 1, 1, tzinfo=timezone.utc),
        market_key=market_key,
        selection=selection,
        point=point,
        odds=odds,
        operator=operator,
        operator_display=operator_display,
        timestamp=datetime.now(timezone.utc),
        status=MarketStatus.ACTIVE,
    )


class TestImpliedProbability:
    def test_even_odds(self):
        assert implied_probability(2.0) == 0.5

    def test_heavy_favorite(self):
        assert abs(implied_probability(1.1) - (1 / 1.1)) < 0.001

    def test_big_underdog(self):
        assert abs(implied_probability(10.0) - 0.1) < 0.001

    def test_zero_odds_returns_one(self):
        assert implied_probability(0) == 1.0


class TestTopNOdds:
    """Tests for the top-N odds selection with fallback candidates."""

    def test_keeps_top_3_distinct_operators(self):
        odds = [
            make_odds(selection="Lakers", odds=2.10, operator="fanduel"),
            make_odds(selection="Lakers", odds=2.05, operator="draftkings"),
            make_odds(selection="Lakers", odds=2.02, operator="betmgm"),
            make_odds(selection="Lakers", odds=1.95, operator="caesars"),
        ]
        result = _get_top_n_odds_by_selection(odds, top_n=3)
        assert "Lakers" in result
        lakers = result["Lakers"]
        assert len(lakers) == 3
        # Sorted best-first
        assert lakers[0].odds == 2.10
        assert lakers[0].operator == "fanduel"
        assert lakers[1].odds == 2.05
        assert lakers[2].odds == 2.02

    def test_best_per_operator_kept(self):
        """If same operator has multiple lines, only the best is kept."""
        odds = [
            make_odds(selection="Lakers", odds=2.10, operator="fanduel"),
            make_odds(selection="Lakers", odds=2.15, operator="fanduel"),  # better line
            make_odds(selection="Lakers", odds=2.00, operator="draftkings"),
        ]
        result = _get_top_n_odds_by_selection(odds, top_n=3)
        lakers = result["Lakers"]
        assert len(lakers) == 2
        assert lakers[0].odds == 2.15  # best fanduel line
        assert lakers[0].operator == "fanduel"

    def test_single_operator_returns_one(self):
        odds = [
            make_odds(selection="Lakers", odds=2.10, operator="fanduel"),
        ]
        result = _get_top_n_odds_by_selection(odds, top_n=3)
        assert len(result["Lakers"]) == 1

    def test_multiple_selections_independent(self):
        odds = [
            make_odds(selection="Lakers", odds=2.10, operator="fanduel"),
            make_odds(selection="Lakers", odds=2.05, operator="draftkings"),
            make_odds(selection="Celtics", odds=1.80, operator="betmgm"),
            make_odds(selection="Celtics", odds=1.85, operator="fanduel"),
        ]
        result = _get_top_n_odds_by_selection(odds, top_n=3)
        assert result["Lakers"][0].odds == 2.10
        assert result["Celtics"][0].odds == 1.85  # fanduel is best for Celtics


class TestFind2WayArbs:
    def test_clear_arb(self):
        """Two operators offering odds that sum to < 100% implied probability."""
        odds = [
            make_odds(selection="Team A", odds=2.15, operator="fanduel", operator_display="FanDuel"),
            make_odds(selection="Team B", odds=2.05, operator="draftkings", operator_display="DraftKings"),
        ]
        arbs = find_2way_arbs(odds, min_profit=0.1, max_profit=25.0, default_stake=1000.0)
        assert len(arbs) == 1
        assert arbs[0].profit_percent > 0

    def test_no_arb_when_sum_exceeds_100(self):
        """Standard vig scenario — no arb."""
        odds = [
            make_odds(selection="Team A", odds=1.90, operator="fanduel"),
            make_odds(selection="Team B", odds=1.90, operator="draftkings"),
        ]
        arbs = find_2way_arbs(odds, min_profit=0.1, max_profit=25.0)
        assert len(arbs) == 0

    def test_same_operator_not_arbed(self):
        """Can't arb the same bookmaker against itself."""
        odds = [
            make_odds(selection="Team A", odds=2.5, operator="fanduel"),
            make_odds(selection="Team B", odds=2.5, operator="fanduel"),
        ]
        arbs = find_2way_arbs(odds, min_profit=0.1, max_profit=25.0)
        assert len(arbs) == 0

    def test_spread_arb_matching_points(self):
        """Spread arb with matching point values."""
        odds = [
            make_odds(selection="Team A", odds=2.10, operator="fanduel", market_key=MarketKey.SPREADS, point=3.5),
            make_odds(selection="Team B", odds=2.10, operator="draftkings", market_key=MarketKey.SPREADS, point=-3.5),
        ]
        arbs = find_2way_arbs(odds, min_profit=0.1, max_profit=25.0)
        assert len(arbs) == 1

    def test_edge_case_identical_odds(self):
        """Identical odds (exactly 2.0 each) → no arb (sum = 100%)."""
        odds = [
            make_odds(selection="Team A", odds=2.0, operator="fanduel"),
            make_odds(selection="Team B", odds=2.0, operator="draftkings"),
        ]
        arbs = find_2way_arbs(odds, min_profit=0.1, max_profit=25.0)
        assert len(arbs) == 0

    def test_picks_best_from_multiple_operators(self):
        """With 3 operators per side, the best odds should be used."""
        odds = [
            make_odds(selection="Team A", odds=2.10, operator="fanduel"),
            make_odds(selection="Team A", odds=2.05, operator="draftkings"),
            make_odds(selection="Team A", odds=2.00, operator="betmgm"),
            make_odds(selection="Team B", odds=2.05, operator="caesars"),
            make_odds(selection="Team B", odds=1.95, operator="espnbet"),
        ]
        arbs = find_2way_arbs(odds, min_profit=0.1, max_profit=25.0)
        # Best: fanduel 2.10 vs caesars 2.05
        assert len(arbs) == 1
        legs = arbs[0].legs
        assert legs[0].operator == "fanduel"
        assert legs[1].operator == "caesars"


class TestFind3WayArbs:
    def test_soccer_1x2_arb(self):
        """3-way soccer arb (home/draw/away)."""
        odds = [
            make_odds(selection="Home", odds=3.5, operator="fanduel"),
            make_odds(selection="Draw", odds=4.0, operator="draftkings"),
            make_odds(selection="Away", odds=3.0, operator="betmgm"),
        ]
        # 1/3.5 + 1/4.0 + 1/3.0 = 0.2857 + 0.25 + 0.3333 = 0.8690 → arb!
        arbs = find_3way_arbs(odds, min_profit=0.1, max_profit=30.0)
        assert len(arbs) == 1
        assert arbs[0].profit_percent > 10  # ~15% profit

    def test_no_3way_arb_with_vig(self):
        """Standard 3-way vig — no arb."""
        odds = [
            make_odds(selection="Home", odds=2.0, operator="fanduel"),
            make_odds(selection="Draw", odds=3.2, operator="draftkings"),
            make_odds(selection="Away", odds=3.5, operator="betmgm"),
        ]
        # 1/2.0 + 1/3.2 + 1/3.5 = 0.5 + 0.3125 + 0.2857 = 1.098 → no arb
        arbs = find_3way_arbs(odds, min_profit=0.1, max_profit=25.0)
        assert len(arbs) == 0


class TestDetectArbs:
    def test_dispatches_to_2way(self):
        odds_by_market = {
            "h2h": [
                make_odds(selection="Team A", odds=2.15, operator="fanduel"),
                make_odds(selection="Team B", odds=2.05, operator="draftkings"),
            ]
        }
        arbs = detect_arbs(odds_by_market, min_profit=0.1, max_profit=25.0)
        assert len(arbs) == 1

    def test_empty_market(self):
        arbs = detect_arbs({}, min_profit=0.1, max_profit=25.0)
        assert len(arbs) == 0
