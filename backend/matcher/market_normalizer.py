"""
Market Normalizer — canonicalizes market types and periods.

Design principles:
  1. Controlled canonical enums — only valid market types and periods
  2. Resolution status returned (CANONICAL / UNRESOLVED)
  3. Explicit handling for unsupported market types
  4. h2h is NOT confused with 3-way match winner
  5. spread vs alternate spread distinguished by context
  6. All period variations (1h, first half, half 1, etc.) → one canonical
  7. Unknown → stays unknown, NEVER silently mapped

Failure modes defended against:
  - h2h confused with 3-way match winner
  - spread vs alternate spread vs run line vs puck line
  - game vs full_game vs match vs regulation
  - 1h, first half, half 1 not resolving consistently
  - alternate lines mixed with main lines
"""

import re
import logging
from enum import Enum
from dataclasses import dataclass

logger = logging.getLogger(__name__)


# ============================================================================
# Canonical Enums
# ============================================================================

class CanonicalMarket(str, Enum):
    """Valid canonical market types. Using str+Enum for JSON serialization."""
    MONEYLINE = "moneyline"           # 2-way (US sports)
    MONEYLINE_3WAY = "moneyline_3way"  # 3-way (soccer/draw possible)
    SPREAD = "spread"                  # Point spread / handicap / run line / puck line
    TOTAL = "total"                    # Over/under

    def __str__(self):
        return self.value


class CanonicalPeriod(str, Enum):
    """Valid canonical period types."""
    FULL_GAME = "full_game"
    FIRST_HALF = "first_half"
    SECOND_HALF = "second_half"
    FIRST_QUARTER = "first_quarter"
    SECOND_QUARTER = "second_quarter"
    THIRD_QUARTER = "third_quarter"
    FOURTH_QUARTER = "fourth_quarter"
    FIRST_PERIOD = "first_period"      # Hockey
    SECOND_PERIOD = "second_period"
    THIRD_PERIOD = "third_period"
    FIRST_5_INNINGS = "first_5_innings"  # Baseball

    def __str__(self):
        return self.value


class MarketResolutionStatus(Enum):
    """Resolution confidence of a market/period normalization."""
    CANONICAL = "canonical"
    UNRESOLVED = "unresolved"


@dataclass(frozen=True)
class MarketResolution:
    """Result of market type normalization with resolution metadata."""
    name: str
    status: MarketResolutionStatus
    is_canonical: bool
    raw_input: str = ""


@dataclass(frozen=True)
class PeriodResolution:
    """Result of period normalization with resolution metadata."""
    name: str
    status: MarketResolutionStatus
    is_canonical: bool
    raw_input: str = ""


# ============================================================================
# Market Type Mapping
# ============================================================================
# CRITICAL: h2h (2-way) and 1x2 (3-way) are DIFFERENT market types
# and must NEVER be mixed. A 2-way moneyline has no draw outcome.

MARKET_MAP: dict[str, str] = {
    # Moneyline — 2-way (no draw)
    "moneyline": "moneyline",
    "h2h": "moneyline",
    "head to head": "moneyline",
    "money line": "moneyline",
    "ml": "moneyline",
    "winner": "moneyline",
    "to win": "moneyline",
    "match winner 2way": "moneyline",
    "2way": "moneyline",
    # Moneyline — 3-way (includes draw)
    "moneyline 3way": "moneyline_3way",
    "moneyline_3way": "moneyline_3way",
    "match winner": "moneyline_3way",
    "1x2": "moneyline_3way",
    "match result": "moneyline_3way",
    "three way": "moneyline_3way",
    "3way": "moneyline_3way",
    "3 way": "moneyline_3way",
    "draw no bet": "moneyline_3way",
    # Spread / Handicap
    "spread": "spread",
    "spreads": "spread",
    "handicap": "spread",
    "point spread": "spread",
    "asian handicap": "spread",
    "run line": "spread",        # MLB-specific spread
    "puck line": "spread",       # NHL-specific spread
    "runline": "spread",
    "puckline": "spread",
    # Total / Over-Under
    "total": "total",
    "totals": "total",
    "over/under": "total",
    "over under": "total",
    "overunder": "total",
    "ou": "total",
    "game total": "total",
    "match total": "total",
}

# Set of all valid canonical market names for validation
VALID_CANONICAL_MARKETS: set[str] = {m.value for m in CanonicalMarket}


# ============================================================================
# Period Mapping
# ============================================================================

PERIOD_MAP: dict[str, str] = {
    # Full game (default)
    "full game": "full_game",
    "full_game": "full_game",
    "game": "full_game",
    "match": "full_game",
    "regulation": "full_game",
    "ft": "full_game",
    "reg": "full_game",
    "": "full_game",              # Default when not specified
    # Halves — all variations must converge
    "1st half": "first_half",
    "first half": "first_half",
    "1h": "first_half",
    "h1": "first_half",
    "first_half": "first_half",
    "half 1": "first_half",
    "1sthalf": "first_half",
    "2nd half": "second_half",
    "second half": "second_half",
    "2h": "second_half",
    "h2": "second_half",
    "second_half": "second_half",
    "half 2": "second_half",
    "2ndhalf": "second_half",
    # Quarters — all variations must converge
    "1st quarter": "first_quarter",
    "first quarter": "first_quarter",
    "1q": "first_quarter",
    "q1": "first_quarter",
    "first_quarter": "first_quarter",
    "quarter 1": "first_quarter",
    "1stquarter": "first_quarter",
    "2nd quarter": "second_quarter",
    "second quarter": "second_quarter",
    "2q": "second_quarter",
    "q2": "second_quarter",
    "second_quarter": "second_quarter",
    "quarter 2": "second_quarter",
    "2ndquarter": "second_quarter",
    "3rd quarter": "third_quarter",
    "third quarter": "third_quarter",
    "3q": "third_quarter",
    "q3": "third_quarter",
    "third_quarter": "third_quarter",
    "quarter 3": "third_quarter",
    "3rdquarter": "third_quarter",
    "4th quarter": "fourth_quarter",
    "fourth quarter": "fourth_quarter",
    "4q": "fourth_quarter",
    "q4": "fourth_quarter",
    "fourth_quarter": "fourth_quarter",
    "quarter 4": "fourth_quarter",
    "4thquarter": "fourth_quarter",
    # Periods (hockey) — all variations must converge
    "1st period": "first_period",
    "first period": "first_period",
    "1p": "first_period",
    "p1": "first_period",
    "first_period": "first_period",
    "period 1": "first_period",
    "2nd period": "second_period",
    "second period": "second_period",
    "2p": "second_period",
    "p2": "second_period",
    "second_period": "second_period",
    "period 2": "second_period",
    "3rd period": "third_period",
    "third period": "third_period",
    "3p": "third_period",
    "p3": "third_period",
    "third_period": "third_period",
    "period 3": "third_period",
    # Innings (baseball)
    "first 5 innings": "first_5_innings",
    "f5": "first_5_innings",
    "first_5_innings": "first_5_innings",
    "f5i": "first_5_innings",
    "1st 5 innings": "first_5_innings",
}

# Set of all valid canonical period names
VALID_CANONICAL_PERIODS: set[str] = {p.value for p in CanonicalPeriod}


# ============================================================================
# Public API
# ============================================================================

def _clean_market_text(raw: str) -> str:
    """Clean market/period text for lookup."""
    cleaned = raw.lower().strip()
    cleaned = re.sub(r"[_\-/]", " ", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned)
    return cleaned


def resolve_market(raw_market: str) -> MarketResolution:
    """
    Normalize a market type and return full resolution metadata.

    This is the trusted API. Use this instead of normalize_market()
    in matching, scoring, and arb-critical code paths.
    """
    cleaned = _clean_market_text(raw_market)

    if cleaned in MARKET_MAP:
        canonical = MARKET_MAP[cleaned]
        return MarketResolution(
            name=canonical,
            status=MarketResolutionStatus.CANONICAL,
            is_canonical=True,
            raw_input=raw_market,
        )

    logger.debug(f"Unresolved market: '{raw_market}' (cleaned: '{cleaned}')")
    return MarketResolution(
        name=cleaned,
        status=MarketResolutionStatus.UNRESOLVED,
        is_canonical=False,
        raw_input=raw_market,
    )


def resolve_period(raw_period: str) -> PeriodResolution:
    """
    Normalize a period name and return full resolution metadata.

    This is the trusted API. Use this instead of normalize_period()
    in matching, scoring, and arb-critical code paths.
    """
    cleaned = raw_period.lower().strip()

    if cleaned in PERIOD_MAP:
        canonical = PERIOD_MAP[cleaned]
        return PeriodResolution(
            name=canonical,
            status=MarketResolutionStatus.CANONICAL,
            is_canonical=True,
            raw_input=raw_period,
        )

    logger.debug(f"Unresolved period: '{raw_period}' (cleaned: '{cleaned}')")
    return PeriodResolution(
        name=cleaned if cleaned else "full_game",
        status=MarketResolutionStatus.UNRESOLVED if cleaned else MarketResolutionStatus.CANONICAL,
        is_canonical=not bool(cleaned),
        raw_input=raw_period,
    )


def normalize_market(raw_market: str) -> str:
    """
    Normalize a market type to its canonical form.

    ⚠️ CONVENIENCE ONLY — returns a plain string with no resolution metadata.
    Do NOT use in matching, scoring, or arb-critical code paths.
    Use resolve_market() instead, which returns MarketResolution with
    full status (CANONICAL / UNRESOLVED).
    """
    return resolve_market(raw_market).name


def normalize_period(raw_period: str) -> str:
    """
    Normalize a period name to its canonical form.

    ⚠️ CONVENIENCE ONLY — returns a plain string with no resolution metadata.
    Do NOT use in matching, scoring, or arb-critical code paths.
    Use resolve_period() instead, which returns PeriodResolution with
    full status (CANONICAL / UNRESOLVED).
    """
    return resolve_period(raw_period).name


def is_valid_canonical_market(name: str) -> bool:
    """Check if a string is a valid canonical market name."""
    return name in VALID_CANONICAL_MARKETS


def is_valid_canonical_period(name: str) -> bool:
    """Check if a string is a valid canonical period name."""
    return name in VALID_CANONICAL_PERIODS
