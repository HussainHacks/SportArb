"""
Canonical data models for normalized odds.

All odds from any vendor are transformed into these models
before entering the arbitrage engine.
"""

from datetime import datetime
from enum import Enum
from typing import Optional
from pydantic import BaseModel, Field
import uuid


class MarketStatus(str, Enum):
    ACTIVE = "active"
    SUSPENDED = "suspended"
    CLOSED = "closed"


class MarketKey(str, Enum):
    H2H = "h2h"           # Moneyline (2-way or 3-way for soccer)
    SPREADS = "spreads"    # Point spreads / handicaps
    TOTALS = "totals"      # Over/Under


class NormalizedOdds(BaseModel):
    """A single odds line from one operator for one selection in a market."""

    event_id: str = Field(..., description="Canonical event identifier")
    sport_key: str = Field(..., description="Sport identifier (e.g. basketball_nba)")
    event_name: str = Field(..., description="Human-readable event name")
    commence_time: datetime = Field(..., description="Event start time (UTC)")
    market_key: MarketKey = Field(..., description="Market type")
    period: str = Field("full_game", description="Period: full_game, 1st_half, 2nd_half, 1st_quarter, etc.")
    selection: str = Field(..., description="Selection name (team, over/under)")
    point: Optional[float] = Field(None, description="Spread/total line (e.g. -3.5, 220.5)")
    odds: float = Field(..., description="Decimal odds (e.g. 2.15)")
    operator: str = Field(..., description="Bookmaker key (e.g. draftkings)")
    operator_display: str = Field("", description="Human-readable operator name")
    timestamp: datetime = Field(default_factory=datetime.utcnow, description="When this odds line was captured")
    status: MarketStatus = Field(default=MarketStatus.ACTIVE)
    is_live: bool = Field(default=False, description="Whether this is a live/in-play market")
    is_player_prop: bool = Field(default=False, description="Whether this is a player prop market")


class ArbOpportunity(BaseModel):
    """A detected arbitrage opportunity."""

    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    event_id: str
    event_name: str
    sport_key: str
    market_key: MarketKey
    legs: list[NormalizedOdds] = Field(..., description="The odds lines forming the arb")
    arb_percent: float = Field(..., description="Sum of implied probabilities * 100 (< 100 = arb)")
    profit_percent: float = Field(..., description="Guaranteed profit % on total stake")
    stakes: dict[str, float] = Field(default_factory=dict, description="Operator → stake amount")
    total_stake: float = Field(0.0)
    guaranteed_payout: float = Field(0.0)
    detected_at: datetime = Field(default_factory=datetime.utcnow)
    is_live: bool = Field(default=True)
    market_group_key: str = Field(default="", description="Key of the exact market group that formed this arb")


class EventGroup(BaseModel):
    """All normalized odds for a single event, grouped by market+period+line."""

    event_id: str
    event_name: str
    sport_key: str
    commence_time: datetime
    odds_by_market: dict[str, list[NormalizedOdds]] = Field(
        default_factory=dict,
        description="Key = '{market_key}:{period}:{line_value}', Value = odds lines",
    )
