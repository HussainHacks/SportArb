"""
Normalizer — transforms raw Odds API JSON into canonical NormalizedOdds models.
"""

import logging
from datetime import datetime, timezone
from typing import Any

from backend.config import settings
from backend.collectors.odds_api import OPERATOR_DISPLAY_NAMES
from backend.normalization.models import NormalizedOdds, MarketKey, MarketStatus

logger = logging.getLogger(__name__)


def american_to_decimal(american: int | float) -> float:
    """Convert American odds to decimal odds."""
    if american > 0:
        return round(1 + (american / 100), 4)
    elif american < 0:
        return round(1 + (100 / abs(american)), 4)
    else:
        return 1.0  # Edge case


def normalize_event(raw_event: dict[str, Any]) -> list[NormalizedOdds]:
    """
    Transform a single raw event from The Odds API into a list of NormalizedOdds.

    Each event can have multiple bookmakers, each with multiple markets,
    each with multiple outcomes → we flatten this into individual odds lines.
    """
    odds_list = []

    event_id = raw_event.get("id", "")
    sport_key = raw_event.get("sport_key", "")
    home_team = raw_event.get("home_team", "Unknown")
    away_team = raw_event.get("away_team", "Unknown")
    event_name = f"{away_team} @ {home_team}"
    commence_time_str = raw_event.get("commence_time", "")

    try:
        commence_time = datetime.fromisoformat(commence_time_str.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        commence_time = datetime.now(timezone.utc)

    bookmakers = raw_event.get("bookmakers", [])

    for bookmaker in bookmakers:
        operator_key = bookmaker.get("key", "").lower()

        # Filter to Iowa-legal operators only
        if operator_key not in settings.iowa_legal_operators:
            continue

        operator_display = OPERATOR_DISPLAY_NAMES.get(operator_key, bookmaker.get("title", operator_key))
        last_update_str = bookmaker.get("last_update", "")

        try:
            timestamp = datetime.fromisoformat(last_update_str.replace("Z", "+00:00"))
        except (ValueError, AttributeError):
            timestamp = datetime.now(timezone.utc)

        markets = bookmaker.get("markets", [])

        for market in markets:
            market_key_str = market.get("key", "")

            # Map to our canonical MarketKey
            try:
                market_key = MarketKey(market_key_str)
            except ValueError:
                logger.debug(f"Skipping unsupported market type: {market_key_str}")
                continue

            outcomes = market.get("outcomes", [])

            for outcome in outcomes:
                selection = outcome.get("name", "")
                odds_value = outcome.get("price", 0.0)
                point = outcome.get("point", None)

                # The Odds API returns decimal when requested, but safeguard:
                if isinstance(odds_value, (int, float)) and odds_value > 0:
                    normalized = NormalizedOdds(
                        event_id=event_id,
                        sport_key=sport_key,
                        event_name=event_name,
                        commence_time=commence_time,
                        market_key=market_key,
                        selection=selection,
                        point=point,
                        odds=float(odds_value),
                        operator=operator_key,
                        operator_display=operator_display,
                        timestamp=timestamp,
                        status=MarketStatus.ACTIVE,
                        is_player_prop=False,  # The Odds API h2h/spreads/totals are not player props
                    )
                    odds_list.append(normalized)

    return odds_list


def normalize_all(raw_events: list[dict[str, Any]]) -> list[NormalizedOdds]:
    """Normalize a batch of raw events into canonical odds lines."""
    all_odds = []
    for event in raw_events:
        all_odds.extend(normalize_event(event))
    logger.info(f"Normalized {len(all_odds)} odds lines from {len(raw_events)} events.")
    return all_odds
