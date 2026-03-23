"""
Paper Trade Recorder — freezes arb alert state into PaperTrade records.

Called from `run_scan_cycle()` after `apply_all_filters()` returns
validated arb opportunities.

Design:
  - Freezes the EXACT ArbOpportunity (full model_dump for reconstruction)
  - Ties each paper trade to the specific market_group_key
  - Stores min_arb_threshold for interpretability
  - Returns PaperTrade objects for the rechecker to track
  - In-memory storage (v1) — no external DB dependency
"""

import logging
from datetime import datetime, timezone

from backend.normalization.models import ArbOpportunity
from backend.paper_trading.paper_trade_models import (
    PaperTrade,
    PaperTradeStatus,
)

logger = logging.getLogger(__name__)


# Default max trades before evicting terminal entries
DEFAULT_MAX_TRADES = 10_000


class PaperTradeRecorder:
    """
    Creates PaperTrade records from detected arb opportunities.

    Each valid arb alert produces one PaperTrade that captures
    the full snapshot at alert time.

    Memory management: when the trade list exceeds max_trades,
    terminal (completed) trades are evicted in FIFO order.
    """

    def __init__(
        self,
        min_arb_threshold: float = 0.5,
        max_trades: int = DEFAULT_MAX_TRADES,
    ):
        self._trades: list[PaperTrade] = []
        self._min_arb_threshold = min_arb_threshold
        self._max_trades = max_trades
        self._stats = {
            "total_recorded": 0,
            "total_evicted": 0,
        }

    @property
    def trades(self) -> list[PaperTrade]:
        """All recorded paper trades."""
        return list(self._trades)

    @property
    def pending_trades(self) -> list[PaperTrade]:
        """
        Paper trades still awaiting re-checks.
        NOTE: Returns a new list, but the items are mutable PaperTrade references.
        Modifying them here will mutate the original objects.
        """
        return [t for t in self._trades if not t.is_terminal]

    @property
    def stats(self) -> dict[str, int]:
        return dict(self._stats)

    def record(
        self,
        arb: ArbOpportunity,
        market_group_key: str = "",
    ) -> PaperTrade:
        """
        Freeze an ArbOpportunity into a PaperTrade.

        The frozen_arb field contains the full serialized ArbOpportunity
        so the exact alert can be reconstructed later.
        """
        legs = arb.legs

        # Invariant: all legs in a valid arb must share the same period.
        # Upstream normalization guarantees this, but we assert here to
        # catch any future bugs early rather than producing corrupt data.
        if len(legs) >= 2:
            periods = {leg.period for leg in legs}
            if len(periods) > 1:
                logger.error(
                    f"Period mismatch in arb legs: {periods}. "
                    f"Event: {arb.event_name}, Market: {arb.market_key.value}. "
                    f"Skipping paper trade recording."
                )
                return None

        trade = PaperTrade(
            created_at=datetime.now(timezone.utc),
            status=PaperTradeStatus.ALERTED,

            # Event context
            canonical_event_id=arb.event_id,
            event_name=arb.event_name,
            sport_key=arb.sport_key,

            # Market context
            market_type=arb.market_key.value,
            # Upstream normalizer ensures all legs have the exact same period,
            # so using legs[0].period safely applies to the entire group.
            period=legs[0].period if legs else "",
            line_value=legs[0].point if legs else None,
            market_group_key=market_group_key,

            # Leg A
            operator_a=legs[0].operator if len(legs) > 0 else "",
            selection_a=legs[0].selection if len(legs) > 0 else "",
            odds_a_at_alert=legs[0].odds if len(legs) > 0 else 0.0,

            # Leg B
            operator_b=legs[1].operator if len(legs) > 1 else "",
            selection_b=legs[1].selection if len(legs) > 1 else "",
            odds_b_at_alert=legs[1].odds if len(legs) > 1 else 0.0,

            # Leg C (3-way only)
            operator_c=legs[2].operator if len(legs) > 2 else "",
            selection_c=legs[2].selection if len(legs) > 2 else "",
            odds_c_at_alert=legs[2].odds if len(legs) > 2 else 0.0,

            # Alert-time calculations
            profit_pct_at_alert=arb.profit_percent,
            stake_plan=dict(arb.stakes),
            min_arb_threshold=self._min_arb_threshold,

            # Frozen state — full ArbOpportunity for reconstruction
            frozen_arb=arb.model_dump(mode="json"),
        )

        self._trades.append(trade)
        self._stats["total_recorded"] += 1

        # Evict old terminal trades if we exceed the memory cap
        if len(self._trades) > self._max_trades:
            self._evict_terminal_trades()

        logger.info(
            f"📝 PAPER TRADE RECORDED: {trade.alert_id[:8]} | "
            f"{trade.operator_a} vs {trade.operator_b} | "
            f"{trade.selection_a} vs {trade.selection_b} | "
            f"profit: {trade.profit_pct_at_alert:.2f}% | "
            f"group: {market_group_key}"
        )

        return trade

    def record_batch(
        self,
        arbs: list[ArbOpportunity],
        market_group_keys: dict[str, str] | None = None,
    ) -> list[PaperTrade]:
        """
        Record multiple arbs as paper trades.

        market_group_keys maps arb.id → market_group_key.
        If not provided, market_group_key defaults to empty string.
        """
        trades = []
        keys = market_group_keys or {}

        for arb in arbs:
            key = keys.get(arb.id, "")
            trade = self.record(arb, market_group_key=key)
            if trade is not None:
                trades.append(trade)

        return trades

    def _evict_terminal_trades(self):
        """Remove the oldest terminal trades to stay under memory cap."""
        before = len(self._trades)
        # Keep all pending (non-terminal) trades, evict oldest terminal ones
        pending = [t for t in self._trades if not t.is_terminal]
        terminal = [t for t in self._trades if t.is_terminal]
        
        # Keep the most recent half of terminal trades
        keep_count = max(len(terminal) // 2, 0)
        kept_terminal = terminal[-keep_count:] if keep_count > 0 else []
        
        self._trades = pending + kept_terminal
        evicted = before - len(self._trades)
        self._stats["total_evicted"] += evicted
        
        if evicted > 0:
            logger.info(f"♻️ Evicted {evicted} terminal paper trades (cap: {self._max_trades}).")

    def clear(self):
        """Reset all paper trades."""
        self._trades.clear()
