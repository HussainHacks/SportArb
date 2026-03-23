"""
Market Index — three-index architecture for incremental arb recomputation.

Instead of rebuilding all market groups from scratch each cycle, this module
maintains a persistent in-memory index that is updated incrementally as new
odds arrive. Only the affected market group is recomputed for arb detection.

Three indexes:
  1. market_index    — MarketGroup by market_key  (core structure)
  2. operator_index  — market_keys per operator    (fast update routing)
  3. event_index     — market_keys per event       (cleanup / grouping)

Usage:
    index = MarketIndex()
    changed_keys = index.upsert_odds(normalized_odds_list)
    for key in changed_keys:
        group = index.get_group(key)
        arbs = arb_engine.recompute(group)
"""

import logging
from datetime import datetime, timezone
from dataclasses import dataclass, field
from collections import defaultdict

from backend.normalization.models import NormalizedOdds

logger = logging.getLogger(__name__)


@dataclass
class OddsEntry:
    """A single odds value with metadata, stored inside a MarketGroup."""
    odds: float
    operator: str
    operator_display: str
    selection: str
    timestamp: datetime
    is_live: bool = False
    # Keep the full NormalizedOdds for arb reporting
    source: NormalizedOdds | None = None


class MarketGroup:
    """
    One exact betting market: event + market_type + period + line.

    Stores the current odds from every operator for every selection.
    Structure: books[operator][selection] = OddsEntry
    """

    def __init__(self, key: str, event_id: str, event_name: str,
                 sport_key: str, market_key_str: str):
        self.key = key
        self.event_id = event_id
        self.event_name = event_name
        self.sport_key = sport_key
        self.market_key_str = market_key_str
        self.books: dict[str, dict[str, OddsEntry]] = {}
        self.last_updated: datetime = datetime.now(timezone.utc)

    def update(self, odds: NormalizedOdds) -> bool:
        """
        Upsert an odds line into this group.

        Returns True if the odds actually changed (different value or new entry).
        Always refreshes the stored timestamp so freshness logic stays accurate
        even when the odds value itself hasn't changed.
        """
        operator = odds.operator
        selection = odds.selection

        if operator not in self.books:
            self.books[operator] = {}

        old = self.books[operator].get(selection)
        changed = old is None or old.odds != odds.odds

        self.books[operator][selection] = OddsEntry(
            odds=odds.odds,
            operator=odds.operator,
            operator_display=odds.operator_display,
            selection=selection,
            timestamp=odds.timestamp,
            is_live=odds.is_live,
            source=odds,
        )
        self.last_updated = datetime.now(timezone.utc)
        return changed

    def get_all_odds(self) -> list[NormalizedOdds]:
        """Flatten all current odds into a list for the arb detector."""
        result = []
        for selections in self.books.values():
            for entry in selections.values():
                if entry.source is not None:
                    result.append(entry.source)
        return result

    @property
    def operator_count(self) -> int:
        return len(self.books)

    @property
    def selection_names(self) -> set[str]:
        names = set()
        for selections in self.books.values():
            names.update(selections.keys())
        return names

    def remove_operator(self, operator: str) -> bool:
        """Remove an operator's odds. Returns True if the operator existed."""
        if operator in self.books:
            del self.books[operator]
            return True
        return False

    def __repr__(self) -> str:
        ops = list(self.books.keys())
        return (
            f"MarketGroup({self.key} | {self.operator_count} books | "
            f"selections={self.selection_names} | ops={ops})"
        )


class MarketIndex:
    """
    Three-index structure for fast incremental odds updates and arb detection.

    Maintains:
      - market_index:   market_key → MarketGroup
      - operator_index: operator → set(market_keys)
      - event_index:    event_id → set(market_keys)
    """

    def __init__(self):
        self._markets: dict[str, MarketGroup] = {}
        self._operator_index: dict[str, set[str]] = defaultdict(set)
        self._event_index: dict[str, set[str]] = defaultdict(set)
        self._total_updates = 0
        self._total_changes = 0
        self._total_deletes = 0

    @property
    def market_count(self) -> int:
        return len(self._markets)

    @property
    def operator_count(self) -> int:
        return len(self._operator_index)

    @property
    def event_count(self) -> int:
        return len(self._event_index)

    def get_group(self, market_key: str) -> MarketGroup | None:
        """Get a market group by key."""
        return self._markets.get(market_key)

    def get_groups_for_operator(self, operator: str) -> list[MarketGroup]:
        """Get all market groups that contain odds from this operator."""
        keys = self._operator_index.get(operator, set())
        return [self._markets[k] for k in keys if k in self._markets]

    def get_groups_for_event(self, event_id: str) -> list[MarketGroup]:
        """Get all market groups for an event."""
        keys = self._event_index.get(event_id, set())
        return [self._markets[k] for k in keys if k in self._markets]

    def upsert_odds(self, odds_list: list[NormalizedOdds]) -> set[str]:
        """
        Upsert a batch of normalized odds into the index.

        Returns the set of market_keys whose odds actually changed
        (only these need arb recomputation).
        """
        changed_keys: set[str] = set()

        for odds in odds_list:
            market_key = self._build_market_key(odds)
            self._total_updates += 1

            # Create group if it doesn't exist
            if market_key not in self._markets:
                self._markets[market_key] = MarketGroup(
                    key=market_key,
                    event_id=odds.event_id,
                    event_name=odds.event_name,
                    sport_key=odds.sport_key,
                    market_key_str=odds.market_key.value,
                )

            group = self._markets[market_key]
            did_change = group.update(odds)

            if did_change:
                changed_keys.add(market_key)
                self._total_changes += 1

            # Update indexes
            self._operator_index[odds.operator].add(market_key)
            self._event_index[odds.event_id].add(market_key)

        return changed_keys

    def remove_operator_from_event(self, operator: str, event_id: str) -> set[str]:
        """
        Remove an operator's odds from all market groups for a specific event.
        Used when a WebSocket 'deleted' or 'no_markets' message arrives.
        
        Returns the set of market_keys that were changed (so they can be recomputed).
        """
        changed_keys: set[str] = set()
        
        # We only need to check markets that belong to both the event and the operator
        event_keys = self._event_index.get(event_id, set())
        operator_keys = self._operator_index.get(operator, set())
        
        # Intersection gives us exactly the keys to check
        keys_to_check = event_keys.intersection(operator_keys)
        
        for key in list(keys_to_check):
            group = self._markets.get(key)
            if group and group.remove_operator(operator):
                changed_keys.add(key)
                self._operator_index[operator].discard(key)
                self._total_deletes += 1

        if changed_keys:
            logger.info(f"🗑️ Removed {operator} odds from {len(changed_keys)} markets for event {event_id}")
            
        return changed_keys

    def remove_event(self, event_id: str) -> int:
        """
        Remove all market groups for an event (e.g., event finished).

        Returns the number of groups removed.
        """
        keys = self._event_index.pop(event_id, set())
        removed = 0

        for key in keys:
            group = self._markets.pop(key, None)
            if group:
                removed += 1
                # Clean up operator index
                for operator in group.books:
                    self._operator_index[operator].discard(key)

        if removed:
            logger.info(f"🗑️ Removed {removed} market groups for event {event_id}")
        return removed

    def remove_stale_groups(self, max_age_seconds: float = 600) -> int:
        """Remove market groups that haven't been updated in a while."""
        now = datetime.now(timezone.utc)
        stale_keys = [
            key for key, group in self._markets.items()
            if (now - group.last_updated).total_seconds() > max_age_seconds
        ]

        for key in stale_keys:
            group = self._markets.pop(key, None)
            if group:
                self._event_index[group.event_id].discard(key)
                for operator in group.books:
                    self._operator_index[operator].discard(key)

        if stale_keys:
            logger.info(f"🗑️ Removed {len(stale_keys)} stale market groups")
        return len(stale_keys)

    def get_stats(self) -> dict:
        """Diagnostic stats for logging."""
        return {
            "markets": self.market_count,
            "operators": self.operator_count,
            "events": self.event_count,
            "total_updates": self._total_updates,
            "total_changes": self._total_changes,
            "total_deletes": self._total_deletes,
            "change_rate": (
                f"{self._total_changes / self._total_updates * 100:.1f}%"
                if self._total_updates > 0 else "N/A"
            ),
        }

    @staticmethod
    def _build_market_key(odds: NormalizedOdds) -> str:
        """Build the canonical market key from an odds line.

        For spreads, uses abs(point) so +6.5 and -6.5 land in the same group
        but 6.5 and 7.0 stay separate.  The arb detector's complementary-
        selection check already validates that point_A == -point_B.

        For totals and h2h, uses the raw point (or 'none').
        """
        if odds.market_key.value == "spreads":
            line_part = f"{abs(odds.point)}" if odds.point is not None else "none"
        else:
            line_part = f"{odds.point}" if odds.point is not None else "none"
        return f"{odds.event_id}:{odds.market_key.value}:{odds.period}:{line_part}"
