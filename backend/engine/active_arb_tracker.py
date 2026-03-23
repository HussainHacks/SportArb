"""
Active Arb Tracker — prevents duplicate alerts and tracks arb lifecycle.

When an arb is detected via incremental recomputation, there are two cases:
  1. NEW arb  → alert
  2. EXISTING arb → no repeat alert (unless profit changed significantly)

When an arb disappears (after recomputation finds no arb in a previously
active group), the tracker removes it.

This also provides data for paper trading: we know exactly when an arb
appeared and when it vanished.
"""

import logging
from datetime import datetime, timezone

from backend.normalization.models import ArbOpportunity

logger = logging.getLogger(__name__)


class ActiveArbTracker:
    """
    Tracks currently active arbs by market_group_key.

    Prevents duplicate alerts and logs appearance/disappearance.
    """

    def __init__(self, profit_change_threshold: float = 0.5):
        """
        Args:
            profit_change_threshold: If profit changes by more than this %,
                re-alert. E.g., 0.5 means re-alert if profit went from 1.2%
                to 1.8% or above.
        """
        self._active: dict[str, ArbOpportunity] = {}
        self._profit_threshold = profit_change_threshold
        self.total_new = 0
        self.total_disappeared = 0
        self.total_updated = 0
        self.total_suppressed = 0

    @property
    def active_count(self) -> int:
        return len(self._active)

    @property
    def active_arbs(self) -> dict[str, ArbOpportunity]:
        return dict(self._active)

    def process_arbs(
        self,
        market_key: str,
        arbs: list[ArbOpportunity],
    ) -> list[ArbOpportunity]:
        """
        Process arb results for one market group after recomputation.

        Returns a list of arbs that should generate NEW alerts.
        An arb generates an alert if:
          - It's new (market_key not in active)
          - Its profit changed significantly

        If arbs is empty and the market was previously active, the arb
        has disappeared and is removed from tracking.
        """
        alertable: list[ArbOpportunity] = []

        if not arbs:
            # Arb disappeared — this is normal, markets corrected
            if market_key in self._active:
                old = self._active.pop(market_key)
                self.total_disappeared += 1
                logger.debug(
                    f"📉 Arb disappeared: {old.event_name} "
                    f"({old.profit_percent:.2f}%)"
                )
            return alertable

        # Take the best arb for this market group
        best = max(arbs, key=lambda a: a.profit_percent)

        existing = self._active.get(market_key)

        if existing is None:
            # New arb — alert
            self._active[market_key] = best
            self.total_new += 1
            alertable.append(best)
            logger.info(
                f"🆕 New arb: {best.event_name} | "
                f"{best.profit_percent:.2f}% | {market_key}"
            )
        else:
            # Existing arb — check if profit changed significantly
            profit_delta = abs(best.profit_percent - existing.profit_percent)
            if profit_delta >= self._profit_threshold:
                self._active[market_key] = best
                self.total_updated += 1
                alertable.append(best)
                logger.info(
                    f"📈 Arb updated: {best.event_name} | "
                    f"{existing.profit_percent:.2f}% → {best.profit_percent:.2f}%"
                )
            else:
                # Suppress — same arb, similar profit
                self._active[market_key] = best  # update odds but don't alert
                self.total_suppressed += 1

        return alertable

    def clear_event(self, event_id: str) -> int:
        """Remove all active arbs for a finished event."""
        to_remove = [
            key for key, arb in self._active.items()
            if arb.event_id == event_id
        ]
        for key in to_remove:
            del self._active[key]
        return len(to_remove)

    def get_stats(self) -> dict:
        return {
            "active": self.active_count,
            "total_new": self.total_new,
            "total_disappeared": self.total_disappeared,
            "total_updated": self.total_updated,
            "total_suppressed": self.total_suppressed,
        }
