"""
Tests for the adaptive poll scheduler.

Edge cases tested:
  - Tier transitions (prematch → live → backoff → burst)
  - Burst overrides backoff immediately
  - Burst expires to natural tier
  - Failed fetch does not cause backoff
  - Empty fetch causes backoff
  - Multiple sports due: priority ordering
  - Near-start detection
"""

import pytest
from datetime import datetime, timezone, timedelta
from unittest.mock import patch

from backend.engine.poll_scheduler import (
    PollScheduler,
    PollTier,
    FetchResult,
    SportState,
)


@pytest.fixture
def scheduler():
    """Scheduler with 3 test sports and short intervals for testing."""
    return PollScheduler(
        sport_keys=["basketball_nba", "icehockey_nhl", "soccer_epl"],
        prematch_interval=60,
        live_interval=15,
        near_start_interval=15,
        burst_interval=8,
        backoff_interval=300,
        burst_duration=120,
        near_start_threshold_minutes=30,
    )


class TestInitialization:
    def test_all_sports_start_prematch(self, scheduler):
        states = scheduler.sport_states
        assert len(states) == 3
        for state in states.values():
            assert state.tier == PollTier.PREMATCH

    def test_all_sports_due_immediately(self, scheduler):
        """Sports that have never been polled should be due now."""
        due = scheduler.get_due_sports()
        assert len(due) == 3

    def test_intervals_assigned_correctly(self, scheduler):
        state = scheduler.sport_states["basketball_nba"]
        assert state.interval_seconds == 60  # prematch default


class TestTierTransitions:
    def test_empty_fetch_triggers_backoff(self, scheduler):
        scheduler.report_fetch(FetchResult(
            sport_key="basketball_nba",
            events=[],
            success=True,
        ))
        assert scheduler.sport_states["basketball_nba"].tier == PollTier.BACKOFF

    def test_live_events_trigger_live_tier(self, scheduler):
        scheduler.report_fetch(FetchResult(
            sport_key="basketball_nba",
            events=[{"id": "1"}],
            success=True,
            has_live_events=True,
        ))
        assert scheduler.sport_states["basketball_nba"].tier == PollTier.LIVE
        assert scheduler.sport_states["basketball_nba"].interval_seconds == 15

    def test_near_start_triggers_near_start_tier(self, scheduler):
        scheduler.report_fetch(FetchResult(
            sport_key="basketball_nba",
            events=[{"id": "1"}],
            success=True,
            has_live_events=False,
            nearest_commence_minutes=20,  # within 30 min threshold
        ))
        assert scheduler.sport_states["basketball_nba"].tier == PollTier.NEAR_START

    def test_normal_events_stay_prematch(self, scheduler):
        scheduler.report_fetch(FetchResult(
            sport_key="basketball_nba",
            events=[{"id": "1"}],
            success=True,
            has_live_events=False,
            nearest_commence_minutes=120,  # 2 hours away
        ))
        assert scheduler.sport_states["basketball_nba"].tier == PollTier.PREMATCH

    def test_live_overrides_near_start(self, scheduler):
        """If events are both live and near-start, LIVE wins."""
        scheduler.report_fetch(FetchResult(
            sport_key="basketball_nba",
            events=[{"id": "1"}],
            success=True,
            has_live_events=True,
            nearest_commence_minutes=10,
        ))
        assert scheduler.sport_states["basketball_nba"].tier == PollTier.LIVE


class TestBurstMode:
    def test_burst_overrides_backoff(self, scheduler):
        """Burst should immediately override backoff."""
        # Put into backoff
        scheduler.report_fetch(FetchResult(
            sport_key="basketball_nba", events=[], success=True,
        ))
        assert scheduler.sport_states["basketball_nba"].tier == PollTier.BACKOFF

        # Trigger burst
        result = scheduler.trigger_burst("basketball_nba")
        assert result is True
        assert scheduler.sport_states["basketball_nba"].tier == PollTier.BURST
        assert scheduler.sport_states["basketball_nba"].interval_seconds == 8

    def test_burst_forces_immediate_poll(self, scheduler):
        """After burst, sport should be due immediately."""
        # Poll once to set last_polled
        scheduler.report_fetch(FetchResult(
            sport_key="basketball_nba", events=[{"id": "1"}], success=True,
        ))

        scheduler.trigger_burst("basketball_nba")
        # last_polled is set to None, so it should be due now
        assert scheduler.sport_states["basketball_nba"].is_due

    def test_burst_does_not_change_on_report(self, scheduler):
        """Burst should not be overridden by report_fetch (it expires on timer)."""
        scheduler.trigger_burst("basketball_nba")
        scheduler.report_fetch(FetchResult(
            sport_key="basketball_nba", events=[{"id": "1"}], success=True,
        ))
        assert scheduler.sport_states["basketball_nba"].tier == PollTier.BURST

    def test_burst_expires_to_natural_tier(self, scheduler):
        """After burst duration, sport returns to its natural tier."""
        scheduler.trigger_burst("basketball_nba")
        state = scheduler.sport_states["basketball_nba"]

        # Simulate burst expiration
        state.burst_expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
        state.has_live_events = True  # came from live

        # get_due_sports triggers burst expiration check
        scheduler.get_due_sports()
        assert state.tier == PollTier.LIVE  # falls back to natural tier

    def test_burst_unknown_sport_returns_false(self, scheduler):
        assert scheduler.trigger_burst("unknown_sport") is False


class TestErrorHandling:
    def test_failed_fetch_does_not_cause_backoff(self, scheduler):
        """Errors should NOT cause backoff — only empty results should."""
        scheduler.report_fetch(FetchResult(
            sport_key="basketball_nba",
            success=False,
            error_message="Timeout",
        ))
        # Should still be prematch, not backoff
        assert scheduler.sport_states["basketball_nba"].tier == PollTier.PREMATCH
        assert scheduler.sport_states["basketball_nba"].consecutive_errors == 1

    def test_rate_limit_does_not_change_tier(self, scheduler):
        # First put into live tier
        scheduler.report_fetch(FetchResult(
            sport_key="basketball_nba",
            events=[{"id": "1"}],
            success=True,
            has_live_events=True,
        ))
        assert scheduler.sport_states["basketball_nba"].tier == PollTier.LIVE

        # Rate limit should preserve the current tier
        scheduler.report_fetch(FetchResult(
            sport_key="basketball_nba",
            rate_limited=True,
        ))
        assert scheduler.sport_states["basketball_nba"].tier == PollTier.LIVE

    def test_errors_reset_on_success(self, scheduler):
        scheduler.report_fetch(FetchResult(
            sport_key="basketball_nba", success=False, error_message="err",
        ))
        assert scheduler.sport_states["basketball_nba"].consecutive_errors == 1

        scheduler.report_fetch(FetchResult(
            sport_key="basketball_nba",
            events=[{"id": "1"}],
            success=True,
        ))
        assert scheduler.sport_states["basketball_nba"].consecutive_errors == 0


class TestDueSportOrdering:
    def test_burst_polled_before_prematch(self, scheduler):
        """Burst sports should be polled before prematch sports."""
        # Make all sports due
        scheduler.trigger_burst("basketball_nba")

        due = scheduler.get_due_sports()
        # NBA should be first (burst tier)
        assert due[0] == "basketball_nba"

    def test_live_polled_before_backoff(self, scheduler):
        """Live sports should be polled before backed-off sports."""
        # Put NBA in live
        scheduler.report_fetch(FetchResult(
            sport_key="basketball_nba",
            events=[{"id": "1"}],
            success=True,
            has_live_events=True,
        ))
        # Put NHL in backoff
        scheduler.report_fetch(FetchResult(
            sport_key="icehockey_nhl", events=[], success=True,
        ))

        # Reset last_polled so both are due
        scheduler.sport_states["basketball_nba"].last_polled = None
        scheduler.sport_states["icehockey_nhl"].last_polled = None

        due = scheduler.get_due_sports()
        nba_idx = due.index("basketball_nba")
        nhl_idx = due.index("icehockey_nhl")
        assert nba_idx < nhl_idx


class TestStatusSummary:
    def test_summary_contains_all_sports(self, scheduler):
        summary = scheduler.get_status_summary()
        assert "basketball_nba" in summary
        assert "icehockey_nhl" in summary
        assert "soccer_epl" in summary
        assert "prematch" in summary


class TestNextDueAt:
    def test_never_polled_is_due_now(self, scheduler):
        state = scheduler.sport_states["basketball_nba"]
        assert state.is_due

    def test_just_polled_not_due(self, scheduler):
        scheduler.report_fetch(FetchResult(
            sport_key="basketball_nba",
            events=[{"id": "1"}],
            success=True,
        ))
        state = scheduler.sport_states["basketball_nba"]
        assert not state.is_due
        assert state.seconds_until_due > 0
