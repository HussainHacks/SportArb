"""
Team Name Normalizer — canonicalizes team names across sportsbooks.

Design principles:
  1. Deterministic alias maps ONLY — no aggressive fuzzy logic
  2. League-scoped aliases to prevent cross-league collisions
  3. Ambiguous short names (e.g., "Heat") require league context
  4. Unknown names stay unknown — no silent guessing
  5. Unicode and hidden punctuation stripped cleanly

Pipeline:
  1. strip unicode confusables (smart quotes, accents, etc.)
  2. lowercase + trim
  3. remove punctuation
  4. collapse whitespace
  5. expand common abbreviations
  6. league-scoped alias lookup (if league provided)
  7. global alias lookup (only for unambiguous names)
  8. if no match → return cleaned text as-is

NEVER silently map an ambiguous name to the wrong team.
"""

import re
import unicodedata
import logging
from enum import Enum
from dataclasses import dataclass

logger = logging.getLogger(__name__)


# ============================================================================
# Resolution Status
# ============================================================================

class ResolutionStatus(Enum):
    """Resolution confidence of a team name normalization."""
    CANONICAL = "canonical"      # Matched a known alias → trustworthy
    AMBIGUOUS = "ambiguous"      # Known ambiguous name, needs league context
    UNRESOLVED = "unresolved"    # No alias found → cleaned text only


@dataclass(frozen=True)
class TeamResolution:
    """Result of team name normalization with explicit resolution metadata."""
    name: str                        # The normalized/cleaned team name
    status: ResolutionStatus         # CANONICAL, AMBIGUOUS, or UNRESOLVED
    is_canonical: bool               # True only if status == CANONICAL
    source_league: str = ""          # League that resolved it (if any)
    raw_input: str = ""              # Original input for debugging

# ============================================================================
# Text Cleaning
# ============================================================================

# Unicode characters that look like ASCII but aren't
UNICODE_REPLACEMENTS: dict[str, str] = {
    "\u2018": "'",   # '
    "\u2019": "'",   # '
    "\u201c": '"',   # "
    "\u201d": '"',   # "
    "\u2013": " ",   # – (en dash → space to preserve word boundary)
    "\u2014": " ",   # — (em dash → space to preserve word boundary)
    "\u00e9": "e",   # é
    "\u00e1": "a",   # á
    "\u00f1": "n",   # ñ
    "\u00fc": "u",   # ü
    "\u00f6": "o",   # ö
}


def _clean_text(text: str) -> str:
    """
    Normalize text for matching.

    Steps:
      1. Strip zero-width characters and non-breaking spaces
      2. Replace unicode confusables with ASCII
      3. Strip accents via NFD decomposition
      4. Lowercase
      5. Remove punctuation (periods, hyphens, apostrophes, etc.)
      6. Collapse whitespace
    """
    # Zero-width space → regular space (preserve word boundary)
    text = text.replace("\u200b", " ")
    # Other zero-width format characters → strip
    text = re.sub(r"[\u200c\u200d\ufeff\u00ad]", "", text)
    # Non-breaking space → normal space
    text = text.replace("\u00a0", " ")

    # Unicode replacements
    for char, replacement in UNICODE_REPLACEMENTS.items():
        text = text.replace(char, replacement)

    # Strip accents: decompose → remove combining marks → recompose
    text = unicodedata.normalize("NFD", text)
    text = "".join(c for c in text if unicodedata.category(c) != "Mn")

    text = text.lower().strip()
    text = re.sub(r"[.\-'\"!,&()]", "", text)    # Remove punctuation
    text = re.sub(r"\s+", " ", text)              # Collapse whitespace
    return text


# ============================================================================
# Abbreviation Expansion
# ============================================================================

# Applied BEFORE alias lookup. Only prefix-based to avoid mid-word collisions.
ABBREVIATIONS: list[tuple[str, str]] = [
    ("la ", "los angeles "),
    ("ny ", "new york "),
    ("nj ", "new jersey "),
    ("sf ", "san francisco "),
    ("sd ", "san diego "),
    ("kc ", "kansas city "),
    ("tb ", "tampa bay "),
    ("gb ", "green bay "),
    ("gs ", "golden state "),
    ("stl ", "st louis "),
    ("okc ", "oklahoma city "),
    ("lv ", "las vegas "),
]


def _expand_abbreviations(text: str) -> str:
    """Expand common prefix abbreviations."""
    for abbr, full in ABBREVIATIONS:
        if text.startswith(abbr):
            text = full + text[len(abbr):]
            break  # Only expand one abbreviation
    return text


# ============================================================================
# League-Scoped Alias Tables
# ============================================================================
# Short/ambiguous names are ONLY in league-scoped tables.
# The global table only contains UNAMBIGUOUS full names.

NBA_ALIASES: dict[str, str] = {
    # Full names (unambiguous)
    "atlanta hawks": "atlanta hawks",
    "boston celtics": "boston celtics",
    "brooklyn nets": "brooklyn nets",
    "charlotte hornets": "charlotte hornets",
    "chicago bulls": "chicago bulls",
    "cleveland cavaliers": "cleveland cavaliers",
    "dallas mavericks": "dallas mavericks",
    "denver nuggets": "denver nuggets",
    "detroit pistons": "detroit pistons",
    "golden state warriors": "golden state warriors",
    "houston rockets": "houston rockets",
    "indiana pacers": "indiana pacers",
    "los angeles clippers": "los angeles clippers",
    "los angeles lakers": "los angeles lakers",
    "memphis grizzlies": "memphis grizzlies",
    "miami heat": "miami heat",
    "milwaukee bucks": "milwaukee bucks",
    "minnesota timberwolves": "minnesota timberwolves",
    "new orleans pelicans": "new orleans pelicans",
    "new york knicks": "new york knicks",
    "oklahoma city thunder": "oklahoma city thunder",
    "orlando magic": "orlando magic",
    "philadelphia 76ers": "philadelphia 76ers",
    "phoenix suns": "phoenix suns",
    "portland trail blazers": "portland trail blazers",
    "sacramento kings": "sacramento kings",
    "san antonio spurs": "san antonio spurs",
    "toronto raptors": "toronto raptors",
    "utah jazz": "utah jazz",
    "washington wizards": "washington wizards",
    # Short names — ONLY safe within NBA context
    "hawks": "atlanta hawks",
    "celtics": "boston celtics",
    "nets": "brooklyn nets",
    "hornets": "charlotte hornets",
    "bulls": "chicago bulls",
    "cavaliers": "cleveland cavaliers",
    "cavs": "cleveland cavaliers",
    "mavericks": "dallas mavericks",
    "mavs": "dallas mavericks",
    "nuggets": "denver nuggets",
    "pistons": "detroit pistons",
    "warriors": "golden state warriors",
    "rockets": "houston rockets",
    "pacers": "indiana pacers",
    "clippers": "los angeles clippers",
    "lakers": "los angeles lakers",
    "grizzlies": "memphis grizzlies",
    "heat": "miami heat",
    "bucks": "milwaukee bucks",
    "timberwolves": "minnesota timberwolves",
    "wolves": "minnesota timberwolves",
    "pelicans": "new orleans pelicans",
    "knicks": "new york knicks",
    "thunder": "oklahoma city thunder",
    "magic": "orlando magic",
    "76ers": "philadelphia 76ers",
    "sixers": "philadelphia 76ers",
    "suns": "phoenix suns",
    "trail blazers": "portland trail blazers",
    "blazers": "portland trail blazers",
    "kings": "sacramento kings",
    "spurs": "san antonio spurs",
    "raptors": "toronto raptors",
    "jazz": "utah jazz",
    "wizards": "washington wizards",
    # Abbreviation-expanded forms
    "los angeles clippers": "los angeles clippers",
    "los angeles lakers": "los angeles lakers",
    "golden state warriors": "golden state warriors",
    "oklahoma city thunder": "oklahoma city thunder",
    # Common book variations
    "la clippers": "los angeles clippers",
    "la lakers": "los angeles lakers",
    "gs warriors": "golden state warriors",
    "okc thunder": "oklahoma city thunder",
    "ny knicks": "new york knicks",
    "philly 76ers": "philadelphia 76ers",
}

NFL_ALIASES: dict[str, str] = {
    "arizona cardinals": "arizona cardinals",
    "atlanta falcons": "atlanta falcons",
    "baltimore ravens": "baltimore ravens",
    "buffalo bills": "buffalo bills",
    "carolina panthers": "carolina panthers",
    "chicago bears": "chicago bears",
    "cincinnati bengals": "cincinnati bengals",
    "cleveland browns": "cleveland browns",
    "dallas cowboys": "dallas cowboys",
    "denver broncos": "denver broncos",
    "detroit lions": "detroit lions",
    "green bay packers": "green bay packers",
    "houston texans": "houston texans",
    "indianapolis colts": "indianapolis colts",
    "jacksonville jaguars": "jacksonville jaguars",
    "kansas city chiefs": "kansas city chiefs",
    "las vegas raiders": "las vegas raiders",
    "los angeles chargers": "los angeles chargers",
    "los angeles rams": "los angeles rams",
    "miami dolphins": "miami dolphins",
    "minnesota vikings": "minnesota vikings",
    "new england patriots": "new england patriots",
    "new orleans saints": "new orleans saints",
    "new york giants": "new york giants",
    "new york jets": "new york jets",
    "philadelphia eagles": "philadelphia eagles",
    "pittsburgh steelers": "pittsburgh steelers",
    "san francisco 49ers": "san francisco 49ers",
    "seattle seahawks": "seattle seahawks",
    "tampa bay buccaneers": "tampa bay buccaneers",
    "tennessee titans": "tennessee titans",
    "washington commanders": "washington commanders",
    # Short names — safe within NFL context
    "cardinals": "arizona cardinals",
    "falcons": "atlanta falcons",
    "ravens": "baltimore ravens",
    "bills": "buffalo bills",
    "panthers": "carolina panthers",
    "bears": "chicago bears",
    "bengals": "cincinnati bengals",
    "browns": "cleveland browns",
    "cowboys": "dallas cowboys",
    "broncos": "denver broncos",
    "lions": "detroit lions",
    "packers": "green bay packers",
    "texans": "houston texans",
    "colts": "indianapolis colts",
    "jaguars": "jacksonville jaguars",
    "jags": "jacksonville jaguars",
    "chiefs": "kansas city chiefs",
    "raiders": "las vegas raiders",
    "chargers": "los angeles chargers",
    "rams": "los angeles rams",
    "dolphins": "miami dolphins",
    "vikings": "minnesota vikings",
    "patriots": "new england patriots",
    "pats": "new england patriots",
    "saints": "new orleans saints",
    "giants": "new york giants",
    "jets": "new york jets",
    "eagles": "philadelphia eagles",
    "steelers": "pittsburgh steelers",
    "49ers": "san francisco 49ers",
    "niners": "san francisco 49ers",
    "seahawks": "seattle seahawks",
    "buccaneers": "tampa bay buccaneers",
    "bucs": "tampa bay buccaneers",
    "titans": "tennessee titans",
    "commanders": "washington commanders",
    # Abbreviations
    "kc chiefs": "kansas city chiefs",
    "lv raiders": "las vegas raiders",
    "la chargers": "los angeles chargers",
    "la rams": "los angeles rams",
    "gb packers": "green bay packers",
    "tb buccaneers": "tampa bay buccaneers",
    "tb bucs": "tampa bay buccaneers",
    "ny giants": "new york giants",
    "ny jets": "new york jets",
    "sf 49ers": "san francisco 49ers",
    "ne patriots": "new england patriots",
}

MLB_ALIASES: dict[str, str] = {
    "arizona diamondbacks": "arizona diamondbacks",
    "atlanta braves": "atlanta braves",
    "baltimore orioles": "baltimore orioles",
    "boston red sox": "boston red sox",
    "chicago cubs": "chicago cubs",
    "chicago white sox": "chicago white sox",
    "cincinnati reds": "cincinnati reds",
    "cleveland guardians": "cleveland guardians",
    "colorado rockies": "colorado rockies",
    "detroit tigers": "detroit tigers",
    "houston astros": "houston astros",
    "kansas city royals": "kansas city royals",
    "los angeles angels": "los angeles angels",
    "los angeles dodgers": "los angeles dodgers",
    "miami marlins": "miami marlins",
    "milwaukee brewers": "milwaukee brewers",
    "minnesota twins": "minnesota twins",
    "new york mets": "new york mets",
    "new york yankees": "new york yankees",
    "oakland athletics": "oakland athletics",
    "philadelphia phillies": "philadelphia phillies",
    "pittsburgh pirates": "pittsburgh pirates",
    "san diego padres": "san diego padres",
    "san francisco giants": "san francisco giants",
    "seattle mariners": "seattle mariners",
    "st louis cardinals": "st louis cardinals",
    "tampa bay rays": "tampa bay rays",
    "texas rangers": "texas rangers",
    "toronto blue jays": "toronto blue jays",
    "washington nationals": "washington nationals",
    # Short names — safe within MLB context
    "diamondbacks": "arizona diamondbacks",
    "dbacks": "arizona diamondbacks",
    "braves": "atlanta braves",
    "orioles": "baltimore orioles",
    "red sox": "boston red sox",
    "cubs": "chicago cubs",
    "white sox": "chicago white sox",
    "reds": "cincinnati reds",
    "guardians": "cleveland guardians",
    "rockies": "colorado rockies",
    "tigers": "detroit tigers",
    "astros": "houston astros",
    "royals": "kansas city royals",
    "angels": "los angeles angels",
    "dodgers": "los angeles dodgers",
    "marlins": "miami marlins",
    "brewers": "milwaukee brewers",
    "twins": "minnesota twins",
    "mets": "new york mets",
    "yankees": "new york yankees",
    "athletics": "oakland athletics",
    "phillies": "philadelphia phillies",
    "pirates": "pittsburgh pirates",
    "padres": "san diego padres",
    "mariners": "seattle mariners",
    "rays": "tampa bay rays",
    "blue jays": "toronto blue jays",
    "nationals": "washington nationals",
    "nats": "washington nationals",
    # Ambiguous short names — resolvable within MLB context
    "cardinals": "st louis cardinals",
    "rangers": "texas rangers",
    "giants": "san francisco giants",
    # Abbreviations
    "la angels": "los angeles angels",
    "la dodgers": "los angeles dodgers",
    "kc royals": "kansas city royals",
    "sf giants": "san francisco giants",
    "sd padres": "san diego padres",
    "ny mets": "new york mets",
    "ny yankees": "new york yankees",
    "stl cardinals": "st louis cardinals",
    "tb rays": "tampa bay rays",
}

NHL_ALIASES: dict[str, str] = {
    "anaheim ducks": "anaheim ducks",
    "boston bruins": "boston bruins",
    "buffalo sabres": "buffalo sabres",
    "calgary flames": "calgary flames",
    "carolina hurricanes": "carolina hurricanes",
    "chicago blackhawks": "chicago blackhawks",
    "colorado avalanche": "colorado avalanche",
    "columbus blue jackets": "columbus blue jackets",
    "dallas stars": "dallas stars",
    "detroit red wings": "detroit red wings",
    "edmonton oilers": "edmonton oilers",
    "florida panthers": "florida panthers",
    "los angeles kings": "los angeles kings",
    "minnesota wild": "minnesota wild",
    "montreal canadiens": "montreal canadiens",
    "nashville predators": "nashville predators",
    "new jersey devils": "new jersey devils",
    "new york islanders": "new york islanders",
    "new york rangers": "new york rangers",
    "ottawa senators": "ottawa senators",
    "philadelphia flyers": "philadelphia flyers",
    "pittsburgh penguins": "pittsburgh penguins",
    "san jose sharks": "san jose sharks",
    "seattle kraken": "seattle kraken",
    "st louis blues": "st louis blues",
    "tampa bay lightning": "tampa bay lightning",
    "toronto maple leafs": "toronto maple leafs",
    "utah hockey club": "utah hockey club",
    "vancouver canucks": "vancouver canucks",
    "vegas golden knights": "vegas golden knights",
    "washington capitals": "washington capitals",
    "winnipeg jets": "winnipeg jets",
    # Short names — safe within NHL context
    "ducks": "anaheim ducks",
    "bruins": "boston bruins",
    "sabres": "buffalo sabres",
    "flames": "calgary flames",
    "hurricanes": "carolina hurricanes",
    "canes": "carolina hurricanes",
    "blackhawks": "chicago blackhawks",
    "avalanche": "colorado avalanche",
    "avs": "colorado avalanche",
    "blue jackets": "columbus blue jackets",
    "stars": "dallas stars",
    "red wings": "detroit red wings",
    "oilers": "edmonton oilers",
    "kings": "los angeles kings",
    "wild": "minnesota wild",
    "canadiens": "montreal canadiens",
    "habs": "montreal canadiens",
    "predators": "nashville predators",
    "preds": "nashville predators",
    "devils": "new jersey devils",
    "islanders": "new york islanders",
    "senators": "ottawa senators",
    "sens": "ottawa senators",
    "flyers": "philadelphia flyers",
    "penguins": "pittsburgh penguins",
    "pens": "pittsburgh penguins",
    "sharks": "san jose sharks",
    "kraken": "seattle kraken",
    "blues": "st louis blues",
    "lightning": "tampa bay lightning",
    "bolts": "tampa bay lightning",
    "maple leafs": "toronto maple leafs",
    "leafs": "toronto maple leafs",
    "canucks": "vancouver canucks",
    "golden knights": "vegas golden knights",
    "vgk": "vegas golden knights",
    "capitals": "washington capitals",
    "caps": "washington capitals",
    # Ambiguous short names — resolvable within NHL context
    "panthers": "florida panthers",
    "rangers": "new york rangers",
    "jets": "winnipeg jets",
    # Abbreviations
    "la kings": "los angeles kings",
    "nj devils": "new jersey devils",
    "ny islanders": "new york islanders",
    "ny rangers": "new york rangers",
    "sj sharks": "san jose sharks",
    "stl blues": "st louis blues",
    "tb lightning": "tampa bay lightning",
}

LEAGUE_ALIAS_TABLES: dict[str, dict[str, str]] = {
    "nba": NBA_ALIASES,
    "nfl": NFL_ALIASES,
    "mlb": MLB_ALIASES,
    "nhl": NHL_ALIASES,
}

# ============================================================================
# Global Alias Table — ONLY unambiguous full names
# ============================================================================
# Short names that exist in multiple leagues (e.g., "kings" = NBA + NHL,
# "panthers" = NFL + NHL, "cardinals" = NFL + MLB, "rangers" = MLB + NHL,
# "jets" = NFL + NHL, "giants" = NFL + MLB) are EXCLUDED from this table.
# They can only resolve with league context.

# Names that are AMBIGUOUS across leagues — never put these in the global table
AMBIGUOUS_SHORT_NAMES: set[str] = {
    "kings",       # Sacramento Kings (NBA) vs Los Angeles Kings (NHL)
    "panthers",    # Carolina Panthers (NFL) vs Florida Panthers (NHL)
    "cardinals",   # Arizona Cardinals (NFL) vs St. Louis Cardinals (MLB)
    "rangers",     # Texas Rangers (MLB) vs New York Rangers (NHL)
    "jets",        # New York Jets (NFL) vs Winnipeg Jets (NHL)
    "giants",      # New York Giants (NFL) vs San Francisco Giants (MLB)
    "blues",       # St. Louis Blues (NHL) — could collide with soccer
    "stars",       # Dallas Stars (NHL) — generic
    "heat",        # Miami Heat (NBA) — but also a generic word
    "magic",       # Orlando Magic (NBA) — also a generic word
    "wild",        # Minnesota Wild (NHL) — also a generic word
}


# Tokens that are too dangerous for fuzzy matching — common English words
# that happen to be team names. Fuzzy matching these without league context
# will produce false matches.
FUZZY_BLACKLIST: set[str] = AMBIGUOUS_SHORT_NAMES | {
    "as",          # Oakland Athletics abbreviation, also a word
}


def _build_global_table() -> dict[str, str]:
    """
    Build the global alias table from all league tables,
    EXCLUDING short names that are ambiguous across leagues.

    Logs all dropped aliases at startup for debugging.
    """
    global_table: dict[str, str] = {}
    dropped: list[str] = []

    for league_name, aliases in LEAGUE_ALIAS_TABLES.items():
        for alias, canonical in aliases.items():
            # Skip ambiguous short names in the global table
            if alias in AMBIGUOUS_SHORT_NAMES:
                continue

            # If the alias is already in the global table with a DIFFERENT
            # canonical, it's a cross-league collision — remove it
            if alias in global_table and global_table[alias] != canonical:
                dropped.append(
                    f"  removed '{alias}': "
                    f"'{global_table[alias]}' vs '{canonical}'"
                )
                del global_table[alias]
                continue

            global_table[alias] = canonical

    # Log all drops at startup
    if dropped:
        logger.info(
            f"Global alias table: dropped {len(dropped)} collision(s):\n"
            + "\n".join(dropped)
        )
    logger.info(
        f"Global alias table built: {len(global_table)} aliases, "
        f"{len(AMBIGUOUS_SHORT_NAMES)} ambiguous names blocked"
    )

    return global_table


GLOBAL_ALIASES: dict[str, str] = _build_global_table()


# ============================================================================
# Public API
# ============================================================================

def resolve_team_name(raw_name: str, league: str = "") -> TeamResolution:
    """
    Normalize a team name and return full resolution metadata.

    This is the primary API. Use this instead of normalize_team_name()
    when you need to know whether the result is trustworthy.

    Returns:
        TeamResolution with name, status, is_canonical, source_league
    """
    cleaned = _clean_text(raw_name)
    expanded = _expand_abbreviations(cleaned)

    # Step 1: League-scoped lookup (highest priority, resolves ambiguity)
    if league:
        league_table = LEAGUE_ALIAS_TABLES.get(league.lower(), {})
        if expanded in league_table:
            return TeamResolution(
                name=league_table[expanded],
                status=ResolutionStatus.CANONICAL,
                is_canonical=True,
                source_league=league.lower(),
                raw_input=raw_name,
            )
        if cleaned in league_table:
            return TeamResolution(
                name=league_table[cleaned],
                status=ResolutionStatus.CANONICAL,
                is_canonical=True,
                source_league=league.lower(),
                raw_input=raw_name,
            )

    # Step 2: Check if it's a known ambiguous name
    if cleaned in AMBIGUOUS_SHORT_NAMES or expanded in AMBIGUOUS_SHORT_NAMES:
        return TeamResolution(
            name=cleaned,
            status=ResolutionStatus.AMBIGUOUS,
            is_canonical=False,
            raw_input=raw_name,
        )

    # Step 3: Global lookup (only unambiguous names)
    if expanded in GLOBAL_ALIASES:
        return TeamResolution(
            name=GLOBAL_ALIASES[expanded],
            status=ResolutionStatus.CANONICAL,
            is_canonical=True,
            source_league="global",
            raw_input=raw_name,
        )
    if cleaned in GLOBAL_ALIASES:
        return TeamResolution(
            name=GLOBAL_ALIASES[cleaned],
            status=ResolutionStatus.CANONICAL,
            is_canonical=True,
            source_league="global",
            raw_input=raw_name,
        )

    # Step 4: No match — unresolved
    logger.debug(f"Unresolved team: '{raw_name}' (cleaned: '{cleaned}')")
    return TeamResolution(
        name=cleaned,
        status=ResolutionStatus.UNRESOLVED,
        is_canonical=False,
        raw_input=raw_name,
    )


def normalize_team_name(raw_name: str, league: str = "") -> str:
    """
    Normalize a team name to its canonical form.

    ⚠️ CONVENIENCE ONLY — returns a plain string with no resolution metadata.
    Do NOT use in matching, scoring, or arb-critical code paths.
    Use resolve_team_name() instead, which returns TeamResolution with
    full status (CANONICAL / AMBIGUOUS / UNRESOLVED).

    This wrapper exists for backward compatibility and tests.
    """
    return resolve_team_name(raw_name, league).name


def fuzzy_normalize_team(raw_name: str, league: str = "", threshold: float = 85.0) -> str:
    """
    Fallback fuzzy matching against alias tables.

    ONLY used as a last resort. Prefer deterministic lookup.
    The fuzzy scorer is conservative — requires high threshold.
    Blacklisted tokens (common English words) are refused without league context.

    Args:
        raw_name: Raw team name
        league: Optional league for scoped search
        threshold: Minimum similarity score (0-100)
    """
    # Try deterministic first
    resolution = resolve_team_name(raw_name, league)
    if resolution.is_canonical:
        return resolution.name

    cleaned = _clean_text(raw_name)

    # Refuse fuzzy matching for blacklisted tokens without league context
    if cleaned in FUZZY_BLACKLIST and not league:
        logger.debug(f"Fuzzy blocked for blacklisted token: '{cleaned}'")
        return cleaned

    # Fuzzy fallback
    try:
        from rapidfuzz import process, fuzz

        # Prefer league-scoped if available
        search_keys = list(GLOBAL_ALIASES.keys())
        if league:
            league_table = LEAGUE_ALIAS_TABLES.get(league.lower(), {})
            if league_table:
                search_keys = list(league_table.keys())

        result = process.extractOne(
            cleaned,
            search_keys,
            scorer=fuzz.token_sort_ratio,
            score_cutoff=threshold,
        )
        if result:
            matched_key, score, _ = result
            # Resolve through the table
            if league and league.lower() in LEAGUE_ALIAS_TABLES:
                canonical = LEAGUE_ALIAS_TABLES[league.lower()].get(matched_key, matched_key)
            else:
                canonical = GLOBAL_ALIASES.get(matched_key, matched_key)

            logger.info(
                f"🔍 Fuzzy matched '{raw_name}' → '{canonical}' "
                f"(score: {score:.1f}, league: {league or 'global'})"
            )
            return canonical

    except ImportError:
        pass

    return cleaned


def is_ambiguous_name(name: str) -> bool:
    """Check if a team name is known to be ambiguous across leagues."""
    cleaned = _clean_text(name)
    return cleaned in AMBIGUOUS_SHORT_NAMES
