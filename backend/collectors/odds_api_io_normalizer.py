"""
Odds-API.io Normalizer — transforms Odds-API.io JSON into canonical NormalizedOdds.

Handles these mapping differences:
  - Market names: ML → h2h, Spread → spreads, Totals → totals
  - Bookmaker names: DraftKings → draftkings (canonical lowercase)
  - Odds format: string "2.15" → float 2.15
  - Timestamps: per-market updatedAt → leg timestamp
  - Selections: home/away/draw for ML, home/away for Spread, over/under for Totals
  - Lines: hdp for Spread/Totals → point field
"""

import logging
from datetime import datetime, timezone
from typing import Any

from backend.normalization.models import NormalizedOdds, MarketKey, MarketStatus

logger = logging.getLogger(__name__)

# ---- Market name mapping ----
MARKET_MAP: dict[str, MarketKey] = {
    "ml": MarketKey.H2H,
    "1x2": MarketKey.H2H,
    "moneyline": MarketKey.H2H,
    "spread": MarketKey.SPREADS,
    "handicap": MarketKey.SPREADS,
    "asian handicap": MarketKey.SPREADS,
    "totals": MarketKey.TOTALS,
    "over/under": MarketKey.TOTALS,
    "total": MarketKey.TOTALS,
}

# ---- Bookmaker name canonicalization ----
# Maps Odds-API.io display names → our canonical operator keys
BOOKMAKER_CANONICAL: dict[str, str] = {
    "draftkings": "draftkings",
    "fanduel": "fanduel",
    "betmgm": "betmgm",
    "caesars": "williamhill_us",
    "caesars sportsbook": "williamhill_us",
    "espn bet": "espnbet",
    "espnbet": "espnbet",
    "betrivers": "betrivers",
    "fanatics": "fanatics",
    "bet365": "bet365",
    "circa sports": "circasports",
    "circa": "circasports",
    "bally bet": "ballybet",
    "hard rock": "hardrockbet",
    "hard rock bet": "hardrockbet",
}

DISPLAY_NAMES: dict[str, str] = {
    "draftkings": "DraftKings",
    "fanduel": "FanDuel",
    "betmgm": "BetMGM",
    "williamhill_us": "Caesars",
    "espnbet": "ESPN BET",
    "betrivers": "BetRivers",
    "fanatics": "Fanatics",
    "bet365": "Bet365",
    "circasports": "Circa Sports",
    "ballybet": "Bally Bet",
    "hardrockbet": "Hard Rock",
}


def _canonicalize_bookmaker(name: str) -> tuple[str, str]:
    """
    Return (canonical_key, display_name) for a bookmaker.

    Falls back to lowercased name if not in mapping.
    """
    key = BOOKMAKER_CANONICAL.get(name.lower(), name.lower().replace(" ", ""))
    display = DISPLAY_NAMES.get(key, name)
    return key, display


def _parse_odds(value: Any) -> float | None:
    """Safely parse odds value (may be string or float)."""
    if value is None:
        return None
    try:
        v = float(value)
        return v if v > 0 else None
    except (ValueError, TypeError):
        return None


def _parse_timestamp(ts_str: str | None) -> datetime:
    """Parse ISO timestamp, falling back to now(UTC)."""
    if not ts_str:
        return datetime.now(timezone.utc)
    try:
        dt = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except (ValueError, TypeError):
        return datetime.now(timezone.utc)


def _resolve_market_key(market_name: str) -> MarketKey | None:
    """Map Odds-API.io market name to canonical MarketKey."""
    return MARKET_MAP.get(market_name.lower())


def normalize_odds_response(
    raw: dict[str, Any],
    allowed_operators: set[str] | None = None,
) -> list[NormalizedOdds]:
    """
    Normalize a single event's odds response from Odds-API.io.

    Expected shape:
    {
        "id": 123456,
        "home": "Team A",
        "away": "Team B",
        "date": "2025-10-15T15:00:00Z",
        "status": "pending",
        "sport": {"name": "Basketball", "slug": "basketball"},
        "league": {"name": "NBA", "slug": "usa-nba"},
        "bookmakers": {
            "DraftKings": [
                {
                    "name": "ML",
                    "updatedAt": "...",
                    "odds": [{"home": "2.10", "away": "1.80"}]
                }
            ]
        }
    }
    """
    result: list[NormalizedOdds] = []

    event_id = str(raw.get("id", ""))
    home = raw.get("home", "Unknown")
    away = raw.get("away", "Unknown")
    event_name = f"{away} @ {home}"
    date_str = raw.get("date", "")
    commence_time = _parse_timestamp(date_str)
    status_str = raw.get("status", "pending")
    is_live = status_str.lower() == "live"

    sport = raw.get("sport", {})
    league = raw.get("league", {})
    sport_key = f"{sport.get('slug', 'unknown')}_{league.get('slug', 'unknown')}"

    bookmakers = raw.get("bookmakers", {})
    if not isinstance(bookmakers, dict):
        return result

    for bookie_name, markets in bookmakers.items():
        operator_key, operator_display = _canonicalize_bookmaker(bookie_name)

        # Filter to allowed operators if specified
        if allowed_operators and operator_key not in allowed_operators:
            continue

        if not isinstance(markets, list):
            continue

        for market in markets:
            market_name = market.get("name", "")
            market_key = _resolve_market_key(market_name)
            if market_key is None:
                logger.debug(f"Skipping unsupported market: {market_name}")
                continue

            updated_at = _parse_timestamp(market.get("updatedAt"))
            odds_list = market.get("odds", [])

            for odds_entry in odds_list:
                if not isinstance(odds_entry, dict):
                    continue

                # Extract selections based on market type
                selections = _extract_selections(
                    odds_entry, market_key, home, away,
                )

                for selection_name, odds_value, point in selections:
                    if odds_value is None:
                        continue

                    normalized = NormalizedOdds(
                        event_id=event_id,
                        sport_key=sport_key,
                        event_name=event_name,
                        commence_time=commence_time,
                        market_key=market_key,
                        selection=selection_name,
                        point=point,
                        odds=odds_value,
                        operator=operator_key,
                        operator_display=operator_display,
                        timestamp=updated_at,
                        status=MarketStatus.ACTIVE,
                        is_live=is_live,
                        is_player_prop=False,
                    )
                    result.append(normalized)

    return result


def _extract_selections(
    odds_entry: dict,
    market_key: MarketKey,
    home_team: str,
    away_team: str,
) -> list[tuple[str, float | None, float | None]]:
    """
    Extract (selection_name, odds, point) tuples from an odds entry.

    Returns a list of tuples, one per selection.
    """
    selections: list[tuple[str, float | None, float | None]] = []

    if market_key == MarketKey.H2H:
        # ML / 1X2: home, draw, away
        home_odds = _parse_odds(odds_entry.get("home"))
        away_odds = _parse_odds(odds_entry.get("away"))
        draw_odds = _parse_odds(odds_entry.get("draw"))

        if home_odds is not None:
            selections.append((home_team, home_odds, None))
        if away_odds is not None:
            selections.append((away_team, away_odds, None))
        if draw_odds is not None:
            selections.append(("Draw", draw_odds, None))

    elif market_key == MarketKey.SPREADS:
        # Spread: hdp, home, away
        hdp = odds_entry.get("hdp")
        home_odds = _parse_odds(odds_entry.get("home"))
        away_odds = _parse_odds(odds_entry.get("away"))

        if hdp is not None:
            hdp_val = float(hdp)
            if home_odds is not None:
                selections.append((home_team, home_odds, hdp_val))
            if away_odds is not None:
                selections.append((away_team, away_odds, -hdp_val))

    elif market_key == MarketKey.TOTALS:
        # Totals: hdp (line), over, under
        hdp = odds_entry.get("hdp")
        over_odds = _parse_odds(odds_entry.get("over"))
        under_odds = _parse_odds(odds_entry.get("under"))

        point = float(hdp) if hdp is not None else None

        if over_odds is not None:
            selections.append(("Over", over_odds, point))
        if under_odds is not None:
            selections.append(("Under", under_odds, point))

    return selections


def normalize_events_batch(
    events: list[dict[str, Any]],
    allowed_operators: set[str] | None = None,
) -> list[NormalizedOdds]:
    """Normalize a batch of events from Odds-API.io."""
    all_odds = []
    for event in events:
        all_odds.extend(normalize_odds_response(event, allowed_operators))
    logger.info(
        f"Normalized {len(all_odds)} odds lines from {len(events)} events "
        f"(Odds-API.io)"
    )
    return all_odds
