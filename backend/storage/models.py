"""
SQLAlchemy ORM models for persistent storage.
"""

from datetime import datetime
from sqlalchemy import Column, String, Float, DateTime, Boolean, JSON, Text
from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    pass


class ArbRecord(Base):
    """Persisted arbitrage opportunity for historical analysis."""

    __tablename__ = "arb_opportunities"

    id = Column(String, primary_key=True)
    event_id = Column(String, nullable=False, index=True)
    event_name = Column(Text, nullable=False)
    sport_key = Column(String, nullable=False, index=True)
    market_key = Column(String, nullable=False)
    arb_percent = Column(Float, nullable=False)
    profit_percent = Column(Float, nullable=False)
    total_stake = Column(Float, nullable=False)
    guaranteed_payout = Column(Float, nullable=False)
    legs = Column(JSON, nullable=False)     # Serialized list of odds legs
    stakes = Column(JSON, nullable=False)   # Operator → stake mapping
    detected_at = Column(DateTime, default=datetime.utcnow, index=True)
    alerted = Column(Boolean, default=False)
    is_live = Column(Boolean, default=True)


class OddsSnapshot(Base):
    """Point-in-time snapshot of an odds line for replay testing."""

    __tablename__ = "odds_snapshots"

    id = Column(String, primary_key=True)
    event_id = Column(String, nullable=False, index=True)
    sport_key = Column(String, nullable=False)
    market_key = Column(String, nullable=False)
    selection = Column(String, nullable=False)
    point = Column(Float, nullable=True)
    odds = Column(Float, nullable=False)
    operator = Column(String, nullable=False, index=True)
    timestamp = Column(DateTime, nullable=False, index=True)
    raw_json = Column(JSON, nullable=True)
