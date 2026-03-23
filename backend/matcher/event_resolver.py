"""
Event Resolver — creates and manages canonical events.

When a new record arrives:
  1. Generate candidates (cheap pre-filters)
  2. Score each candidate
  3. If auto-match (>= 80) AND structurally sound → attach to existing event
  4. If no match → create a new canonical event
  5. Probable matches (65-79) are logged for review but NOT auto-attached

Safety rules:
  - No auto-match unless score is_structurally_sound (both teams +
    league canonical). A high score from unresolved components is
    suspicious and should not silently merge events.
  - Canonical event IDs are deterministic and stable.
  - Source references are preserved, never silently overwritten.
  - Canonical fields (home_team, away_team, league) are set once
    from the first source and not silently mutated by later sources.

Failure modes defended against:
  - Accidental merging of separate events (e.g. back-to-back games)
  - Accidental splitting of same event into two canonicals
  - Silent overwrites of canonical fields from less-trusted sources
  - Non-deterministic canonical event IDs
"""

import logging
from datetime import datetime
from typing import Any

from backend.matcher.event_candidate_generator import (
    NormalizedRecord,
    EventCandidateGenerator,
)
from backend.matcher.event_scorer import EventScorer, MatchScore, AUTO_MATCH_THRESHOLD

logger = logging.getLogger(__name__)


class EventResolver:
    """
    Manages canonical events and resolves new records to them.

    Source precedence policy (current: first-source-wins):
      - First source to create a canonical event sets the canonical fields
      - Later sources attach to it but do NOT overwrite home_team,
        away_team, league, or start_time_utc
      - All source references are preserved in source_events list

    NOTE: First-source-wins is a deterministic placeholder policy,
    not a claim that the first source is most trustworthy. Future
    override paths may include:
      - trusted-source-wins (source trust ranking)
      - earliest-source-wins (timestamp-based)
      - majority-consensus-wins (multiple sources agree)
    """

    def __init__(self):
        self._canonical_events: list[dict[str, Any]] = []
        self._candidate_gen = EventCandidateGenerator()
        self._scorer = EventScorer()
        self._stats = {
            "events_created": 0,
            "events_matched": 0,
            "structural_rejects": 0,
            "probable_not_attached": 0,
        }

    @property
    def events(self) -> list[dict[str, Any]]:
        return list(self._canonical_events)

    @property
    def stats(self) -> dict[str, int]:
        """Return resolver statistics."""
        return dict(self._stats)

    def resolve(self, record: NormalizedRecord) -> dict[str, Any]:
        """
        Resolve a normalized record to a canonical event.

        Returns the canonical event dict (existing or newly created).

        Rules:
          1. Score >= 80 AND structurally_sound → attach to existing
          2. Score >= 80 but NOT structurally_sound → log warning, create new
          3. Score 65-79 → log for review, create new
          4. Score < 65 → create new
        """
        candidates = self._candidate_gen.find_candidates(
            record, self._canonical_events
        )

        best_match = None
        best_score: MatchScore | None = None
        best_total = 0

        for candidate in candidates:
            score = self._scorer.score(record, candidate)

            if score.total > best_total:
                best_total = score.total
                best_score = score
                best_match = candidate

                # High confidence + structurally sound → early exit
                if score.is_auto_match and score.is_structurally_sound:
                    break

        if (
            best_total >= AUTO_MATCH_THRESHOLD
            and best_match is not None
            and best_score is not None
        ):
            if best_score.is_structurally_sound:
                # Safe to attach — canonical resolution backs the score
                self._attach_source(best_match, record)
                self._stats["events_matched"] += 1
                logger.debug(
                    f"Matched '{record.source}:{record.source_event_id}' → "
                    f"'{best_match['canonical_event_id']}' "
                    f"(score: {best_total}, structural: ✅)"
                )
                return best_match
            else:
                # High score but weak composition — do NOT merge
                self._stats["structural_rejects"] += 1
                logger.warning(
                    f"⚠️ STRUCTURAL REJECT: score {best_total} but NOT "
                    f"structurally sound for '{record.home_team} vs {record.away_team}' "
                    f"from {record.source} — creating separate canonical event. "
                    f"teams_canonical={best_score.both_teams_canonical}, "
                    f"league_canonical={best_score.league_canonical}"
                )

        # Log probable matches that weren't attached
        if best_total >= 65 and (best_score is None or not best_score.is_structurally_sound or best_total < AUTO_MATCH_THRESHOLD):
            self._stats["probable_not_attached"] += 1
            logger.info(
                f"Probable match (score {best_total}) not auto-attached: "
                f"'{record.home_team} vs {record.away_team}' from {record.source}"
            )

        # Create new canonical event
        new_event = self._create_event(record)
        self._canonical_events.append(new_event)
        self._stats["events_created"] += 1
        return new_event

    def _create_event(self, record: NormalizedRecord) -> dict[str, Any]:
        """
        Create a new canonical event from a record.

        Canonical ID is deterministic:
          {league}_{date}_{home_team}_{away_team}

        Canonical fields are set ONCE and not overwritten by later sources.
        """
        canonical_id = self._generate_canonical_id(record)

        event: dict[str, Any] = {
            "canonical_event_id": canonical_id,
            "sport": record.sport,
            "league": record.league,
            "start_time_utc": record.start_time_utc,
            "home_team": record.home_team,
            "away_team": record.away_team,
            "participant_set": list(record.participant_set),
            "is_live": record.is_live,
            # Resolution quality of the creating source
            "created_fully_resolved": record.is_fully_resolved,
            "created_by_source": record.source,
            # All source references
            "source_events": [
                {
                    "source": record.source,
                    "source_event_id": record.source_event_id,
                    "timestamp_utc": record.timestamp_utc.isoformat(),
                }
            ],
        }

        logger.debug(
            f"Created canonical event: '{canonical_id}' "
            f"(resolved: {'✅' if record.is_fully_resolved else '⚠️'}, "
            f"source: {record.source})"
        )
        return event

    def _generate_canonical_id(self, record: NormalizedRecord) -> str:
        """
        Generate a deterministic canonical event ID.

        Format: {league}_{date}_{home}_{away}
        Sorted participants ensure stability regardless of home/away order.

        NOTE: This means the ID is stable even if two sources disagree
        on which team is home vs away.
        """
        date_str = record.start_time_utc.strftime("%Y-%m-%d")
        # Sort participants for ID stability
        sorted_participants = sorted(record.participant_set)
        parts = [record.league, date_str] + sorted_participants
        return "_".join(p.replace(" ", "_") for p in parts)

    def _attach_source(self, event: dict[str, Any], record: NormalizedRecord):
        """
        Attach a new source record to an existing canonical event.

        Rules:
          - Source references are appended, never replaced
          - Canonical fields (home_team, league, etc.) are NOT overwritten
          - Duplicate source entries are skipped
        """
        source_entry = {
            "source": record.source,
            "source_event_id": record.source_event_id,
            "timestamp_utc": record.timestamp_utc.isoformat(),
        }

        # Don't add duplicate source entries
        existing = event.get("source_events", [])
        for s in existing:
            if s["source"] == record.source and s["source_event_id"] == record.source_event_id:
                return

        existing.append(source_entry)

    def clear(self):
        """Reset all canonical events (for testing or new scan cycle)."""
        self._canonical_events.clear()
        # Don't reset stats — they accumulate across cycles

    def get_event_by_id(self, canonical_id: str) -> dict[str, Any] | None:
        """Look up a canonical event by ID."""
        for event in self._canonical_events:
            if event["canonical_event_id"] == canonical_id:
                return event
        return None

    def get_events_by_source(self, source: str) -> list[dict[str, Any]]:
        """Get all canonical events that have records from a specific source."""
        result = []
        for event in self._canonical_events:
            for se in event.get("source_events", []):
                if se["source"] == source:
                    result.append(event)
                    break
        return result
