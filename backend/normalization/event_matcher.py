"""
Event Matcher — groups normalized odds by canonical event + market + period + line.

Grouping key: {event_id}:{market_key}:{period}:{line_value}

This ensures that arb detection only compares odds that are
for the same event, same market type, same period, and same line.
"""

import logging
from collections import defaultdict
from backend.normalization.models import NormalizedOdds, EventGroup

logger = logging.getLogger(__name__)


def _market_group_key(odds: NormalizedOdds) -> str:
    """
    Build a grouping key that ensures only comparable odds are grouped together.

    Key format: {market_key}:{period}:{abs_point}
    """
    point_str = str(abs(odds.point)) if odds.point is not None else "none"
    return f"{odds.market_key.value}:{odds.period}:{point_str}"


def group_odds_by_event(odds_list: list[NormalizedOdds]) -> list[EventGroup]:
    """
    Group a flat list of NormalizedOdds into EventGroups.

    Each EventGroup contains all odds for one event, organized by
    market_key + period + line_value — ensuring the arb engine only
    compares odds that belong to the same market context.
    """
    # Group by event_id
    event_map: dict[str, list[NormalizedOdds]] = defaultdict(list)
    for odds in odds_list:
        event_map[odds.event_id].append(odds)

    event_groups = []
    for event_id, odds_lines in event_map.items():
        if not odds_lines:
            continue

        first = odds_lines[0]

        # Sub-group by market+period+line
        odds_by_market: dict[str, list[NormalizedOdds]] = defaultdict(list)
        for odds in odds_lines:
            key = _market_group_key(odds)
            odds_by_market[key].append(odds)

        group = EventGroup(
            event_id=event_id,
            event_name=first.event_name,
            sport_key=first.sport_key,
            commence_time=first.commence_time,
            odds_by_market=dict(odds_by_market),
        )
        event_groups.append(group)

    logger.info(f"Grouped {len(odds_list)} odds lines into {len(event_groups)} events.")
    return event_groups


def fuzzy_match_team_name(name_a: str, name_b: str, threshold: float = 80.0) -> bool:
    """
    Check if two team names are likely the same using fuzzy matching.

    Used for multi-vendor scenarios where event IDs don't match.
    Requires the `rapidfuzz` library.
    """
    try:
        from rapidfuzz import fuzz
        score = fuzz.token_sort_ratio(name_a.lower(), name_b.lower())
        return score >= threshold
    except ImportError:
        return name_a.lower().strip() == name_b.lower().strip()
