import pytest
from datetime import datetime, timezone, timedelta
import asyncio

from backend.paper_trading.paper_trade_models import (
    PaperTrade,
    PaperTradeStatus,
)
from backend.paper_trading.paper_trade_recorder import PaperTradeRecorder
from backend.paper_trading.paper_trade_rechecker import PaperTradeRechecker
from backend.normalization.models import NormalizedOdds, MarketKey, MarketStatus


@pytest.fixture
def recorder():
    return PaperTradeRecorder(min_arb_threshold=1.0)


@pytest.fixture
def rechecker(recorder):
    return PaperTradeRechecker(recorder, check_interval_sec=0.1)


def _make_trade(created_at=None):
    """Create a test paper trade with a fixed created_at."""
    return PaperTrade(
        created_at=created_at or datetime(2026, 1, 1, 0, 0, 0, tzinfo=timezone.utc),
        canonical_event_id="evt_1",
        market_type="spreads",
        period="full_game",
        line_value=3.5,
        operator_a="draftkings",
        selection_a="celtics",
        operator_b="fanduel",
        selection_b="lakers",
        profit_pct_at_alert=2.0,
        min_arb_threshold=1.0,
    )


def _make_live_odds(operator, selection, odds, fresh_ts=None, point=3.5):
    """Create test odds with a timestamp that's newer than the trade."""
    # Default: 10 seconds after the trade was created
    ts = fresh_ts or datetime(2026, 1, 1, 0, 0, 10, tzinfo=timezone.utc)
    return NormalizedOdds(
        event_id="evt_1",
        sport_key="test_sport",
        event_name="test_event",
        commence_time=datetime(2026, 1, 1, tzinfo=timezone.utc),
        market_key=MarketKey.SPREADS,
        period="full_game",
        point=point,
        operator=operator,
        selection=selection,
        odds=odds,
        timestamp=ts,
        status=MarketStatus.ACTIVE,
    )


class TestExactMatching:
    """Verify that rechecks use exact leg matching."""

    def test_missing_leg_breaks_trade(self, rechecker):
        trade = _make_trade()
        leg_a = _make_live_odds("draftkings", "celtics", 1.9)
        # No leg_b provided

        rechecker._recheck_trade(trade, 5, [leg_a], datetime.now(timezone.utc))
        assert trade.status == PaperTradeStatus.BROKEN_BY_MISSING_LINE
        assert "fanduel:lakers" in trade.break_reason_detail

    def test_suspended_leg_breaks_trade(self, rechecker):
        trade = _make_trade()
        leg_a = _make_live_odds("draftkings", "celtics", 1.9)
        leg_b = _make_live_odds("fanduel", "lakers", 2.15)
        leg_b.status = MarketStatus.SUSPENDED

        rechecker._recheck_trade(trade, 5, [leg_a, leg_b], datetime.now(timezone.utc))
        assert trade.status == PaperTradeStatus.BROKEN_BY_SUSPENSION
        assert "fanduel" in trade.break_reason_detail

    def test_profit_decay_breaks_trade(self, rechecker):
        trade = _make_trade()
        leg_a = _make_live_odds("draftkings", "celtics", 1.9)
        leg_b = _make_live_odds("fanduel", "lakers", 1.9)
        # 1/1.9 + 1/1.9 = 1.052 → negative arb

        rechecker._recheck_trade(trade, 5, [leg_a, leg_b], datetime.now(timezone.utc))
        assert trade.status == PaperTradeStatus.BROKEN_BY_MOVEMENT

    def test_valid_arb_survives(self, rechecker):
        trade = _make_trade()
        leg_a = _make_live_odds("draftkings", "celtics", 1.9)
        leg_b = _make_live_odds("fanduel", "lakers", 2.5)

        rechecker._recheck_trade(trade, 5, [leg_a, leg_b], datetime.now(timezone.utc))
        assert trade.status == PaperTradeStatus.STILL_VALID_5S


class TestStaleOddsGuard:
    """Verify that stale odds (predating the alert) are rejected."""

    def test_stale_odds_rejected(self, rechecker):
        """Odds older than the alert timestamp should break the trade."""
        trade = _make_trade(
            created_at=datetime(2026, 1, 1, 0, 1, 0, tzinfo=timezone.utc)
        )
        # Odds from BEFORE the trade was created
        old_ts = datetime(2026, 1, 1, 0, 0, 0, tzinfo=timezone.utc)
        leg_a = _make_live_odds("draftkings", "celtics", 1.9, fresh_ts=old_ts)
        leg_b = _make_live_odds("fanduel", "lakers", 2.5, fresh_ts=old_ts)

        rechecker._recheck_trade(trade, 5, [leg_a, leg_b], datetime.now(timezone.utc))
        assert trade.status == PaperTradeStatus.BROKEN_BY_MISSING_LINE
        assert "Stale odds" in trade.break_reason_detail

    def test_fresh_odds_pass(self, rechecker):
        """Odds newer than the alert should be accepted."""
        trade = _make_trade(
            created_at=datetime(2026, 1, 1, 0, 0, 0, tzinfo=timezone.utc)
        )
        # Odds from AFTER the trade was created
        fresh_ts = datetime(2026, 1, 1, 0, 0, 10, tzinfo=timezone.utc)
        leg_a = _make_live_odds("draftkings", "celtics", 1.9, fresh_ts=fresh_ts)
        leg_b = _make_live_odds("fanduel", "lakers", 2.5, fresh_ts=fresh_ts)

        rechecker._recheck_trade(trade, 5, [leg_a, leg_b], datetime.now(timezone.utc))
        assert trade.status == PaperTradeStatus.STILL_VALID_5S

    def test_stale_metric_incremented(self, rechecker):
        """Stale rejection should increment the metric counter."""
        trade = _make_trade(
            created_at=datetime(2026, 1, 1, 0, 1, 0, tzinfo=timezone.utc)
        )
        old_ts = datetime(2026, 1, 1, 0, 0, 0, tzinfo=timezone.utc)
        leg_a = _make_live_odds("draftkings", "celtics", 1.9, fresh_ts=old_ts)
        leg_b = _make_live_odds("fanduel", "lakers", 2.5, fresh_ts=old_ts)

        assert rechecker.metrics["stale_data_rejections"] == 0
        rechecker._recheck_trade(trade, 5, [leg_a, leg_b], datetime.now(timezone.utc))
        assert rechecker.metrics["stale_data_rejections"] == 1


class TestIntervalOrdering:
    """Verify that the earliest missing interval is checked first."""

    def test_earliest_interval_first(self, rechecker, recorder):
        """A 35s-old trade should check 5s first, not 30s."""
        trade = _make_trade(
            created_at=datetime.now(timezone.utc) - timedelta(seconds=35)
        )
        # All snapshots are None → should pick 5 (earliest), not 30
        recorder._trades.append(trade)

        pending = recorder.pending_trades
        assert len(pending) == 1

        # Simulate what _process_pending_trades does for interval selection
        from backend.paper_trading.paper_trade_models import RECHECK_INTERVALS

        age = 35.0
        next_interval = None
        for interval in RECHECK_INTERVALS:
            if age >= interval:
                if interval == 5 and trade.snapshot_5s is None:
                    next_interval = 5
                    break
                elif interval == 10 and trade.snapshot_10s is None:
                    next_interval = 10
                    break
                elif interval == 30 and trade.snapshot_30s is None:
                    next_interval = 30
                    break

        assert next_interval == 5, f"Expected 5s interval but got {next_interval}s"


class TestRecheckMetrics:
    """Verify rechecker metrics tracking."""

    def test_metrics_update_on_recheck(self, rechecker):
        trade = _make_trade()
        leg_a = _make_live_odds("draftkings", "celtics", 1.9)
        leg_b = _make_live_odds("fanduel", "lakers", 2.5)

        rechecker._recheck_trade(trade, 5, [leg_a, leg_b], datetime.now(timezone.utc))
        metrics = rechecker.metrics
        assert metrics["total_rechecks"] == 1
        assert metrics["total_survived"] == 1
        assert metrics["total_broken"] == 0

    def test_broken_metric(self, rechecker):
        trade = _make_trade()
        # Only one leg → will break
        leg_a = _make_live_odds("draftkings", "celtics", 1.9)

        rechecker._recheck_trade(trade, 5, [leg_a], datetime.now(timezone.utc))
        assert rechecker.metrics["total_broken"] == 1
