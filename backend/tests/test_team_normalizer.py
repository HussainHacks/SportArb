"""
Adversarial tests for team_normalizer.py

Tests cover every failure mode from the spec:
  1. Alias collisions (LA → wrong team)
  2. City-name ambiguity (New York, Miami, LA)
  3. College/pro variants
  4. Short names across leagues
  5. Unicode / hidden punctuation
  6. League-scoped vs global resolution
  7. Resolution status (canonical / ambiguous / unresolved)
  8. Fuzzy blacklist
  9. Zero-width / non-breaking space
"""

import pytest
from backend.matcher.team_normalizer import (
    normalize_team_name,
    resolve_team_name,
    fuzzy_normalize_team,
    is_ambiguous_name,
    _clean_text,
    AMBIGUOUS_SHORT_NAMES,
    ResolutionStatus,
    TeamResolution,
)


# =====================================================================
# Resolution Status API
# =====================================================================

class TestResolutionStatus:
    """resolve_team_name() must return explicit status metadata."""

    def test_canonical_returns_is_canonical_true(self):
        res = resolve_team_name("Boston Celtics")
        assert res.is_canonical is True
        assert res.status == ResolutionStatus.CANONICAL
        assert res.name == "boston celtics"

    def test_canonical_with_league(self):
        res = resolve_team_name("Kings", league="nba")
        assert res.is_canonical is True
        assert res.status == ResolutionStatus.CANONICAL
        assert res.name == "sacramento kings"
        assert res.source_league == "nba"

    def test_ambiguous_without_league(self):
        res = resolve_team_name("Kings")
        assert res.is_canonical is False
        assert res.status == ResolutionStatus.AMBIGUOUS
        assert res.name == "kings"

    def test_unresolved_unknown_team(self):
        res = resolve_team_name("Faketown Unicorns")
        assert res.is_canonical is False
        assert res.status == ResolutionStatus.UNRESOLVED
        assert res.name == "faketown unicorns"

    def test_raw_input_preserved(self):
        res = resolve_team_name("LA Lakers")
        assert res.raw_input == "LA Lakers"

    def test_ambiguous_resolved_with_league_is_canonical(self):
        """Ambiguous name with league context should resolve to CANONICAL."""
        res = resolve_team_name("Panthers", league="nfl")
        assert res.is_canonical is True
        assert res.status == ResolutionStatus.CANONICAL
        assert res.name == "carolina panthers"

    def test_downstream_can_distinguish_canonical_from_unresolved(self):
        """This is the key safety test — downstream must be able to tell them apart."""
        canonical = resolve_team_name("Boston Celtics")
        unresolved = resolve_team_name("Some Random Team")
        ambiguous = resolve_team_name("Kings")

        assert canonical.status != unresolved.status
        assert canonical.status != ambiguous.status
        assert unresolved.status != ambiguous.status


# =====================================================================
# Basic Happy Path — Full Names
# =====================================================================

class TestFullNameResolution:
    """Full team names should always resolve correctly."""

    def test_nba_full_name(self):
        assert normalize_team_name("Los Angeles Lakers") == "los angeles lakers"

    def test_nfl_full_name(self):
        assert normalize_team_name("Kansas City Chiefs") == "kansas city chiefs"

    def test_mlb_full_name(self):
        assert normalize_team_name("New York Yankees") == "new york yankees"

    def test_nhl_full_name(self):
        assert normalize_team_name("Toronto Maple Leafs") == "toronto maple leafs"

    def test_case_insensitive(self):
        assert normalize_team_name("BOSTON CELTICS") == "boston celtics"
        assert normalize_team_name("boston celtics") == "boston celtics"
        assert normalize_team_name("Boston CELTICS") == "boston celtics"

    def test_mixed_crazy_case(self):
        """lA LaKeRs should still resolve."""
        assert normalize_team_name("lA LaKeRs") == "los angeles lakers"


# =====================================================================
# Failure Mode 1: Alias Collisions (LA teams)
# =====================================================================

class TestAliasCollisions:
    """LA Clippers, LA Lakers, LA Chargers, LA Rams, LA Kings, LA Angels, LA Dodgers
    must NEVER be confused with each other."""

    def test_la_lakers_vs_la_clippers(self):
        assert normalize_team_name("LA Lakers") == "los angeles lakers"
        assert normalize_team_name("LA Clippers") == "los angeles clippers"
        assert normalize_team_name("LA Lakers") != normalize_team_name("LA Clippers")

    def test_la_chargers_vs_la_rams(self):
        assert normalize_team_name("LA Chargers") == "los angeles chargers"
        assert normalize_team_name("LA Rams") == "los angeles rams"

    def test_la_angels_vs_la_dodgers(self):
        assert normalize_team_name("LA Angels") == "los angeles angels"
        assert normalize_team_name("LA Dodgers") == "los angeles dodgers"

    def test_la_kings_nhl(self):
        assert normalize_team_name("LA Kings", league="nhl") == "los angeles kings"

    def test_all_la_teams_distinct(self):
        teams = {
            normalize_team_name("LA Lakers"),
            normalize_team_name("LA Clippers"),
            normalize_team_name("LA Chargers"),
            normalize_team_name("LA Rams"),
            normalize_team_name("LA Angels"),
            normalize_team_name("LA Dodgers"),
            normalize_team_name("LA Kings"),
        }
        assert len(teams) == 7  # All 7 must be distinct


# =====================================================================
# Failure Mode 2: City-Name Ambiguity
# =====================================================================

class TestCityAmbiguity:
    """Teams sharing city names must resolve correctly."""

    def test_new_york_teams_nfl(self):
        """NY Giants and NY Jets are different teams."""
        assert normalize_team_name("NY Giants") == "new york giants"
        assert normalize_team_name("NY Jets") == "new york jets"
        assert normalize_team_name("NY Giants") != normalize_team_name("NY Jets")

    def test_new_york_teams_mlb(self):
        assert normalize_team_name("NY Mets") == "new york mets"
        assert normalize_team_name("NY Yankees") == "new york yankees"

    def test_new_york_teams_nhl(self):
        assert normalize_team_name("NY Islanders") == "new york islanders"
        assert normalize_team_name("NY Rangers") == "new york rangers"

    def test_chicago_teams(self):
        assert normalize_team_name("Chicago Bulls", league="nba") == "chicago bulls"
        assert normalize_team_name("Chicago Bears", league="nfl") == "chicago bears"
        assert normalize_team_name("Chicago Cubs", league="mlb") == "chicago cubs"
        assert normalize_team_name("Chicago White Sox", league="mlb") == "chicago white sox"
        assert normalize_team_name("Chicago Blackhawks", league="nhl") == "chicago blackhawks"

    def test_miami_teams(self):
        """Miami Heat vs Miami Dolphins vs Miami Marlins."""
        assert normalize_team_name("Miami Heat", league="nba") == "miami heat"
        assert normalize_team_name("Miami Dolphins", league="nfl") == "miami dolphins"
        assert normalize_team_name("Miami Marlins", league="mlb") == "miami marlins"


# =====================================================================
# Failure Mode 3: Cross-League Short Name Ambiguity
# =====================================================================

class TestCrossLeagueAmbiguity:
    """Short names that exist in multiple leagues must be handled safely."""

    def test_kings_ambiguous_without_league(self):
        assert is_ambiguous_name("Kings")
        assert is_ambiguous_name("kings")

    def test_kings_resolved_with_league(self):
        assert normalize_team_name("Kings", league="nba") == "sacramento kings"
        assert normalize_team_name("Kings", league="nhl") == "los angeles kings"

    def test_panthers_ambiguous(self):
        assert is_ambiguous_name("Panthers")
        assert normalize_team_name("Panthers", league="nfl") == "carolina panthers"
        assert normalize_team_name("Panthers", league="nhl") == "florida panthers"

    def test_cardinals_ambiguous(self):
        assert is_ambiguous_name("Cardinals")
        assert normalize_team_name("Cardinals", league="nfl") == "arizona cardinals"
        assert normalize_team_name("Cardinals", league="mlb") == "st louis cardinals"

    def test_rangers_ambiguous(self):
        assert is_ambiguous_name("Rangers")
        assert normalize_team_name("Rangers", league="mlb") == "texas rangers"
        assert normalize_team_name("Rangers", league="nhl") == "new york rangers"

    def test_jets_ambiguous(self):
        assert is_ambiguous_name("Jets")
        assert normalize_team_name("Jets", league="nfl") == "new york jets"
        assert normalize_team_name("Jets", league="nhl") == "winnipeg jets"

    def test_giants_ambiguous(self):
        assert is_ambiguous_name("Giants")
        assert normalize_team_name("Giants", league="nfl") == "new york giants"
        assert normalize_team_name("Giants", league="mlb") == "san francisco giants"

    def test_ambiguous_without_league_returns_cleaned(self):
        result = normalize_team_name("Kings")
        assert result == "kings"  # Just the cleaned text

    def test_ambiguous_resolution_status(self):
        """Ambiguous names must return AMBIGUOUS status, not UNRESOLVED."""
        res = resolve_team_name("Kings")
        assert res.status == ResolutionStatus.AMBIGUOUS

    def test_unambiguous_short_names_work_globally(self):
        """Short names unique to one league should work without league context."""
        assert normalize_team_name("Celtics") == "boston celtics"
        assert normalize_team_name("Cowboys") == "dallas cowboys"
        assert normalize_team_name("Yankees") == "new york yankees"
        assert normalize_team_name("Bruins") == "boston bruins"

    # User-requested cross-league edge cases
    def test_ny_rangers_vs_texas_rangers(self):
        assert normalize_team_name("NY Rangers") == "new york rangers"
        assert normalize_team_name("Texas Rangers") == "texas rangers"

    def test_la_kings_vs_sacramento_kings(self):
        assert normalize_team_name("LA Kings", league="nhl") == "los angeles kings"
        assert normalize_team_name("Sacramento Kings") == "sacramento kings"

    def test_giants_mlb_vs_nfl(self):
        assert normalize_team_name("SF Giants") == "san francisco giants"
        assert normalize_team_name("NY Giants") == "new york giants"

    def test_cardinals_mlb_vs_nfl(self):
        assert normalize_team_name("Arizona Cardinals") == "arizona cardinals"
        assert normalize_team_name("STL Cardinals", league="mlb") == "st louis cardinals"


# =====================================================================
# Failure Mode 4: Abbreviation Handling
# =====================================================================

class TestAbbreviations:
    def test_kc_prefix(self):
        assert normalize_team_name("KC Chiefs") == "kansas city chiefs"

    def test_okc_prefix(self):
        assert normalize_team_name("OKC Thunder") == "oklahoma city thunder"

    def test_tb_prefix(self):
        assert normalize_team_name("TB Buccaneers") == "tampa bay buccaneers"

    def test_sf_prefix(self):
        assert normalize_team_name("SF 49ers") == "san francisco 49ers"

    def test_stl_prefix(self):
        assert normalize_team_name("STL Cardinals", league="mlb") == "st louis cardinals"

    def test_gs_prefix(self):
        assert normalize_team_name("GS Warriors") == "golden state warriors"

    def test_abbreviation_doesnt_match_mid_word(self):
        result = _clean_text("Lancaster Ducks")
        assert result == "lancaster ducks"


# =====================================================================
# Failure Mode 5: Unicode and Punctuation
# =====================================================================

class TestUnicodeAndPunctuation:
    def test_smart_quotes(self):
        assert normalize_team_name("\u201cBoston Celtics\u201d") == "boston celtics"

    def test_accented_characters(self):
        assert normalize_team_name("Montréal Canadiens") == "montreal canadiens"

    def test_em_dash(self):
        assert normalize_team_name("Tampa Bay\u2014Lightning") == "tampa bay lightning"

    def test_en_dash(self):
        assert normalize_team_name("Tampa Bay\u2013Lightning") == "tampa bay lightning"

    def test_periods_in_abbreviations(self):
        assert normalize_team_name("L.A. Lakers") == "los angeles lakers"
        assert normalize_team_name("S.F. 49ers", league="nfl") == "san francisco 49ers"

    def test_extra_whitespace(self):
        assert normalize_team_name("  Boston   Celtics  ") == "boston celtics"

    def test_apostrophe_variations(self):
        result1 = _clean_text("O'Brien")
        result2 = _clean_text("O\u2019Brien")
        assert result1 == result2

    def test_non_breaking_space(self):
        """Non-breaking space (\\u00a0) must be treated as a regular space."""
        assert normalize_team_name("Boston\u00a0Celtics") == "boston celtics"

    def test_zero_width_characters(self):
        """Zero-width junk must be stripped."""
        assert normalize_team_name("Boston\u200bCeltics") == "boston celtics"
        assert normalize_team_name("Lakers\ufeff") == "los angeles lakers"

    def test_tabs_and_weird_separators(self):
        """Tabs and multiple spaces must collapse."""
        assert normalize_team_name("Boston\t\tCeltics") == "boston celtics"

    def test_montreal_vs_montréal(self):
        """Both forms must resolve to the same canonical."""
        assert normalize_team_name("Montreal Canadiens") == normalize_team_name("Montréal Canadiens")


# =====================================================================
# Failure Mode: Unknown Teams
# =====================================================================

class TestUnknownTeams:
    def test_unknown_team_returns_cleaned(self):
        result = normalize_team_name("Faketown Unicorns")
        assert result == "faketown unicorns"

    def test_unknown_team_not_in_aliases(self):
        result = normalize_team_name("Real Madrid", league="epl")
        assert result == "real madrid"

    def test_college_team_not_in_pro_aliases(self):
        """College teams should not silently resolve to pro teams."""
        result = normalize_team_name("Miami Hurricanes")
        assert "miami heat" != result
        assert result == "miami hurricanes"

    def test_unknown_is_unresolved_not_ambiguous(self):
        """Unknown names should be UNRESOLVED, not AMBIGUOUS."""
        res = resolve_team_name("Faketown Unicorns")
        assert res.status == ResolutionStatus.UNRESOLVED


# =====================================================================
# Fuzzy Matching — Conservative
# =====================================================================

class TestFuzzyMatching:
    def test_minor_typo(self):
        result = fuzzy_normalize_team("Los Angls Lakers")
        assert result == "los angeles lakers"

    def test_fuzzy_with_league_scope(self):
        result = fuzzy_normalize_team("Sacremento Kings", league="nba")
        assert result == "sacramento kings"

    def test_fuzzy_refuses_low_confidence(self):
        result = fuzzy_normalize_team("Zebra Stripers", league="nba")
        assert result == "zebra stripers"

    def test_fuzzy_blacklisted_without_league(self):
        """Blacklisted tokens should NOT fuzzy match without league context."""
        result = fuzzy_normalize_team("Heat")
        # Should return cleaned text, not try to fuzzy-match to Miami Heat
        assert result == "heat"

    def test_fuzzy_blacklisted_with_league_resolves(self):
        """Blacklisted tokens WITH league context should resolve deterministically."""
        result = fuzzy_normalize_team("Heat", league="nba")
        assert result == "miami heat"
