"""
League Normalizer — canonicalizes league/competition names.

Design principles:
  1. Strict deterministic mapping only
  2. Unknown league stays unknown — no guessing
  3. Returns resolution status so downstream can distinguish
  4. Handles vendor-specific format keys (e.g., "basketball_nba")
"""

import re
import logging
from enum import Enum
from dataclasses import dataclass

logger = logging.getLogger(__name__)


class LeagueResolutionStatus(Enum):
    """Resolution confidence of a league normalization."""
    CANONICAL = "canonical"      # Matched a known alias → trustworthy
    UNRESOLVED = "unresolved"    # No alias found → cleaned text only


@dataclass(frozen=True)
class LeagueResolution:
    """Result of league normalization with explicit resolution metadata."""
    name: str                            # The normalized league name
    status: LeagueResolutionStatus       # CANONICAL or UNRESOLVED
    is_canonical: bool                   # True only if status == CANONICAL
    raw_input: str = ""                  # Original input for debugging


LEAGUE_ALIASES: dict[str, str] = {
    # Basketball
    "nba": "nba",
    "national basketball association": "nba",
    "nba basketball": "nba",
    # College Basketball
    "ncaab": "ncaab",
    "ncaa basketball": "ncaab",
    "college basketball": "ncaab",
    "ncaa mens basketball": "ncaab",
    # Football
    "nfl": "nfl",
    "national football league": "nfl",
    "nfl football": "nfl",
    # College Football
    "ncaaf": "ncaaf",
    "ncaa football": "ncaaf",
    "college football": "ncaaf",
    "cfb": "ncaaf",
    # Baseball
    "mlb": "mlb",
    "major league baseball": "mlb",
    "mlb baseball": "mlb",
    # Hockey
    "nhl": "nhl",
    "national hockey league": "nhl",
    "nhl hockey": "nhl",
    # Soccer
    "epl": "epl",
    "english premier league": "epl",
    "premier league": "epl",
    "barclays premier league": "epl",
    "mls": "mls",
    "major league soccer": "mls",
    "ucl": "ucl",
    "champions league": "ucl",
    "uefa champions league": "ucl",
    "uefa cl": "ucl",
    "la liga": "la_liga",
    "laliga": "la_liga",
    "spanish la liga": "la_liga",
    "bundesliga": "bundesliga",
    "german bundesliga": "bundesliga",
    "serie a": "serie_a",
    "italian serie a": "serie_a",
    "ligue 1": "ligue_1",
    "french ligue 1": "ligue_1",
    # The Odds API sport keys → canonical leagues
    "americanfootball_nfl": "nfl",
    "americanfootball_ncaaf": "ncaaf",
    "basketball_nba": "nba",
    "basketball_ncaab": "ncaab",
    "baseball_mlb": "mlb",
    "icehockey_nhl": "nhl",
    "soccer_usa_mls": "mls",
    "soccer_epl": "epl",
    "soccer_uefa_champs_league": "ucl",
    "soccer_spain_la_liga": "la_liga",
    "soccer_germany_bundesliga": "bundesliga",
    "soccer_italy_serie_a": "serie_a",
    "soccer_france_ligue_one": "ligue_1",
}


def _clean_league(raw_league: str) -> str:
    """Clean a league name for lookup."""
    cleaned = raw_league.lower().strip()
    cleaned = re.sub(r"[.\-']", "", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned)
    return cleaned


def resolve_league(raw_league: str) -> LeagueResolution:
    """
    Normalize a league name and return full resolution metadata.

    Returns:
        LeagueResolution with name, status, is_canonical
    """
    cleaned = _clean_league(raw_league)

    if cleaned in LEAGUE_ALIASES:
        return LeagueResolution(
            name=LEAGUE_ALIASES[cleaned],
            status=LeagueResolutionStatus.CANONICAL,
            is_canonical=True,
            raw_input=raw_league,
        )

    # Also try the raw (for keys like "basketball_nba" with underscores)
    raw_lower = raw_league.lower().strip()
    if raw_lower in LEAGUE_ALIASES:
        return LeagueResolution(
            name=LEAGUE_ALIASES[raw_lower],
            status=LeagueResolutionStatus.CANONICAL,
            is_canonical=True,
            raw_input=raw_league,
        )

    logger.debug(f"Unresolved league: '{raw_league}' (cleaned: '{cleaned}')")
    return LeagueResolution(
        name=cleaned,
        status=LeagueResolutionStatus.UNRESOLVED,
        is_canonical=False,
        raw_input=raw_league,
    )


def normalize_league(raw_league: str) -> str:
    """
    Normalize a league name to its canonical form.

    ⚠️ CONVENIENCE ONLY — returns a plain string with no resolution metadata.
    Do NOT use in matching, scoring, or arb-critical code paths.
    Use resolve_league() instead, which returns LeagueResolution with
    full status (CANONICAL / UNRESOLVED).

    This wrapper exists for backward compatibility and tests.
    """
    return resolve_league(raw_league).name
