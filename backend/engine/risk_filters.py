"""
Risk Filters — validates arb opportunities before alerting.

Filters:
  1. Stale odds (timestamp too old)
  2. Palpable errors (unrealistically high profit %)
  3. Mismatched spreads/totals
  4. Iowa college player prop blacklist
  5. Suspended/closed markets
"""

import logging
from datetime import datetime, timezone

from backend.config import settings
from backend.normalization.models import ArbOpportunity, MarketStatus

logger = logging.getLogger(__name__)


class RiskFilterResult:
    """Result of applying risk filters to an arb opportunity."""

    def __init__(self, passed: bool, reason: str = ""):
        self.passed = passed
        self.reason = reason

    def __bool__(self):
        return self.passed


def filter_stale_odds(arb: ArbOpportunity) -> RiskFilterResult:
    """
    Reject if any leg's odds are older than the threshold.

    Thresholds per spec:
      - prematch: stale_odds_seconds (default 60s)
      - live:     10 seconds
    """
    now = datetime.now(timezone.utc)
    prematch_max_age = settings.stale_odds_seconds
    live_max_age = 10  # Live markets move fast

    for leg in arb.legs:
        ts = leg.timestamp
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)

        age = (now - ts).total_seconds()
        max_age = live_max_age if leg.is_live else prematch_max_age

        if age > max_age:
            market_type = "live" if leg.is_live else "prematch"
            return RiskFilterResult(
                False,
                f"Stale odds ({market_type}): {leg.operator_display} odds are {age:.0f}s old (max {max_age}s)",
            )

    return RiskFilterResult(True)


def filter_palpable_error(arb: ArbOpportunity) -> RiskFilterResult:
    """
    Reject arbs with unrealistically high profit (likely a bookmaker error
    that will be voided — "palp").
    """
    if arb.profit_percent > settings.max_arb_percent:
        return RiskFilterResult(
            False,
            f"Suspected palpable error: {arb.profit_percent:.2f}% profit exceeds {settings.max_arb_percent}%",
        )
    return RiskFilterResult(True)


def filter_minimum_profit(arb: ArbOpportunity) -> RiskFilterResult:
    """
    Reject arbs below the minimum profit threshold (not worth the effort).
    """
    if arb.profit_percent < settings.min_arb_percent:
        return RiskFilterResult(
            False,
            f"Below minimum: {arb.profit_percent:.2f}% < {settings.min_arb_percent}%",
        )
    return RiskFilterResult(True)


def filter_suspended_markets(arb: ArbOpportunity) -> RiskFilterResult:
    """
    Reject if any leg has a suspended or closed market.
    """
    for leg in arb.legs:
        if leg.status != MarketStatus.ACTIVE:
            return RiskFilterResult(
                False,
                f"Market not active: {leg.operator_display} status={leg.status.value}",
            )
    return RiskFilterResult(True)


def filter_iowa_college_props(arb: ArbOpportunity) -> RiskFilterResult:
    """
    Iowa regulation: player prop bets on individual athletes from
    Iowa colleges are prohibited.

    This filter checks if any leg involves a player prop for an
    Iowa college team.
    """
    for leg in arb.legs:
        if not leg.is_player_prop:
            continue

        event_lower = leg.event_name.lower()
        for team in settings.iowa_college_blacklist:
            if team in event_lower:
                return RiskFilterResult(
                    False,
                    f"Iowa regulation: player prop involves blacklisted team '{team}'",
                )

    return RiskFilterResult(True)


def filter_mismatched_lines(arb: ArbOpportunity) -> RiskFilterResult:
    """
    For spreads/totals: ensure all legs reference compatible point values.
    e.g., don't arb Team A +3.5 (Book1) vs Team B -4.0 (Book2)
    """
    if arb.market_key.value not in ("spreads", "totals"):
        return RiskFilterResult(True)

    points = set()
    for leg in arb.legs:
        if leg.point is not None:
            points.add(abs(leg.point))

    # For valid arbs, all absolute point values should be the same
    if len(points) > 1:
        return RiskFilterResult(
            False,
            f"Mismatched lines: point values {points} are not consistent",
        )

    return RiskFilterResult(True)


# --- Freshness-gap thresholds ---
# These target the #1 cause of phantom arbs: stale feed mismatches.
# Aligned with the 35s poll interval so legitimate arbs aren't
# rejected simply because the poll cycle took its full duration.
_PREMATCH_MAX_LEG_AGE = 45      # seconds (> 35s poll interval)
_PREMATCH_MAX_CROSS_GAP = 15    # seconds
_LIVE_MAX_LEG_AGE = 12          # seconds
_LIVE_MAX_CROSS_GAP = 5         # seconds


def filter_freshness_gap(arb: ArbOpportunity) -> RiskFilterResult:
    """
    Reject arbs where legs come from feeds updated at very different times.

    Two checks:
      1. Absolute age: reject if any leg is too old
      2. Cross-book gap: reject if two legs' timestamps are too far apart

    This catches phantom arbs from slow bookmaker feeds (e.g., one book
    shows a stale price while another has already moved).
    """
    now = datetime.now(timezone.utc)
    legs = arb.legs

    if len(legs) < 2:
        return RiskFilterResult(True)

    # Determine thresholds based on whether any leg is live
    any_live = any(getattr(leg, 'is_live', False) for leg in legs)
    max_age = _LIVE_MAX_LEG_AGE if any_live else _PREMATCH_MAX_LEG_AGE
    max_gap = _LIVE_MAX_CROSS_GAP if any_live else _PREMATCH_MAX_CROSS_GAP

    # Check 1: absolute leg age
    timestamps = []
    for leg in legs:
        ts = leg.timestamp
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        timestamps.append(ts)

        age = (now - ts).total_seconds()
        if age > max_age:
            mode = "live" if any_live else "prematch"
            return RiskFilterResult(
                False,
                f"Freshness: {leg.operator} leg is {age:.0f}s old "
                f"(max {max_age}s {mode})",
            )

    # Check 2: cross-book timestamp gap
    for i in range(len(timestamps)):
        for j in range(i + 1, len(timestamps)):
            gap = abs((timestamps[i] - timestamps[j]).total_seconds())
            if gap > max_gap:
                mode = "live" if any_live else "prematch"
                return RiskFilterResult(
                    False,
                    f"Freshness: cross-book gap {gap:.0f}s between "
                    f"{legs[i].operator} and {legs[j].operator} "
                    f"(max {max_gap}s {mode})",
                )

    return RiskFilterResult(True)


# All filters in order of priority (cheapest checks first)
ALL_FILTERS = [
    ("suspended_markets", filter_suspended_markets),
    ("minimum_profit", filter_minimum_profit),
    ("palpable_error", filter_palpable_error),
    ("stale_odds", filter_stale_odds),
    ("freshness_gap", filter_freshness_gap),
    ("mismatched_lines", filter_mismatched_lines),
    ("iowa_college_props", filter_iowa_college_props),
]


def apply_all_filters(
    arbs: list[ArbOpportunity],
) -> tuple[list[ArbOpportunity], dict[str, int]]:
    """
    Apply all risk filters to a list of arb opportunities.

    Returns a tuple of:
      - list of arbs that pass ALL filters
      - dict of per-filter rejection counts (for diagnostics)
    """
    passed = []
    rejection_counts: dict[str, int] = {name: 0 for name, _ in ALL_FILTERS}

    for arb in arbs:
        is_valid = True

        for filter_name, filter_fn in ALL_FILTERS:
            result = filter_fn(arb)
            if not result:
                rejection_counts[filter_name] += 1
                logger.debug(
                    f"Filtered out arb {arb.event_name} ({arb.market_key.value}): "
                    f"[{filter_name}] {result.reason}"
                )
                is_valid = False
                break

        if is_valid:
            passed.append(arb)

    # Build concise rejection summary
    rejected_parts = [
        f"{name}={count}" for name, count in rejection_counts.items() if count > 0
    ]
    rejected_summary = ", ".join(rejected_parts) if rejected_parts else "none"
    logger.info(
        f"Risk filters: {len(passed)}/{len(arbs)} passed | rejected by: {rejected_summary}"
    )
    return passed, rejection_counts
