import logging
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger(__name__)


def rank_events_for_polling(
    raw_events: list[dict[str, Any]],
    active_arb_event_ids: set[str],
    limit: int = 10,
) -> list[dict[str, Any]]:
    """
    Rank raw events from Odds-API.io to select the best candidates for polling odds.
    
    Ranking criteria:
      1. Events with active arbs get absolute priority.
      2. Events starting soon (< 1 hour) are prioritized.
      3. Events with high bookmaker coverage.
      4. Avoid settled or completed events.
    """
    if not raw_events:
        return []

    now = datetime.now(timezone.utc)
    scored_events = []

    for event in raw_events:
        # Skip events clearly marked completed
        if event.get("completed") or event.get("status") == "settled":
            continue

        event_id = str(event.get("id"))
        score = 0.0

        # Baseline: bookmaker overlap
        bookmakers = event.get("bookmakers", [])
        score += len(bookmakers) * 2  # +2 points per bookmaker

        # Priority: Active arb history
        if event_id in active_arb_event_ids:
            score += 1000.0  # Must poll if it has an active arb

        # Priority: Commence proximity
        commence_str = event.get("commence_time")
        if commence_str:
            try:
                commence = datetime.fromisoformat(commence_str.replace("Z", "+00:00"))
                minutes_until = (commence - now).total_seconds() / 60.0
                
                if 0 < minutes_until <= 60:
                    score += 500.0  # Starting < 1 hour
                elif 60 < minutes_until <= 240:
                    score += 200.0  # Starting < 4 hours
                elif minutes_until <= 0 and minutes_until > -180:
                    score += 300.0  # Live/recently started
            except (ValueError, TypeError):
                pass
                
        scored_events.append((score, event))

    # Sort descending by score
    scored_events.sort(key=lambda x: x[0], reverse=True)
    return [e for _, e in scored_events[:limit]]
