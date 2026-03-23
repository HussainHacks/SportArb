"""
Paper Trade Rechecker — background loop that tracks arb survival.

Runs concurrently with the main scan loop. Periodically checks pending
paper trades at 5s, 10s, and 30s intervals.

Survival rules (strict):
  - Both legs must still exist in Redis
  - Neither leg can be suspended
  - Operator, selection, market, period, and line must match EXACTLY
  - Profit must remain >= min_arb_threshold
  - Odds data must be NEWER than the alert timestamp (stale-data guard)

CONCURRENCY NOTE:
  This runs as an asyncio.Task in the SAME event loop as the main scanner.
  Cooperative scheduling means there are no true data races. However, avoid
  adding `await` calls inside `_recheck_trade()` — doing so would create
  yield points where the recorder could append new trades mid-iteration.
  The `pending_trades` property returns a snapshot list, so iteration is
  safe, but in-place mutations on PaperTrade objects are shared references.
"""

import asyncio
import logging
from datetime import datetime, timezone
from collections import defaultdict

from backend.storage.redis_cache import redis_cache
from backend.normalization.models import NormalizedOdds, MarketStatus
from backend.engine.arb_detector import implied_probability
from backend.paper_trading.paper_trade_models import (
    PaperTrade,
    PaperTradeStatus,
    OddsSnapshot,
    RECHECK_INTERVALS,
)
from backend.paper_trading.paper_trade_recorder import PaperTradeRecorder

logger = logging.getLogger(__name__)


# Float precision for matching lines
LINE_TOLERANCE = 0.01

# Rechecker metrics keys
_METRIC_RECHECKS = "total_rechecks"
_METRIC_STALE_REJECTIONS = "stale_data_rejections"
_METRIC_SURVIVED = "total_survived"
_METRIC_BROKEN = "total_broken"


def _lines_match(line_a: float | None, line_b: float | None) -> bool:
    """Helper to safely compare line values with tolerance."""
    if line_a is None and line_b is None:
        return True
    if line_a is None or line_b is None:
        return False
    return abs(line_a - line_b) < LINE_TOLERANCE


class PaperTradeRechecker:
    """
    Background worker that re-tests arb opportunities against live odds.
    """

    def __init__(self, recorder: PaperTradeRecorder, check_interval_sec: float = 1.0):
        self._recorder = recorder
        self._check_interval_sec = check_interval_sec
        self._is_running = False
        self._task = None
        self._metrics = {
            _METRIC_RECHECKS: 0,
            _METRIC_STALE_REJECTIONS: 0,
            _METRIC_SURVIVED: 0,
            _METRIC_BROKEN: 0,
        }

    @property
    def metrics(self) -> dict[str, int]:
        """Rechecker performance metrics."""
        return dict(self._metrics)

    def start(self):
        """Start the background rechecker loop."""
        if not self._is_running:
            self._is_running = True
            self._task = asyncio.create_task(self._run_loop())
            logger.info("PaperTradeRechecker started.")

    async def stop(self):
        """Stop the background rechecker loop gracefully."""
        self._is_running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            logger.info(
                f"PaperTradeRechecker stopped. Metrics: {self._metrics}"
            )

    async def _run_loop(self):
        """Main recheck loop. Runs every check_interval_sec."""
        while self._is_running:
            try:
                await self._process_pending_trades()
            except Exception as e:
                logger.error(f"Error in PaperTradeRechecker loop: {e}", exc_info=True)
            
            await asyncio.sleep(self._check_interval_sec)

    async def _process_pending_trades(self):
        """Find trades due for a recheck and process them."""
        now = datetime.now(timezone.utc)
        # NOTE: pending_trades returns a new list but with shared PaperTrade
        # references. Mutations in _recheck_trade update the originals in
        # the recorder. This is intentional — do not copy the objects.
        pending = self._recorder.pending_trades

        if not pending:
            return

        # Group by event_id to minimize Redis queries
        trades_by_event: dict[str, list[tuple[PaperTrade, int]]] = defaultdict(list)
        due_count = 0

        for trade in pending:
            age_seconds = (now - trade.created_at).total_seconds()
            
            # FIX: Find the EARLIEST missing interval, not the latest.
            # Without `break`, a 35s-old trade would skip 5s and 10s
            # and jump straight to 30s, corrupting survival metrics.
            next_interval = None
            for interval in RECHECK_INTERVALS:
                if age_seconds >= interval:
                    if interval == 5 and trade.snapshot_5s is None:
                        next_interval = 5
                        break
                    elif interval == 10 and trade.snapshot_10s is None:
                        next_interval = 10
                        break
                    elif interval == 30 and trade.snapshot_30s is None:
                        next_interval = 30
                        break
            
            if next_interval is not None:
                trades_by_event[trade.canonical_event_id].append((trade, next_interval))
                due_count += 1

        if due_count > 0:
            logger.debug(
                f"Rechecker processing {due_count} trades across "
                f"{len(trades_by_event)} events (total pending: {len(pending)})."
            )

        # Process each event's trades together
        for event_id, trades_with_intervals in trades_by_event.items():
            # Fetch all current odds for this event from Redis
            live_odds = await redis_cache.get_all_odds_for_event(event_id)
            
            for trade, interval in trades_with_intervals:
                self._recheck_trade(trade, interval, live_odds, now)

    def _find_exact_leg(
        self, 
        trade: PaperTrade, 
        operator: str, 
        selection: str, 
        live_odds: list[NormalizedOdds]
    ) -> NormalizedOdds | None:
        """
        Finds the exact matching odds line for a leg.
        Must match: operator, selection, market_type, period, line_value.
        """
        if not operator or not selection:
            return None

        # Filter to exact match
        for odds in live_odds:
            if (
                odds.operator == operator
                and odds.selection == selection
                and odds.market_key.value == trade.market_type
                and odds.period == trade.period
                and _lines_match(odds.point, trade.line_value)
            ):
                return odds
                
        return None

    def _check_odds_freshness(
        self,
        trade: PaperTrade,
        legs: list[NormalizedOdds],
        interval: int,
    ) -> str | None:
        """
        Verify that all leg odds have timestamps NEWER than the alert.

        If the odds data predates the alert, the rechecker would be
        validating survival against the same data that triggered the
        alert — producing false survival results.

        Returns None if fresh, or a human-readable reason string if stale.
        """
        for leg in legs:
            ts = leg.timestamp
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)

            if ts <= trade.created_at:
                return (
                    f"Stale odds at {interval}s: {leg.operator}:{leg.selection} "
                    f"timestamp {ts.isoformat()} predates alert "
                    f"{trade.created_at.isoformat()}"
                )
        return None

    def _recheck_trade(
        self, 
        trade: PaperTrade, 
        interval: int, 
        live_odds: list[NormalizedOdds],
        now: datetime
    ):
        """
        Perform a single recheck on a trade at a specific interval.
        Updates the trade in-place with the snapshot and new status.
        """
        self._metrics[_METRIC_RECHECKS] += 1
        is_3way = bool(trade.operator_c)
        
        # 1. Find the exact matching legs in live odds
        leg_a = self._find_exact_leg(trade, trade.operator_a, trade.selection_a, live_odds)
        leg_b = self._find_exact_leg(trade, trade.operator_b, trade.selection_b, live_odds)
        leg_c = None
        
        if is_3way:
            leg_c = self._find_exact_leg(trade, trade.operator_c, trade.selection_c, live_odds)

        # 2. Build the snapshot
        snapshot = OddsSnapshot(
            timestamp=now,
            interval_seconds=interval,
            odds_a=leg_a.odds if leg_a else None,
            odds_b=leg_b.odds if leg_b else None,
            odds_c=leg_c.odds if leg_c else None,
            both_legs_found=False,
            any_suspended=False,
        )

        # 3. Apply survival rules
        
        # Rule check: Do all legs exist?
        if is_3way:
            legs_found = bool(leg_a and leg_b and leg_c)
        else:
            legs_found = bool(leg_a and leg_b)

        snapshot.both_legs_found = legs_found

        if not legs_found:
            # Missing line
            missing = []
            if not leg_a: missing.append(f"{trade.operator_a}:{trade.selection_a}")
            if not leg_b: missing.append(f"{trade.operator_b}:{trade.selection_b}")
            if is_3way and not leg_c: missing.append(f"{trade.operator_c}:{trade.selection_c}")
            
            trade.record_snapshot(snapshot)
            trade.mark_broken(
                PaperTradeStatus.BROKEN_BY_MISSING_LINE,
                f"Missing legs at {interval}s: {', '.join(missing)}",
                interval
            )
            self._metrics[_METRIC_BROKEN] += 1
            return

        # Rule check: Are the odds FRESH (newer than the alert)?
        # This prevents false survival when Redis hasn't been refreshed.
        found_legs = [leg_a, leg_b]
        if is_3way:
            found_legs.append(leg_c)

        stale_reason = self._check_odds_freshness(trade, found_legs, interval)
        if stale_reason:
            self._metrics[_METRIC_STALE_REJECTIONS] += 1
            trade.record_snapshot(snapshot)
            trade.mark_broken(
                PaperTradeStatus.BROKEN_BY_MISSING_LINE,
                stale_reason,
                interval,
            )
            self._metrics[_METRIC_BROKEN] += 1
            logger.warning(f"⚠️ Trade {trade.alert_id[:8]}: {stale_reason}")
            return

        # Rule check: Are any legs suspended?
        any_suspended = any(
            leg.status != MarketStatus.ACTIVE for leg in found_legs if leg
        )
        snapshot.any_suspended = any_suspended
        
        if any_suspended:
            suspended_ops = [
                leg.operator for leg in found_legs
                if leg and leg.status != MarketStatus.ACTIVE
            ]
            trade.record_snapshot(snapshot)
            trade.mark_broken(
                PaperTradeStatus.BROKEN_BY_SUSPENSION,
                f"Market suspended at {interval}s ({', '.join(suspended_ops)})",
                interval
            )
            self._metrics[_METRIC_BROKEN] += 1
            return

        # Rule check: Recompute profit% and check threshold
        if is_3way:
            arb_sum = (
                implied_probability(leg_a.odds)
                + implied_probability(leg_b.odds)
                + implied_probability(leg_c.odds)
            )
        else:
            arb_sum = (
                implied_probability(leg_a.odds)
                + implied_probability(leg_b.odds)
            )
            
        profit_pct = (1 - arb_sum) * 100
        snapshot.profit_pct = profit_pct
        trade.record_snapshot(snapshot)

        if profit_pct < trade.min_arb_threshold:
            trade.mark_broken(
                PaperTradeStatus.BROKEN_BY_MOVEMENT,
                f"Profit decayed at {interval}s: {profit_pct:.2f}% < {trade.min_arb_threshold}% limit",
                interval
            )
            self._metrics[_METRIC_BROKEN] += 1
            return

        # Data is valid, profit is good → Trade survives this interval!
        trade.mark_valid(interval)
        self._metrics[_METRIC_SURVIVED] += 1
        logger.debug(
            f"✅ Trade {trade.alert_id[:8]} survived {interval}s "
            f"(Profit {trade.profit_pct_at_alert:.2f}% → {profit_pct:.2f}%)"
        )
