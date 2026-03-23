"""
Adversarial tests for league_normalizer.py

Failure modes covered:
  1. Standard league name resolution
  2. Vendor-specific format keys (The Odds API)
  3. Unknown league stays unknown
  4. Resolution status (CANONICAL vs UNRESOLVED)
  5. Weird / partial league names
"""

import pytest
from backend.matcher.league_normalizer import (
    normalize_league,
    resolve_league,
    LeagueResolutionStatus,
)


class TestLeagueResolution:
    """resolve_league() must return explicit status metadata."""

    def test_canonical_simple(self):
        res = resolve_league("NBA")
        assert res.is_canonical is True
        assert res.status == LeagueResolutionStatus.CANONICAL
        assert res.name == "nba"

    def test_canonical_odds_api_key(self):
        res = resolve_league("basketball_nba")
        assert res.is_canonical is True
        assert res.name == "nba"

    def test_unresolved_unknown_league(self):
        res = resolve_league("Martian League")
        assert res.is_canonical is False
        assert res.status == LeagueResolutionStatus.UNRESOLVED
        assert res.name == "martian league"


class TestStandardLeagues:
    def test_nba(self):
        assert normalize_league("NBA") == "nba"
        assert normalize_league("nba") == "nba"

    def test_nfl(self):
        assert normalize_league("NFL") == "nfl"
        assert normalize_league("National Football League") == "nfl"

    def test_mlb(self):
        assert normalize_league("MLB") == "mlb"
        assert normalize_league("Major League Baseball") == "mlb"

    def test_nhl(self):
        assert normalize_league("NHL") == "nhl"
        assert normalize_league("National Hockey League") == "nhl"

    def test_ncaab(self):
        assert normalize_league("NCAAB") == "ncaab"
        assert normalize_league("College Basketball") == "ncaab"

    def test_ncaaf(self):
        assert normalize_league("NCAAF") == "ncaaf"
        assert normalize_league("CFB") == "ncaaf"

    def test_epl(self):
        assert normalize_league("Premier League") == "epl"
        assert normalize_league("English Premier League") == "epl"

    def test_champions_league(self):
        assert normalize_league("Champions League") == "ucl"
        assert normalize_league("UEFA Champions League") == "ucl"


class TestOddsApiKeys:
    """The Odds API sends sport keys like 'basketball_nba'."""

    def test_nba_key(self):
        assert normalize_league("basketball_nba") == "nba"

    def test_nfl_key(self):
        assert normalize_league("americanfootball_nfl") == "nfl"

    def test_mlb_key(self):
        assert normalize_league("baseball_mlb") == "mlb"

    def test_nhl_key(self):
        assert normalize_league("icehockey_nhl") == "nhl"

    def test_epl_key(self):
        assert normalize_league("soccer_epl") == "epl"


class TestUnknownLeagues:
    """Unknown leagues must stay unknown — no silent guessing."""

    def test_unknown_returns_cleaned(self):
        assert normalize_league("Weird League Name") == "weird league name"

    def test_unknown_is_unresolved(self):
        res = resolve_league("Underwater Polo")
        assert res.status == LeagueResolutionStatus.UNRESOLVED

    def test_partial_name_doesnt_guess(self):
        """'Basket' should NOT silently map to nba."""
        res = resolve_league("Basket")
        assert res.is_canonical is False

    def test_case_insensitive(self):
        assert normalize_league("nBa") == "nba"
        assert normalize_league("PREMIER LEAGUE") == "epl"
