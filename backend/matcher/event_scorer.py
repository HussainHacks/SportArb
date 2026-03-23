"""
Event Scorer — multi-signal confidence scoring for event matching.

Scoring signals:
  A. League match:       +30
  B. Team match:         +35 (exact both) / +20 (one exact + fuzzy)
  C. Start time:         +20 (5min) / +15 (15min) / +8 (60min)
  D. Home/away:          +10 (exact match) / +5 (reversed)
  E. Live state:         +5 (same state)

Resolution-aware penalties:
  - Unresolved teams:    team score capped at +15 (can't trust)
  - Unresolved league:   league score capped at +20

Thresholds:
  >= 80: auto-match
  65-79: probable match (log for review)
  < 65:  reject

CRITICAL INVARIANT:
  Scoring establishes event identity confidence ONLY.
  It does NOT confirm market compatibility or arb validity.
  Same event ≠ same market ≠ arb opportunity.

Explainability:
  Every scored pair logs its full component breakdown:
  league, team, time, home/away, live.
  This makes threshold tuning possible from real data.
"""

import logging
from datetime import datetime
from dataclasses import dataclass, field

from backend.matcher.event_candidate_generator import NormalizedRecord

logger = logging.getLogger(__name__)

# Scoring thresholds
AUTO_MATCH_THRESHOLD = 80
PROBABLE_MATCH_THRESHOLD = 65


@dataclass
class MatchScore:
    """
    Detailed breakdown of a match score.

    Every component is tracked separately for:
      1. Threshold tuning from real data
      2. Explainability logging on every decision
      3. Building regression datasets from false positives/negatives
    """
    league_score: int = 0
    team_score: int = 0
    time_score: int = 0
    orientation_score: int = 0
    live_score: int = 0

    # Resolution quality flags — for downstream filtering
    both_teams_canonical: bool = False
    league_canonical: bool = False

    # Debug context
    record_label: str = ""
    candidate_label: str = ""

    @property
    def total(self) -> int:
        return (
            self.league_score
            + self.team_score
            + self.time_score
            + self.orientation_score
            + self.live_score
        )

    @property
    def is_auto_match(self) -> bool:
        return self.total >= AUTO_MATCH_THRESHOLD

    @property
    def is_probable_match(self) -> bool:
        return PROBABLE_MATCH_THRESHOLD <= self.total < AUTO_MATCH_THRESHOLD

    @property
    def is_rejected(self) -> bool:
        return self.total < PROBABLE_MATCH_THRESHOLD

    @property
    def component_breakdown(self) -> str:
        """Formatted component scores for logging."""
        return (
            f"league +{self.league_score}, team +{self.team_score}, "
            f"time +{self.time_score}, h/a +{self.orientation_score}, "
            f"live +{self.live_score}"
        )

    @property
    def is_structurally_sound(self) -> bool:
        """
        True only if the score is backed by canonical resolution.

        A high total score from unresolved components is structurally
        suspicious — this flag lets downstream code enforce:
          no auto-match unless both teams AND league are canonical

        This is NOT checked inside the scorer itself — it's a flag
        for the resolver and arb engine to use as a hard gate.
        """
        return self.both_teams_canonical and self.league_canonical

    @property
    def is_threshold_edge(self) -> bool:
        """True if score is within ±2 of any threshold boundary."""
        for threshold in (AUTO_MATCH_THRESHOLD, PROBABLE_MATCH_THRESHOLD):
            if abs(self.total - threshold) <= 2:
                return True
        return False

    def __repr__(self):
        return (
            f"MatchScore(total={self.total}, {self.component_breakdown})"
        )


class EventScorer:
    """
    Scores the confidence of a match between two event records.

    Resolution-aware: scores are adjusted based on whether teams
    and leagues resolved to canonical names.
    """

    def __init__(self):
        self._stats = {
            "total_scored": 0,
            "auto_matches": 0,
            "probable_matches": 0,
            "rejected": 0,
        }

    @property
    def stats(self) -> dict[str, int]:
        """Return scoring statistics."""
        return dict(self._stats)

    def score(
        self,
        record: NormalizedRecord,
        candidate: dict,
    ) -> MatchScore:
        """
        Compute match confidence between a new record and a candidate event.

        Logs full component breakdown for every decision:
          ✅ AUTO-MATCH   (>= 80)
          ⚠️ PROBABLE     (65-79)
          ❌ REJECT        (< 65)
        """
        self._stats["total_scored"] += 1

        result = MatchScore()

        # Context labels for logging
        result.record_label = f"{record.source}:{record.home_team} vs {record.away_team}"
        result.candidate_label = candidate.get("canonical_event_id", "?")

        # Track resolution quality
        result.both_teams_canonical = record.home_is_canonical and record.away_is_canonical
        result.league_canonical = record.league_is_canonical

        # A. League match (+30, capped at +20 if unresolved)
        if record.league == candidate.get("league", ""):
            if record.league_is_canonical:
                result.league_score = 30
            else:
                # Unresolved league — lower confidence
                result.league_score = 20

        # B. Team match (+35 exact both, +20 partial, capped at +15 if unresolved)
        raw_team_score = self._score_teams(record, candidate)
        if result.both_teams_canonical:
            result.team_score = raw_team_score
        else:
            # Unresolved teams — cap score, can't fully trust
            result.team_score = min(raw_team_score, 15)

        # C. Start time match (+20/+15/+8/0)
        result.time_score = self._score_time(
            record.start_time_utc,
            candidate.get("start_time_utc"),
        )

        # D. Home/away orientation (+10 match, +5 reversed)
        result.orientation_score = self._score_orientation(record, candidate)

        # E. Live state match (+5)
        if record.is_live == candidate.get("is_live", False):
            result.live_score = 5

        # --- Explainability log ---
        if result.is_auto_match:
            self._stats["auto_matches"] += 1
            logger.info(
                f"✅ AUTO-MATCH ({result.total}/100): "
                f"{result.record_label} → {result.candidate_label} | "
                f"{result.component_breakdown}"
            )
        elif result.is_probable_match:
            self._stats["probable_matches"] += 1
            logger.warning(
                f"⚠️ PROBABLE ({result.total}/100): "
                f"{result.record_label} → {result.candidate_label} | "
                f"{result.component_breakdown}"
            )
        else:
            self._stats["rejected"] += 1
            logger.debug(
                f"❌ REJECT ({result.total}/100): "
                f"{result.record_label} → {result.candidate_label} | "
                f"{result.component_breakdown}"
            )

        # --- Threshold-edge logging (most valuable for tuning) ---
        if result.is_threshold_edge:
            logger.warning(
                f"🔍 THRESHOLD EDGE ({result.total}/100): "
                f"{result.record_label} → {result.candidate_label} | "
                f"{result.component_breakdown} | "
                f"canonical: teams={result.both_teams_canonical}, "
                f"league={result.league_canonical}"
            )

        return result

    def _score_teams(self, record: NormalizedRecord, candidate: dict) -> int:
        """Score participant matching."""
        cand_participants = frozenset(candidate.get("participant_set", []))

        # Exact match: both participants match exactly
        if record.participant_set == cand_participants:
            return 35

        # Partial match: at least one exact overlap
        overlap = record.participant_set.intersection(cand_participants)
        if len(overlap) >= 1:
            # Check if the non-overlapping names are fuzzy matches
            non_overlap_record = record.participant_set - overlap
            non_overlap_cand = cand_participants - overlap

            if non_overlap_record and non_overlap_cand:
                from backend.matcher.team_normalizer import fuzzy_normalize_team
                for r_name in non_overlap_record:
                    for c_name in non_overlap_cand:
                        fuzzy_r = fuzzy_normalize_team(r_name)
                        fuzzy_c = fuzzy_normalize_team(c_name)
                        if fuzzy_r == fuzzy_c:
                            return 20

            # One team matched, other didn't — still partial confidence
            return 20

        return 0

    def _score_time(self, time_a: datetime, time_b: datetime | None) -> int:
        """Score start time proximity."""
        if time_b is None:
            return 0

        diff_seconds = abs((time_a - time_b).total_seconds())

        if diff_seconds <= 300:       # 5 minutes
            return 20
        elif diff_seconds <= 900:     # 15 minutes
            return 15
        elif diff_seconds <= 3600:    # 60 minutes
            return 8
        else:
            return 0

    def _score_orientation(self, record: NormalizedRecord, candidate: dict) -> int:
        """Score home/away orientation."""
        cand_home = candidate.get("home_team", "")
        cand_away = candidate.get("away_team", "")

        if record.home_team == cand_home and record.away_team == cand_away:
            return 10  # Exact orientation

        if record.home_team == cand_away and record.away_team == cand_home:
            return 5   # Reversed but valid

        # Participant sets match (already scored in teams) — partial orientation
        if record.participant_set == frozenset([cand_home, cand_away]):
            return 5

        return 0
