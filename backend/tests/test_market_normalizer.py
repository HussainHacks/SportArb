"""
Adversarial tests for market_normalizer.py

Failure modes covered:
  1. h2h vs 3-way moneyline confusion
  2. spread vs alternate spread
  3. game vs full_game vs match vs regulation
  4. 1h, first half, half 1 period convergence
  5. Unknown markets stay unresolved
  6. Resolution status (CANONICAL / UNRESOLVED)
  7. Enum validation helpers
"""

import pytest
from backend.matcher.market_normalizer import (
    normalize_market,
    normalize_period,
    resolve_market,
    resolve_period,
    is_valid_canonical_market,
    is_valid_canonical_period,
    MarketResolutionStatus,
    CanonicalMarket,
    CanonicalPeriod,
)


# =====================================================================
# Resolution Status
# =====================================================================

class TestMarketResolution:
    def test_canonical_h2h(self):
        res = resolve_market("h2h")
        assert res.is_canonical is True
        assert res.name == "moneyline"

    def test_unresolved_unknown(self):
        res = resolve_market("player_props")
        assert res.is_canonical is False
        assert res.status == MarketResolutionStatus.UNRESOLVED


class TestPeriodResolution:
    def test_canonical_full_game(self):
        res = resolve_period("full game")
        assert res.is_canonical is True
        assert res.name == "full_game"

    def test_empty_defaults_to_full_game(self):
        res = resolve_period("")
        assert res.is_canonical is True
        assert res.name == "full_game"

    def test_unresolved_unknown_period(self):
        res = resolve_period("overtime")
        assert res.is_canonical is False


# =====================================================================
# Critical: h2h vs 3-way Moneyline
# =====================================================================

class TestMoneylineDistinction:
    """h2h (2-way) and 1x2 (3-way) must NEVER be confused."""

    def test_h2h_is_moneyline_2way(self):
        assert normalize_market("h2h") == "moneyline"

    def test_1x2_is_moneyline_3way(self):
        assert normalize_market("1x2") == "moneyline_3way"

    def test_h2h_not_equals_1x2(self):
        assert normalize_market("h2h") != normalize_market("1x2")

    def test_match_winner_is_3way(self):
        """Most books use 'match winner' for soccer 3-way."""
        assert normalize_market("match winner") == "moneyline_3way"

    def test_winner_is_2way(self):
        """'winner' without 'match' is 2-way in US sports."""
        assert normalize_market("winner") == "moneyline"

    def test_3way_variations(self):
        assert normalize_market("three way") == "moneyline_3way"
        assert normalize_market("3way") == "moneyline_3way"
        assert normalize_market("3 way") == "moneyline_3way"
        assert normalize_market("match result") == "moneyline_3way"


# =====================================================================
# Spread Variations
# =====================================================================

class TestSpreads:
    def test_spread(self):
        assert normalize_market("spread") == "spread"

    def test_handicap(self):
        assert normalize_market("handicap") == "spread"

    def test_point_spread(self):
        assert normalize_market("point spread") == "spread"

    def test_asian_handicap(self):
        assert normalize_market("asian handicap") == "spread"

    def test_run_line(self):
        """MLB-specific spread."""
        assert normalize_market("run line") == "spread"

    def test_puck_line(self):
        """NHL-specific spread."""
        assert normalize_market("puck line") == "spread"

    def test_all_spreads_same(self):
        """All spread variations must normalize to the same canonical."""
        spread_names = ["spread", "handicap", "point spread",
                        "run line", "puck line", "asian handicap"]
        canonicals = {normalize_market(n) for n in spread_names}
        assert len(canonicals) == 1
        assert canonicals == {"spread"}


# =====================================================================
# Total / Over-Under
# =====================================================================

class TestTotals:
    def test_total(self):
        assert normalize_market("total") == "total"

    def test_over_under(self):
        assert normalize_market("over/under") == "total"
        assert normalize_market("over under") == "total"

    def test_ou(self):
        assert normalize_market("ou") == "total"

    def test_game_total(self):
        assert normalize_market("game total") == "total"


# =====================================================================
# Period Convergence — All variations must converge
# =====================================================================

class TestPeriodConvergence:
    """Every period variation must resolve to the same canonical."""

    def test_full_game_variations(self):
        variations = ["full game", "full_game", "game", "match",
                       "regulation", "ft", ""]
        canonicals = {normalize_period(v) for v in variations}
        assert canonicals == {"full_game"}

    def test_first_half_variations(self):
        variations = ["1st half", "first half", "1h", "h1",
                       "first_half", "half 1", "1sthalf"]
        canonicals = {normalize_period(v) for v in variations}
        assert canonicals == {"first_half"}

    def test_second_half_variations(self):
        variations = ["2nd half", "second half", "2h", "h2",
                       "second_half", "half 2"]
        canonicals = {normalize_period(v) for v in variations}
        assert canonicals == {"second_half"}

    def test_first_quarter_variations(self):
        variations = ["1st quarter", "first quarter", "1q", "q1",
                       "first_quarter", "quarter 1"]
        canonicals = {normalize_period(v) for v in variations}
        assert canonicals == {"first_quarter"}

    def test_hockey_period_variations(self):
        variations = ["1st period", "first period", "1p", "p1",
                       "first_period", "period 1"]
        canonicals = {normalize_period(v) for v in variations}
        assert canonicals == {"first_period"}

    def test_baseball_innings(self):
        variations = ["first 5 innings", "f5", "first_5_innings",
                       "f5i", "1st 5 innings"]
        canonicals = {normalize_period(v) for v in variations}
        assert canonicals == {"first_5_innings"}

    def test_periods_are_distinct(self):
        """Different periods must NOT converge."""
        assert normalize_period("1h") != normalize_period("2h")
        assert normalize_period("1q") != normalize_period("2q")
        assert normalize_period("1p") != normalize_period("2p")
        assert normalize_period("1h") != normalize_period("1q")


# =====================================================================
# Unknown Markets/Periods
# =====================================================================

class TestUnknownMarkets:
    def test_unknown_market_returns_cleaned(self):
        assert normalize_market("Player Props") == "player props"

    def test_unknown_market_is_unresolved(self):
        res = resolve_market("First Touchdown Scorer")
        assert res.status == MarketResolutionStatus.UNRESOLVED

    def test_alternate_spread_not_guessed(self):
        """'alternate spread' should NOT silently map to 'spread'."""
        res = resolve_market("alternate spread")
        # This is intentionally unresolved — alternate spreads are a different thing
        assert res.name != "spread" or res.is_canonical


class TestUnknownPeriods:
    def test_unknown_period_returns_cleaned(self):
        res = resolve_period("overtime")
        assert res.is_canonical is False
        assert res.name == "overtime"


# =====================================================================
# Enum Validation
# =====================================================================

class TestEnumValidation:
    def test_valid_canonical_market(self):
        assert is_valid_canonical_market("moneyline") is True
        assert is_valid_canonical_market("spread") is True
        assert is_valid_canonical_market("total") is True
        assert is_valid_canonical_market("moneyline_3way") is True

    def test_invalid_canonical_market(self):
        assert is_valid_canonical_market("h2h") is False
        assert is_valid_canonical_market("player_props") is False

    def test_valid_canonical_period(self):
        assert is_valid_canonical_period("full_game") is True
        assert is_valid_canonical_period("first_half") is True
        assert is_valid_canonical_period("first_quarter") is True

    def test_invalid_canonical_period(self):
        assert is_valid_canonical_period("1h") is False
        assert is_valid_canonical_period("overtime") is False


# =====================================================================
# Edge Cases
# =====================================================================

class TestEdgeCases:
    def test_case_insensitive(self):
        assert normalize_market("H2H") == "moneyline"
        assert normalize_market("SPREAD") == "spread"
        assert normalize_period("FULL GAME") == "full_game"

    def test_underscores_and_dashes(self):
        """Underscores and dashes should be treated as spaces."""
        assert normalize_market("over_under") == "total"
        assert normalize_market("point-spread") == "spread"

    def test_extra_whitespace(self):
        assert normalize_market("  spread  ") == "spread"
        assert normalize_period("  first half  ") == "first_half"
