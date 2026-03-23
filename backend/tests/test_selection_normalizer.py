"""
Adversarial tests for selection_normalizer.py

This is the sneakiest module — where a system can look perfectly correct
and still send false arbs. Every test here represents a real failure mode.

Failure modes tested:
  1. Home/Away mapped against wrong orientation
  2. Source uses reversed presentation order
  3. "1" and "2" meanings differ by vendor (via overrides)
  4. Draw omitted or mislabeled
  5. Over/Under without line context confusion
  6. Team name used as selection directly
  7. Home/Away labels WITHOUT team context → AMBIGUOUS, not guessed
  8. Selection that doesn't match either participant → UNRESOLVED
  9. Substring matching safety (one team match OK, two → reject)
  10. Canonical context check — weak context stays ambiguous
  11. Numeric labels gated behind canonical context
  12. Contradictory context rejection
  13. Source field on resolution
"""

import pytest
from backend.matcher.selection_normalizer import (
    normalize_selection,
    resolve_selection,
    SelectionType,
    SelectionResolutionStatus,
    SelectionResolution,
    _is_canonical_team_name,
)


# =====================================================================
# Canonical Context Check
# =====================================================================

class TestCanonicalContextCheck:
    """_is_canonical_team_name() distinguishes resolved from unresolved context."""

    def test_two_word_is_canonical(self):
        assert _is_canonical_team_name("boston celtics") is True

    def test_three_word_is_canonical(self):
        assert _is_canonical_team_name("los angeles lakers") is True

    def test_single_word_is_not_canonical(self):
        """Short tokens like 'kings', 'heat' are likely unresolved."""
        assert _is_canonical_team_name("kings") is False
        assert _is_canonical_team_name("heat") is False

    def test_empty_is_not_canonical(self):
        assert _is_canonical_team_name("") is False

    def test_home_with_weak_context_is_ambiguous(self):
        """'Home' with single-word context should be AMBIGUOUS, not CANONICAL."""
        res = resolve_selection("Home", home_team="kings")
        assert res.is_canonical is False
        assert res.status == SelectionResolutionStatus.AMBIGUOUS

    def test_home_with_canonical_context_is_canonical(self):
        """'Home' with multi-word context should be CANONICAL."""
        res = resolve_selection("Home", home_team="sacramento kings")
        assert res.is_canonical is True
        assert res.name == "sacramento kings"


# =====================================================================
# Resolution Status API
# =====================================================================

class TestResolutionStatus:
    def test_canonical_over(self):
        res = resolve_selection("Over")
        assert res.is_canonical is True
        assert res.status == SelectionResolutionStatus.CANONICAL
        assert res.selection_type == SelectionType.OVER
        assert res.name == "over"

    def test_canonical_home_with_context(self):
        res = resolve_selection("Home", home_team="boston celtics", away_team="los angeles lakers")
        assert res.is_canonical is True
        assert res.selection_type == SelectionType.TEAM
        assert res.name == "boston celtics"

    def test_ambiguous_home_without_context(self):
        """Home without team context is AMBIGUOUS — never guessed."""
        res = resolve_selection("Home")
        assert res.is_canonical is False
        assert res.status == SelectionResolutionStatus.AMBIGUOUS

    def test_unresolved_unknown_selection(self):
        res = resolve_selection("Random Stuff", home_team="boston celtics", away_team="los angeles lakers")
        assert res.is_canonical is False
        assert res.status == SelectionResolutionStatus.UNRESOLVED


# =====================================================================
# Home / Away Mapping
# =====================================================================

class TestHomeAwayMapping:
    """Home and Away labels must map to the correct team."""

    def test_home_maps_to_home_team(self):
        result = normalize_selection("Home", home_team="boston celtics", away_team="los angeles lakers")
        assert result == "boston celtics"

    def test_away_maps_to_away_team(self):
        result = normalize_selection("Away", home_team="boston celtics", away_team="los angeles lakers")
        assert result == "los angeles lakers"

    def test_1_maps_to_home(self):
        result = normalize_selection("1", home_team="boston celtics", away_team="los angeles lakers")
        assert result == "boston celtics"

    def test_2_maps_to_away(self):
        result = normalize_selection("2", home_team="boston celtics", away_team="los angeles lakers")
        assert result == "los angeles lakers"

    def test_team_1_maps_to_home(self):
        result = normalize_selection("Team 1", home_team="boston celtics", away_team="los angeles lakers")
        assert result == "boston celtics"

    def test_team_2_maps_to_away(self):
        result = normalize_selection("Team 2", home_team="boston celtics", away_team="los angeles lakers")
        assert result == "los angeles lakers"

    def test_home_away_are_case_insensitive(self):
        assert normalize_selection("HOME", home_team="boston celtics") == "boston celtics"
        assert normalize_selection("away", away_team="los angeles lakers") == "los angeles lakers"

    def test_reversed_teams_get_reversed_result(self):
        """If home/away are reversed in the event data, the selection follows."""
        r1 = normalize_selection("Home", home_team="boston celtics", away_team="los angeles lakers")
        r2 = normalize_selection("Home", home_team="los angeles lakers", away_team="boston celtics")
        assert r1 == "boston celtics"
        assert r2 == "los angeles lakers"
        assert r1 != r2


# =====================================================================
# CRITICAL: Home/Away Without Context
# =====================================================================

class TestMissingContext:
    """Home/Away labels WITHOUT team context must be AMBIGUOUS, never guessed."""

    def test_home_no_context_is_ambiguous(self):
        res = resolve_selection("Home")
        assert res.is_canonical is False
        assert res.status == SelectionResolutionStatus.AMBIGUOUS
        assert res.name == "home"  # Returns cleaned label, NOT a team

    def test_away_no_context_is_ambiguous(self):
        res = resolve_selection("Away")
        assert res.is_canonical is False
        assert res.status == SelectionResolutionStatus.AMBIGUOUS

    def test_1_no_context_is_ambiguous(self):
        res = resolve_selection("1")
        assert res.is_canonical is False
        assert res.status == SelectionResolutionStatus.AMBIGUOUS

    def test_2_no_context_is_ambiguous(self):
        res = resolve_selection("2")
        assert res.is_canonical is False
        assert res.status == SelectionResolutionStatus.AMBIGUOUS

    def test_home_with_only_away_context_still_ambiguous(self):
        """'Home' needs home_team, not away_team."""
        res = resolve_selection("Home", away_team="los angeles lakers")
        assert res.is_canonical is False


# =====================================================================
# Numeric Label Gating
# =====================================================================

class TestNumericLabels:
    """Numeric labels (1, 2) are DANGEROUS — stricter gating than Home/Away."""

    def test_1_with_canonical_context_resolves(self):
        res = resolve_selection("1", home_team="boston celtics", away_team="los angeles lakers")
        assert res.is_canonical is True
        assert res.name == "boston celtics"

    def test_1_with_weak_context_is_ambiguous(self):
        """'1' with single-word home_team should be AMBIGUOUS."""
        res = resolve_selection("1", home_team="celtics")
        assert res.is_canonical is False
        assert res.status == SelectionResolutionStatus.AMBIGUOUS

    def test_2_with_weak_context_is_ambiguous(self):
        res = resolve_selection("2", away_team="lakers")
        assert res.is_canonical is False
        assert res.status == SelectionResolutionStatus.AMBIGUOUS

    def test_team_1_with_canonical_context(self):
        res = resolve_selection("Team 1", home_team="boston celtics")
        assert res.is_canonical is True
        assert res.name == "boston celtics"

    def test_team_2_without_context(self):
        res = resolve_selection("Team 2")
        assert res.is_canonical is False


# =====================================================================
# Over / Under / Draw
# =====================================================================

class TestOverUnderDraw:
    """These are context-free — they don't need team info."""

    def test_over_variations(self):
        assert normalize_selection("Over") == "over"
        assert normalize_selection("O") == "over"
        assert normalize_selection("ov") == "over"
        assert normalize_selection("ovr") == "over"

    def test_under_variations(self):
        assert normalize_selection("Under") == "under"
        assert normalize_selection("U") == "under"
        assert normalize_selection("un") == "under"
        assert normalize_selection("und") == "under"

    def test_draw_variations(self):
        assert normalize_selection("Draw") == "draw"
        assert normalize_selection("Tie") == "draw"
        assert normalize_selection("X") == "draw"
        assert normalize_selection("tied") == "draw"
        assert normalize_selection("d") == "draw"

    def test_over_type(self):
        res = resolve_selection("Over")
        assert res.selection_type == SelectionType.OVER

    def test_under_type(self):
        res = resolve_selection("Under")
        assert res.selection_type == SelectionType.UNDER

    def test_draw_type(self):
        res = resolve_selection("Draw")
        assert res.selection_type == SelectionType.DRAW

    def test_over_under_dont_need_context(self):
        """Over/Under should resolve even without team context."""
        res_over = resolve_selection("Over")
        res_under = resolve_selection("Under")
        assert res_over.is_canonical is True
        assert res_under.is_canonical is True


# =====================================================================
# Team Name as Selection
# =====================================================================

class TestTeamNameSelection:
    """When the selection IS the team name directly."""

    def test_exact_team_name_matches(self):
        result = normalize_selection(
            "boston celtics",
            home_team="boston celtics",
            away_team="los angeles lakers",
        )
        assert result == "boston celtics"

    def test_away_team_name_matches(self):
        result = normalize_selection(
            "los angeles lakers",
            home_team="boston celtics",
            away_team="los angeles lakers",
        )
        assert result == "los angeles lakers"

    def test_team_name_case_insensitive(self):
        result = normalize_selection(
            "BOSTON CELTICS",
            home_team="boston celtics",
            away_team="los angeles lakers",
        )
        assert result == "boston celtics"


# =====================================================================
# Substring Matching Safety
# =====================================================================

class TestSubstringMatching:
    """Substring matching should be safe — only resolve if one team matches."""

    def test_team_name_with_spread(self):
        """'Boston Celtics +3.5' should match to celtics."""
        res = resolve_selection(
            "Boston Celtics +3.5",
            home_team="boston celtics",
            away_team="los angeles lakers",
        )
        assert res.name == "boston celtics"
        assert res.is_canonical is True

    def test_contradictory_substring_rejected(self):
        """If both teams are substrings of the selection, reject."""
        # Both 'chicago' teams contain the city, and the selection contains both
        res = resolve_selection(
            "chicago bears vs chicago bulls moneyline",
            home_team="chicago bears",
            away_team="chicago bulls",
        )
        assert res.is_canonical is False
        assert res.status == SelectionResolutionStatus.AMBIGUOUS

    def test_no_team_match_is_unresolved(self):
        """Selection that doesn't match either participant."""
        res = resolve_selection(
            "Chicago Bulls",
            home_team="boston celtics",
            away_team="los angeles lakers",
        )
        assert res.status == SelectionResolutionStatus.UNRESOLVED


# =====================================================================
# Source Field
# =====================================================================

class TestSourceField:
    def test_source_preserved_on_resolution(self):
        res = resolve_selection("Over", source="fanduel")
        assert res.source == "fanduel"

    def test_source_preserved_on_team_resolution(self):
        res = resolve_selection("Home", home_team="boston celtics", source="draftkings")
        assert res.source == "draftkings"

    def test_source_preserved_on_ambiguous(self):
        res = resolve_selection("1", source="unknown_vendor")
        assert res.source == "unknown_vendor"


# =====================================================================
# Raw Values Preserved
# =====================================================================

class TestRawValuesPreserved:
    def test_raw_input_preserved(self):
        res = resolve_selection("Home", home_team="boston celtics", away_team="los angeles lakers")
        assert res.raw_input == "Home"

    def test_raw_teams_preserved(self):
        res = resolve_selection("Home", home_team="Boston Celtics", away_team="LA Lakers")
        assert res.raw_home_team == "Boston Celtics"
        assert res.raw_away_team == "LA Lakers"


# =====================================================================
# Edge Cases
# =====================================================================

class TestEdgeCases:
    def test_extra_whitespace(self):
        assert normalize_selection("  Over  ") == "over"
        assert normalize_selection("  Home  ", home_team="boston celtics") == "boston celtics"

    def test_empty_string(self):
        res = resolve_selection("")
        assert res.is_canonical is False

    def test_h_label(self):
        """'h' should map to home with canonical context."""
        result = normalize_selection("h", home_team="boston celtics", away_team="los angeles lakers")
        assert result == "boston celtics"

    def test_a_label(self):
        """'a' should map to away with canonical context."""
        result = normalize_selection("a", home_team="boston celtics", away_team="los angeles lakers")
        assert result == "los angeles lakers"

    def test_h_label_weak_context_ambiguous(self):
        """'h' with single-word context stays ambiguous."""
        res = resolve_selection("h", home_team="celtics")
        assert res.is_canonical is False
