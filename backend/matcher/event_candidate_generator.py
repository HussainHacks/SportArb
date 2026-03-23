"""
Event Candidate Generator — builds candidate pairs for matching.

Uses cheap filters to avoid O(n²) all-pairs comparison:
  1. Same normalized league
  2. Start time within 4-hour window
  3. At least one participant name overlap

CRITICAL INVARIANT:
  Normalization can reduce ambiguity. It CANNOT create compatibility.
  Candidate generation only creates pairs for scoring — it does NOT
  confirm matches. Scoring and line checks belong downstream.

Resolution-aware filtering:
  Records with unresolved leagues are treated as lower confidence
  and logged for feed debugging.
"""

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from backend.matcher.team_normalizer import (
    resolve_team_name,
    ResolutionStatus,
    TeamResolution,
)
from backend.matcher.league_normalizer import (
    resolve_league,
    LeagueResolutionStatus,
    LeagueResolution,
)
from backend.matcher.market_normalizer import (
    resolve_market,
    resolve_period,
    MarketResolution,
    PeriodResolution,
)
from backend.matcher.selection_normalizer import (
    resolve_selection,
    SelectionResolution,
)

logger = logging.getLogger(__name__)

# Maximum time difference to consider two events as potential matches
CANDIDATE_TIME_WINDOW = timedelta(hours=4)


class NormalizedRecord:
    """
    A normalized odds record used as input to the matching engine.

    This is the intermediate representation after text normalization
    but before canonical event assignment.

    Uses resolve_*() APIs (NOT the legacy string wrappers) so that
    resolution status is preserved for downstream safety checks.

    All raw values are preserved for debugging and audit.
    """

    def __init__(
        self,
        source: str,
        source_event_id: str,
        league: str,
        sport: str,
        start_time_utc: datetime,
        home_team: str,
        away_team: str,
        market_type: str,
        period: str,
        selection: str,
        line_value: float | None,
        odds_decimal: float,
        status: str = "active",
        is_live: bool = False,
        timestamp_utc: datetime | None = None,
    ):
        self.source = source
        self.source_event_id = source_event_id

        # --- League resolution ---
        self._league_resolution: LeagueResolution = resolve_league(league)
        self.league = self._league_resolution.name
        self.league_is_canonical = self._league_resolution.is_canonical

        self.sport = sport.lower().strip()
        self.start_time_utc = start_time_utc

        # --- Team resolution (with league context) ---
        resolved_league = self.league if self.league_is_canonical else ""
        self._home_resolution: TeamResolution = resolve_team_name(home_team, league=resolved_league)
        self._away_resolution: TeamResolution = resolve_team_name(away_team, league=resolved_league)
        self.home_team = self._home_resolution.name
        self.away_team = self._away_resolution.name
        self.home_is_canonical = self._home_resolution.is_canonical
        self.away_is_canonical = self._away_resolution.is_canonical

        self.participant_set = frozenset([self.home_team, self.away_team])

        # --- Market resolution ---
        self._market_resolution: MarketResolution = resolve_market(market_type)
        self.market_type = self._market_resolution.name
        self.market_is_canonical = self._market_resolution.is_canonical

        # --- Period resolution ---
        self._period_resolution: PeriodResolution = resolve_period(period)
        self.period = self._period_resolution.name
        self.period_is_canonical = self._period_resolution.is_canonical

        # --- Selection resolution (needs team context) ---
        self._selection_resolution: SelectionResolution = resolve_selection(
            selection,
            home_team=self.home_team,
            away_team=self.away_team,
            source=source,
        )
        self.selection = self._selection_resolution.name
        self.selection_is_canonical = self._selection_resolution.is_canonical

        self.line_value = line_value
        self.odds_decimal = odds_decimal
        self.status = status
        self.is_live = is_live
        self.timestamp_utc = timestamp_utc or datetime.now(timezone.utc)

        # Raw values preserved for debugging
        self._raw = {
            "league": league,
            "home_team": home_team,
            "away_team": away_team,
            "market_type": market_type,
            "period": period,
            "selection": selection,
        }

        # Log resolution warnings for debugging bad feeds
        self._log_resolution_warnings()

    @property
    def is_fully_resolved(self) -> bool:
        """True only if ALL fields resolved to canonical names."""
        return (
            self.league_is_canonical
            and self.home_is_canonical
            and self.away_is_canonical
            and self.market_is_canonical
            and self.period_is_canonical
            and self.selection_is_canonical
        )

    def _log_resolution_warnings(self):
        """Log when any field couldn't be fully resolved."""
        label = f"{self.source}:{self.source_event_id}"

        if not self.league_is_canonical:
            logger.warning(
                f"⚠️ UNRESOLVED LEAGUE in {label}: "
                f"'{self._raw['league']}' → '{self.league}'"
            )

        if not self.home_is_canonical:
            status = self._home_resolution.status.value
            logger.warning(
                f"⚠️ {status.upper()} HOME TEAM in {label}: "
                f"'{self._raw['home_team']}' → '{self.home_team}'"
            )

        if not self.away_is_canonical:
            status = self._away_resolution.status.value
            logger.warning(
                f"⚠️ {status.upper()} AWAY TEAM in {label}: "
                f"'{self._raw['away_team']}' → '{self.away_team}'"
            )

        if not self.market_is_canonical:
            logger.warning(
                f"⚠️ UNRESOLVED MARKET in {label}: "
                f"'{self._raw['market_type']}' → '{self.market_type}' "
                f"(source: {self.source})"
            )

        if not self.selection_is_canonical:
            status = self._selection_resolution.status.value
            logger.warning(
                f"⚠️ {status.upper()} SELECTION in {label}: "
                f"'{self._raw['selection']}' → '{self.selection}' "
                f"(source: {self.source})"
            )

    def __repr__(self):
        resolved = "✅" if self.is_fully_resolved else "⚠️"
        return (
            f"NormalizedRecord({resolved} {self.source}, {self.home_team} vs {self.away_team}, "
            f"{self.league}, {self.market_type})"
        )


class EventCandidateGenerator:
    """
    Generates candidate event matches using cheap pre-filters.

    Avoids expensive all-pairs scoring by filtering on league + time + participants.
    Resolution-aware: logs rejection reasons by bucket for feed debugging.

    Usage of is_fully_resolved:
      - Hard gate for production matching and alerts
      - NOT a reason to discard useful audit data early
      Partially resolved records still survive for inspection and logging.
    """

    def __init__(self):
        self._stats = {
            "total_comparisons": 0,
            "candidates_found": 0,
            # Rejection reason buckets
            "rejected_league_mismatch": 0,
            "rejected_time_window": 0,
            "rejected_participant_overlap": 0,
            # Efficiency tracking
            "find_calls": 0,
            "max_candidates_per_event": 0,
        }

    @property
    def stats(self) -> dict[str, int | float]:
        """Return candidate generation statistics with efficiency metrics."""
        s = dict(self._stats)
        if s["find_calls"] > 0:
            s["avg_candidates_per_event"] = round(
                s["candidates_found"] / s["find_calls"], 2
            )
        else:
            s["avg_candidates_per_event"] = 0.0
        return s

    def find_candidates(
        self,
        record: NormalizedRecord,
        existing_events: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """
        Find existing canonical events that could match a new record.

        Pre-filters (applied in order, cheapest first):
          1. Same league (cheap string compare)
          2. Start time within CANDIDATE_TIME_WINDOW
          3. At least one participant overlap (set intersection)

        Records with unresolved leagues still participate but are
        logged for monitoring — feed issues show up in NormalizedRecord
        warning logs, not filtered here.

        Returns a list of candidate events for scoring.
        """
        self._stats["find_calls"] += 1
        candidates = []

        for event in existing_events:
            self._stats["total_comparisons"] += 1

            # Filter 1: League must match
            if event["league"] != record.league:
                self._stats["rejected_league_mismatch"] += 1
                continue

            # Filter 2: Start time within window
            time_diff = abs((event["start_time_utc"] - record.start_time_utc).total_seconds())
            if time_diff > CANDIDATE_TIME_WINDOW.total_seconds():
                self._stats["rejected_time_window"] += 1
                continue

            # Filter 3: At least one participant overlap
            event_participants = frozenset(event.get("participant_set", []))
            if not record.participant_set.intersection(event_participants):
                self._stats["rejected_participant_overlap"] += 1
                continue

            candidates.append(event)

        self._stats["candidates_found"] += len(candidates)
        self._stats["max_candidates_per_event"] = max(
            self._stats["max_candidates_per_event"], len(candidates)
        )

        return candidates


