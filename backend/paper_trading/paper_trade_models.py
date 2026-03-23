"""
Paper Trade Models — data structures for arb survival measurement.

A PaperTrade freezes the exact state of an arb alert and tracks
whether it survives at 5s, 10s, and 30s re-checks.

Design rules:
  - Tied to the EXACT market group key that triggered the alert
  - Frozen ArbOpportunity includes full raw detail for reconstruction
  - Break reasons have both enum (machine) and string (human) forms
  - min_arb_threshold stored for interpretability if thresholds change
"""

from datetime import datetime, timezone
from enum import Enum
from dataclasses import dataclass, field
from typing import Any
import uuid


class PaperTradeStatus(str, Enum):
    """
    Lifecycle status of a paper trade.

    Terminal states: STILL_VALID_30S and all BROKEN_* states.
    """
    ALERTED = "alerted"
    STILL_VALID_5S = "still_valid_5s"
    STILL_VALID_10S = "still_valid_10s"
    STILL_VALID_30S = "still_valid_30s"
    BROKEN_BY_MOVEMENT = "broken_by_movement"
    BROKEN_BY_MISSING_LINE = "broken_by_missing_line"
    BROKEN_BY_SUSPENSION = "broken_by_suspension"

    @property
    def is_terminal(self) -> bool:
        return self in (
            PaperTradeStatus.STILL_VALID_30S,
            PaperTradeStatus.BROKEN_BY_MOVEMENT,
            PaperTradeStatus.BROKEN_BY_MISSING_LINE,
            PaperTradeStatus.BROKEN_BY_SUSPENSION,
        )

    @property
    def is_alive(self) -> bool:
        return self in (
            PaperTradeStatus.ALERTED,
            PaperTradeStatus.STILL_VALID_5S,
            PaperTradeStatus.STILL_VALID_10S,
            PaperTradeStatus.STILL_VALID_30S,
        )


# Re-check intervals in seconds
RECHECK_INTERVALS = [5, 10, 30]

# Map interval → status if still valid
INTERVAL_TO_STATUS = {
    5: PaperTradeStatus.STILL_VALID_5S,
    10: PaperTradeStatus.STILL_VALID_10S,
    30: PaperTradeStatus.STILL_VALID_30S,
}


@dataclass
class OddsSnapshot:
    """
    Lightweight snapshot of odds at a re-check point.

    Captures the exact state of both legs at a specific time.
    """
    timestamp: datetime
    interval_seconds: int
    odds_a: float | None = None
    odds_b: float | None = None
    profit_pct: float | None = None
    both_legs_found: bool = False
    any_suspended: bool = False
    # For 3-way markets
    odds_c: float | None = None


@dataclass
class PaperTrade:
    """
    A frozen arb alert with survival tracking.

    Created when an arb alert fires. Updated at 5s, 10s, 30s
    with re-check snapshots.

    IMPORTANT: market_group_key ties this to the EXACT market
    group that triggered the alert, not just the teams.
    """
    # Identity
    alert_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    status: PaperTradeStatus = PaperTradeStatus.ALERTED

    # Event context
    canonical_event_id: str = ""
    event_name: str = ""
    sport_key: str = ""

    # Market context — exact group identification
    market_type: str = ""
    period: str = ""
    line_value: float | None = None
    market_group_key: str = ""

    # Leg A
    operator_a: str = ""
    selection_a: str = ""
    odds_a_at_alert: float = 0.0

    # Leg B
    operator_b: str = ""
    selection_b: str = ""
    odds_b_at_alert: float = 0.0

    # For 3-way markets
    operator_c: str = ""
    selection_c: str = ""
    odds_c_at_alert: float = 0.0

    # Alert-time calculations
    profit_pct_at_alert: float = 0.0
    stake_plan: dict[str, float] = field(default_factory=dict)
    min_arb_threshold: float = 0.5  # Threshold used at alert time

    # Re-check snapshots
    snapshot_5s: OddsSnapshot | None = None
    snapshot_10s: OddsSnapshot | None = None
    snapshot_30s: OddsSnapshot | None = None

    # Break info — both machine and human forms
    break_reason: PaperTradeStatus | None = None
    break_reason_detail: str = ""
    break_at_interval: int | None = None

    # Frozen alert state — full reconstruction data
    frozen_arb: dict[str, Any] = field(default_factory=dict)

    @property
    def survived_5s(self) -> bool:
        if self.status in (
            PaperTradeStatus.STILL_VALID_5S,
            PaperTradeStatus.STILL_VALID_10S,
            PaperTradeStatus.STILL_VALID_30S,
        ):
            return True
        return self.break_at_interval is not None and self.break_at_interval > 5

    @property
    def survived_10s(self) -> bool:
        if self.status in (
            PaperTradeStatus.STILL_VALID_10S,
            PaperTradeStatus.STILL_VALID_30S,
        ):
            return True
        return self.break_at_interval is not None and self.break_at_interval > 10

    @property
    def survived_30s(self) -> bool:
        return self.status == PaperTradeStatus.STILL_VALID_30S

    @property
    def is_terminal(self) -> bool:
        return self.status.is_terminal

    @property
    def operator_pair(self) -> str:
        """Sorted operator pair for per-pair survival analysis."""
        return "|".join(sorted([self.operator_a, self.operator_b]))

    @property
    def profit_decay(self) -> dict[str, float | None]:
        """Profit at each checkpoint for decay analysis."""
        return {
            "alert": self.profit_pct_at_alert,
            "5s": self.snapshot_5s.profit_pct if self.snapshot_5s else None,
            "10s": self.snapshot_10s.profit_pct if self.snapshot_10s else None,
            "30s": self.snapshot_30s.profit_pct if self.snapshot_30s else None,
        }

    def record_snapshot(self, snapshot: OddsSnapshot):
        """Record a re-check snapshot at the appropriate interval."""
        if snapshot.interval_seconds == 5:
            self.snapshot_5s = snapshot
        elif snapshot.interval_seconds == 10:
            self.snapshot_10s = snapshot
        elif snapshot.interval_seconds == 30:
            self.snapshot_30s = snapshot

    def mark_broken(
        self,
        reason: PaperTradeStatus,
        detail: str,
        interval: int,
    ):
        """Mark this paper trade as broken with full context."""
        self.status = reason
        self.break_reason = reason
        self.break_reason_detail = detail
        self.break_at_interval = interval

    def mark_valid(self, interval: int):
        """Mark this paper trade as surviving at the given interval."""
        if interval in INTERVAL_TO_STATUS:
            self.status = INTERVAL_TO_STATUS[interval]

    def __repr__(self):
        return (
            f"PaperTrade({self.status.value} | "
            f"{self.operator_a}:{self.selection_a} @ {self.odds_a_at_alert} vs "
            f"{self.operator_b}:{self.selection_b} @ {self.odds_b_at_alert} | "
            f"profit: {self.profit_pct_at_alert:.2f}%)"
        )
