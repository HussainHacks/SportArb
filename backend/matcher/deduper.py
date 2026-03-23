"""
Deduper — suppresses duplicate odds records from the same source.

Dedupe key: (source, source_event_id, market_type, period, selection, line_value)

Rules:
  - If a new record has the same key and a NEWER timestamp → replace (odds update)
  - If same key and older/equal timestamp → discard (stale duplicate)

Odds-drift tracking:
  When a record replaces an older one, the odds change is logged.
  This creates an observable trail of line movement that's useful
  for debugging and for detecting stale feed data.

Resolution note:
  Deduplication happens BEFORE event resolution and market matching.
  Records pass through the deduper first, then into the matcher pipeline.
  The deduper does NOT filter on resolution quality — that belongs
  downstream. But it DOES use normalized field values in the key,
  so resolution affects what counts as a "duplicate."
"""

import logging
from datetime import datetime, timedelta, timezone
from backend.matcher.event_candidate_generator import NormalizedRecord

logger = logging.getLogger(__name__)

# Records older than this are considered stale and can be pruned
STALE_RECORD_AGE = timedelta(hours=6)


def _dedupe_key(record: NormalizedRecord) -> str:
    """
    Build a unique key for deduplication.

    Key components are all normalized values from the record,
    ensuring that two records that LOOK the same after normalization
    are treated as duplicates regardless of original formatting.
    """
    line_str = f"{record.line_value:.2f}" if record.line_value is not None else "none"
    return (
        f"{record.source}|"
        f"{record.source_event_id}|"
        f"{record.market_type}|"
        f"{record.period}|"
        f"{record.selection}|"
        f"{line_str}"
    )


class Deduper:
    """
    Tracks seen records and removes duplicates.

    Preserves the freshest version of each unique odds position.
    Tracks odds drift (line movement) for debugging and observability.
    """

    def __init__(self):
        # Key → record (the freshest version)
        self._seen: dict[str, NormalizedRecord] = {}
        self._stats = {
            "total_processed": 0,
            "new_records": 0,
            "updates_accepted": 0,
            "duplicates_discarded": 0,
            "stale_pruned": 0,
            "odds_drift_events": 0,
        }

    @property
    def stats(self) -> dict[str, int]:
        """Return deduper statistics."""
        return dict(self._stats)

    def process(self, record: NormalizedRecord) -> NormalizedRecord | None:
        """
        Process a record through the deduper.

        Returns:
          - The record if it's new or fresher than the existing one
          - None if it's a duplicate that should be discarded
        """
        self._stats["total_processed"] += 1
        key = _dedupe_key(record)

        if key not in self._seen:
            self._seen[key] = record
            self._stats["new_records"] += 1
            return record

        existing = self._seen[key]

        if record.timestamp_utc > existing.timestamp_utc:
            # Newer timestamp → replace (odds update)
            self._seen[key] = record
            self._stats["updates_accepted"] += 1

            # Track odds drift
            if record.odds_decimal != existing.odds_decimal:
                self._stats["odds_drift_events"] += 1
                logger.debug(
                    f"📈 Odds drift: {record.source}:{record.source_event_id} "
                    f"{record.selection} {existing.odds_decimal} → {record.odds_decimal}"
                )
            else:
                logger.debug(f"Updated record (same odds): {key}")

            return record
        else:
            # Older or same → discard
            self._stats["duplicates_discarded"] += 1
            logger.debug(f"Discarded duplicate: {key}")
            return None

    def process_batch(self, records: list[NormalizedRecord]) -> list[NormalizedRecord]:
        """Process a batch, returning only non-duplicate records."""
        results = []
        for record in records:
            result = self.process(record)
            if result is not None:
                results.append(result)

        discarded = len(records) - len(results)
        if discarded > 0:
            logger.info(f"Deduper: kept {len(results)}, discarded {discarded} duplicates")
        return results

    def prune_stale(self, now: datetime | None = None) -> int:
        """
        Remove records older than STALE_RECORD_AGE.

        Returns the number of records pruned.
        Useful for long-running scanners to prevent memory growth.
        """
        if now is None:
            now = datetime.now(timezone.utc)

        cutoff = now - STALE_RECORD_AGE
        stale_keys = [
            key for key, record in self._seen.items()
            if record.timestamp_utc < cutoff
        ]

        for key in stale_keys:
            del self._seen[key]

        self._stats["stale_pruned"] += len(stale_keys)

        if stale_keys:
            logger.info(f"Deduper: pruned {len(stale_keys)} stale records")

        return len(stale_keys)

    def clear(self):
        """Reset the deduper state."""
        self._seen.clear()

    @property
    def size(self) -> int:
        return len(self._seen)

    def get_all_current(self) -> list[NormalizedRecord]:
        """Return all current (non-stale) records. Useful for debugging."""
        return list(self._seen.values())
