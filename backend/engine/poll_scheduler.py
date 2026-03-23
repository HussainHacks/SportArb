"""
Adaptive Poll Scheduler — per-sport interval management.

Instead of a flat sleep loop, each sport has its own polling tier
and last-polled timestamp. The main loop asks the scheduler which
sports are due, polls only those, and reports results back.

Tiers (in priority order):
  BURST       →  8s   Manual or auto trigger, time-limited
  LIVE        → 15s   Sport has live/in-play events
  NEAR_START  → 15s   Event within 30 min of commence_time
  PREMATCH    → 60s   Default for active sports
  BACKOFF     → 300s  Sport returned 0 events (reversible)

Design rules:
  - Tiers are separate even if intervals match today
  - Backoff is a soft state, immediately overridable by burst
  - report_fetch distinguishes empty, error, and rate-limit
  - Each sport exposes next_due_at for observability
"""

import logging
from datetime import datetime, timezone, timedelta
from enum import Enum

logger = logging.getLogger(__name__)


class PollTier(str, Enum):
    """Polling tiers, ordered from most to least aggressive."""
    BURST = "burst"
    LIVE = "live"
    NEAR_START = "near_start"
    PREMATCH = "prematch"
    BACKOFF = "backoff"


# Default intervals per tier (seconds)
DEFAULT_INTERVALS = {
    PollTier.BURST: 8,
    PollTier.LIVE: 15,
    PollTier.NEAR_START: 15,
    PollTier.PREMATCH: 60,
    PollTier.BACKOFF: 300,
}


class SportState:
    """
    Per-sport polling state.

    Tracks the current tier, timing, and metadata for scheduler
    decisions and observability.
    """

    def __init__(self, sport_key: str, intervals: dict[PollTier, int]):
        self.sport_key = sport_key
        self._intervals = intervals
        self.tier: PollTier = PollTier.PREMATCH
        self.last_polled: datetime | None = None
        self.last_event_count: int = 0
        self.has_live_events: bool = False
        self.has_near_start_events: bool = False
        self.burst_expires_at: datetime | None = None
        self.consecutive_errors: int = 0
        self.last_error: str | None = None

    @property
    def interval_seconds(self) -> int:
        """Current polling interval based on tier."""
        return self._intervals.get(self.tier, 60)

    @property
    def next_due_at(self) -> datetime:
        """When this sport is next due for a poll."""
        if self.last_polled is None:
            return datetime.min.replace(tzinfo=timezone.utc)  # Never polled → always due
        return self.last_polled + timedelta(seconds=self.interval_seconds)

    @property
    def is_due(self) -> bool:
        """Whether this sport is ready to be polled."""
        if self.last_polled is None:
            return True
        return datetime.now(timezone.utc) >= self.next_due_at

    @property
    def seconds_until_due(self) -> float:
        """Seconds until this sport is due. Negative = overdue."""
        delta = (self.next_due_at - datetime.now(timezone.utc)).total_seconds()
        return max(delta, 0.0)

    def __repr__(self) -> str:
        due_in = f"{self.seconds_until_due:.0f}s"
        return (
            f"SportState({self.sport_key} | tier={self.tier.value} | "
            f"interval={self.interval_seconds}s | due_in={due_in})"
        )


class FetchResult:
    """
    Structured result of a sport fetch, for the scheduler to react to.

    Distinguishes: success (with events), success (empty), error, rate-limit.
    """

    def __init__(
        self,
        sport_key: str,
        events: list | None = None,
        success: bool = True,
        rate_limited: bool = False,
        error_message: str = "",
        has_live_events: bool = False,
        nearest_commence_minutes: float | None = None,
    ):
        self.sport_key = sport_key
        self.events = events or []
        self.event_count = len(self.events)
        self.success = success
        self.rate_limited = rate_limited
        self.error_message = error_message
        self.has_live_events = has_live_events
        self.nearest_commence_minutes = nearest_commence_minutes


class PollScheduler:
    """
    Manages per-sport polling intervals based on market conditions.

    Usage:
        scheduler = PollScheduler(sport_keys, ...)
        due = scheduler.get_due_sports()
        for sport in due:
            result = await fetch(sport)
            scheduler.report_fetch(result)
    """

    def __init__(
        self,
        sport_keys: list[str],
        prematch_interval: int = 60,
        live_interval: int = 15,
        near_start_interval: int = 15,
        burst_interval: int = 8,
        backoff_interval: int = 300,
        burst_duration: int = 120,
        near_start_threshold_minutes: int = 30,
    ):
        self._burst_duration = burst_duration
        self._near_start_threshold = near_start_threshold_minutes

        intervals = {
            PollTier.BURST: burst_interval,
            PollTier.LIVE: live_interval,
            PollTier.NEAR_START: near_start_interval,
            PollTier.PREMATCH: prematch_interval,
            PollTier.BACKOFF: backoff_interval,
        }

        self._sports: dict[str, SportState] = {
            key: SportState(key, intervals) for key in sport_keys
        }

        logger.info(
            f"PollScheduler initialized: {len(sport_keys)} sports | "
            f"prematch={prematch_interval}s, live={live_interval}s, "
            f"burst={burst_interval}s, backoff={backoff_interval}s"
        )

    @property
    def sport_states(self) -> dict[str, SportState]:
        """All sport states for inspection."""
        return dict(self._sports)

    def get_due_sports(self) -> list[str]:
        """
        Return sport keys that are due for polling, ordered by priority.

        Priority: BURST > LIVE > NEAR_START > PREMATCH > BACKOFF
        Within the same tier, most overdue first.
        """
        now = datetime.now(timezone.utc)

        # Expire burst modes
        for state in self._sports.values():
            if (
                state.tier == PollTier.BURST
                and state.burst_expires_at
                and now >= state.burst_expires_at
            ):
                self._expire_burst(state)

        # Collect due sports
        due = [
            state for state in self._sports.values()
            if state.is_due
        ]

        # Sort by tier priority (enum order), then by how overdue
        tier_priority = {
            PollTier.BURST: 0,
            PollTier.LIVE: 1,
            PollTier.NEAR_START: 2,
            PollTier.PREMATCH: 3,
            PollTier.BACKOFF: 4,
        }
        due.sort(key=lambda s: (tier_priority[s.tier], s.next_due_at))

        return [s.sport_key for s in due]

    def report_fetch(self, result: FetchResult):
        """
        Report the outcome of a fetch so the scheduler can adjust tiers.

        Handles: success, empty, error, rate-limit.
        """
        state = self._sports.get(result.sport_key)
        if not state:
            return

        now = datetime.now(timezone.utc)
        state.last_polled = now

        # Handle failures first
        if result.rate_limited:
            # Don't change tier, just mark and skip
            state.consecutive_errors += 1
            state.last_error = "rate_limited"
            logger.warning(
                f"⚠️ {result.sport_key}: rate limited. "
                f"Keeping tier={state.tier.value}, errors={state.consecutive_errors}"
            )
            return

        if not result.success:
            state.consecutive_errors += 1
            state.last_error = result.error_message
            # Don't backoff on errors — that's for empty results only
            logger.warning(
                f"⚠️ {result.sport_key}: fetch error ({result.error_message}). "
                f"Keeping tier={state.tier.value}, errors={state.consecutive_errors}"
            )
            return

        # Success — reset error state
        state.consecutive_errors = 0
        state.last_error = None
        state.last_event_count = result.event_count

        # Don't override burst mode (it expires on its own timer)
        if state.tier == PollTier.BURST:
            return

        # Decide the new tier based on the fetch result
        old_tier = state.tier

        if result.event_count == 0:
            state.tier = PollTier.BACKOFF
            state.has_live_events = False
            state.has_near_start_events = False
        elif result.has_live_events:
            state.tier = PollTier.LIVE
            state.has_live_events = True
        elif (
            result.nearest_commence_minutes is not None
            and result.nearest_commence_minutes <= self._near_start_threshold
        ):
            state.tier = PollTier.NEAR_START
            state.has_near_start_events = True
            state.has_live_events = False
        else:
            state.tier = PollTier.PREMATCH
            state.has_live_events = False
            state.has_near_start_events = False

        if old_tier != state.tier:
            logger.info(
                f"🔄 {result.sport_key}: tier {old_tier.value} → {state.tier.value} "
                f"({result.event_count} events)"
            )

    def trigger_burst(self, sport_key: str) -> bool:
        """
        Activate burst mode for a sport. Overrides any tier including backoff.

        Returns True if burst was activated, False if sport not found.
        """
        state = self._sports.get(sport_key)
        if not state:
            return False

        now = datetime.now(timezone.utc)
        state.tier = PollTier.BURST
        state.burst_expires_at = now + timedelta(seconds=self._burst_duration)
        state.last_polled = None  # Force immediate poll

        logger.info(
            f"🚀 BURST activated: {sport_key} → {state.interval_seconds}s "
            f"for {self._burst_duration}s"
        )
        return True

    def _expire_burst(self, state: SportState):
        """Return a sport from burst to its natural tier."""
        state.burst_expires_at = None

        # Determine the right fallback tier
        if state.has_live_events:
            state.tier = PollTier.LIVE
        elif state.has_near_start_events:
            state.tier = PollTier.NEAR_START
        elif state.last_event_count == 0:
            state.tier = PollTier.BACKOFF
        else:
            state.tier = PollTier.PREMATCH

        logger.info(
            f"⏱️ Burst expired: {state.sport_key} → {state.tier.value} "
            f"({state.interval_seconds}s)"
        )

    def get_status_summary(self) -> str:
        """Human-readable summary of all sport states for logging."""
        lines = ["📋 Scheduler Status:"]
        for state in sorted(
            self._sports.values(),
            key=lambda s: s.seconds_until_due,
        ):
            due = f"due in {state.seconds_until_due:.0f}s" if not state.is_due else "DUE NOW"
            events = f"{state.last_event_count} events" if state.last_polled else "never polled"
            lines.append(
                f"  {state.sport_key:<35} {state.tier.value:<12} "
                f"{state.interval_seconds:>4}s | {due:<15} | {events}"
            )
        return "\n".join(lines)
