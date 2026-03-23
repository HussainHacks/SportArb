"""
Market Matcher — groups odds records into matchable markets.

This is the most dangerous file before the arb engine.
If event matching is probabilistic, market matching is where
probability stops. Everything here must be nearly exact.

CORE INVARIANT:
  A market group is only actionable if it is:
    1. Exact on canonical event ID
    2. Exact on canonical market type
    3. Exact on canonical period
    4. Exact on line value (for spreads/totals)
    5. Valid on complementary selections
  
  That is the whole philosophy of this file.

LINE VALUE INVARIANT:
  abs() is used for BUCKET IDENTITY only (grouping +3.5 with -3.5).
  The original SIGNED value is preserved on every record and used
  for complementary truth validation. Never reconstruct a line
  from the group key — always use record.line_value.

Must-have rules enforced:
  - Exact canonical event ID match
  - Exact canonical market type match
  - Exact canonical period match
  - Exact line value match for spreads/totals (float precision: 0.01)
  - Complementary side validation only (no best-effort pairing)
  - Strict separation of 2-way and 3-way markets (never mixed)
  - At least 2 DISTINCT operators required (same-operator pairs aren't arbs)

Failure modes defended against:
  - full_game matched to first_half
  - h2h (moneyline) treated like 1x2 (moneyline_3way)
  - +3.5 paired with -4.0
  - over 220.5 paired with under 221.5
  - alternate lines accidentally mixed with main lines
  - home/away selections normalized but paired incorrectly
  - one record making multiple incompatible candidate groups
  - unresolved market/period records silently entering arb groups
  - 3-way group with 3 nonsense selections passing count check
  - same-operator pair treated as arb-ready
"""

import logging
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any

from backend.matcher.event_candidate_generator import NormalizedRecord

logger = logging.getLogger(__name__)


# Precision for float line comparison
LINE_VALUE_TOLERANCE = 0.01


def _market_group_key(record: NormalizedRecord, canonical_event_id: str) -> str:
    """
    Build a strict grouping key for market matching.

    Key: {canonical_event_id}|{market_type}|{period}|{line_value}

    Every component is exact — there is no fuzzy matching at this layer.

    Line value handling:
      - abs() for BUCKET IDENTITY only (grouping complementary sides)
      - :.2f formatting for float consistency
      - The original signed value stays on record.line_value for
        complementary validation (never reconstruct from key)

    IMPORTANT: moneyline and moneyline_3way produce DIFFERENT keys.
    They are different market types and must NEVER be in the same group.
    """
    if record.line_value is not None:
        # abs() for bucket identity, signed value preserved on record
        line_str = f"{abs(record.line_value):.2f}"
    else:
        line_str = "none"
    return f"{canonical_event_id}|{record.market_type}|{record.period}|{line_str}"


@dataclass
class MarketGroupAudit:
    """
    Audit metadata for a market group returned by get_actionable_groups().

    This is the debug-visible context for every group that reaches
    the arb engine.
    """
    group_key: str = ""
    canonical_event_id: str = ""
    market_type: str = ""
    period: str = ""
    line_value_key: str = ""
    selections: set = field(default_factory=set)
    operators: set = field(default_factory=set)
    record_count: int = 0
    complementary_reason: str = ""

    def __repr__(self):
        return (
            f"MarketGroupAudit({self.market_type}|{self.period}|{self.line_value_key} "
            f"sel={self.selections} ops={self.operators} n={self.record_count})"
        )


class MarketMatcher:
    """
    Groups normalized records into market buckets for arb detection.

    Strict matching: event + market + period + line must all match exactly.
    Resolution-aware: records with unresolved market/period are rejected.

    One record enters exactly one group (determined by its group key).
    A record cannot participate in multiple incompatible groups because
    the key is fully deterministic from (event, market, period, line).
    """

    def __init__(self):
        # Key: market_group_key → list of records
        self._markets: dict[str, list[NormalizedRecord]] = defaultdict(list)
        self._stats = {
            "records_added": 0,
            "records_rejected_unresolved_market": 0,
            "records_rejected_unresolved_period": 0,
            "groups_total": 0,
            "groups_actionable": 0,
            # Invalid-group reason buckets
            "invalid_too_few_records": 0,
            "invalid_single_operator": 0,
            "invalid_too_few_selections": 0,
            "invalid_mixed_market_type": 0,
            "invalid_mixed_period": 0,
            "invalid_draw_in_2way": 0,
            "invalid_missing_draw_3way": 0,
            "invalid_non_complementary_spread": 0,
            "invalid_non_complementary_total": 0,
            "invalid_unknown_market_type": 0,
            "groups_complementary_valid": 0,
        }

    @property
    def stats(self) -> dict[str, int]:
        """Return market matcher statistics."""
        return dict(self._stats)

    def add_record(self, canonical_event_id: str, record: NormalizedRecord):
        """
        Add a record to the appropriate market group.

        Records with unresolved market types or periods are rejected
        from arb-critical groups and logged as warnings.

        Each record enters exactly ONE group (deterministic key).
        """
        # Gate: reject records with unresolved market type
        if not record.market_is_canonical:
            self._stats["records_rejected_unresolved_market"] += 1
            logger.warning(
                f"⚠️ MARKET MATCHER REJECT: unresolved market type "
                f"'{record.market_type}' from {record.source}:{record.source_event_id} "
                f"— record not added to any market group"
            )
            return

        # Gate: reject records with unresolved period
        if not record.period_is_canonical:
            self._stats["records_rejected_unresolved_period"] += 1
            logger.warning(
                f"⚠️ MARKET MATCHER REJECT: unresolved period "
                f"'{record.period}' from {record.source}:{record.source_event_id} "
                f"— record not added to any market group"
            )
            return

        key = _market_group_key(record, canonical_event_id)
        self._markets[key].append(record)
        self._stats["records_added"] += 1

    def get_all_market_groups(self) -> dict[str, list[NormalizedRecord]]:
        """Return all market groups."""
        self._stats["groups_total"] = len(self._markets)
        return dict(self._markets)

    def get_actionable_groups(
        self,
    ) -> dict[str, tuple[list[NormalizedRecord], MarketGroupAudit]]:
        """
        Return only market groups that are safe for arb calculation.

        Requirements:
          - At least 2 records from different operators
          - At least 2 distinct selections (for 2-way) or 3 (for 3-way)
          - Complementary validation passes

        Returns dict of group_key → (records, audit_metadata).
        The audit metadata provides full debug context for every
        group that reaches the arb engine.
        """
        actionable = {}

        for key, records in self._markets.items():
            if len(records) < 2:
                self._stats["invalid_too_few_records"] += 1
                continue

            operators = {r.source for r in records}
            if len(operators) < 2:
                self._stats["invalid_single_operator"] += 1
                continue

            selections = {r.selection for r in records}

            # Determine required selection count by market type
            market_type = records[0].market_type
            if market_type == "moneyline_3way":
                if len(selections) < 3:
                    self._stats["invalid_too_few_selections"] += 1
                    continue
            else:
                if len(selections) < 2:
                    self._stats["invalid_too_few_selections"] += 1
                    continue

            # Complementary validation — explicit, not assumed
            valid, reason = self._validate_complementary_with_reason(records)

            # Build audit metadata
            key_parts = key.split("|")
            audit = MarketGroupAudit(
                group_key=key,
                canonical_event_id=key_parts[0] if len(key_parts) > 0 else "",
                market_type=key_parts[1] if len(key_parts) > 1 else "",
                period=key_parts[2] if len(key_parts) > 2 else "",
                line_value_key=key_parts[3] if len(key_parts) > 3 else "",
                selections=selections,
                operators=operators,
                record_count=len(records),
                complementary_reason=reason,
            )

            if valid:
                actionable[key] = (records, audit)
                self._stats["groups_complementary_valid"] += 1
            else:
                logger.warning(
                    f"⚠️ GROUP FAILED COMPLEMENTARY: {key} | "
                    f"reason: {reason} | "
                    f"selections: {selections} | "
                    f"operators: {operators} | "
                    f"records: {len(records)}"
                )

        self._stats["groups_actionable"] = len(actionable)
        self._stats["groups_total"] = len(self._markets)

        logger.info(
            f"Market matcher: {len(actionable)} actionable groups "
            f"out of {len(self._markets)} total"
        )
        return actionable

    def validate_complementary(
        self, records: list[NormalizedRecord]
    ) -> bool:
        """
        Public convenience wrapper — returns bool only.

        For internal use and detailed reason tracking, use
        _validate_complementary_with_reason().
        """
        valid, _ = self._validate_complementary_with_reason(records)
        return valid

    def _validate_complementary_with_reason(
        self, records: list[NormalizedRecord]
    ) -> tuple[bool, str]:
        """
        Validate complementary outcomes with explicit reason tracking.

        Returns (is_valid, reason_string).

        This is the strictest check in the entire matching pipeline.
        If this passes, the group is safe for arb calculation.

        Rules:
          Moneyline 2-way:  exactly 2 distinct team selections, NO draw
          Moneyline 3-way:  exactly draw + 2 team selections (NOT just "3 unique labels")
          Spreads:          opposite point values (+X, -X) with same magnitude
          Totals:           same line value, "over" + "under" present
        """
        if not records:
            return False, "empty_group"

        market_type = records[0].market_type
        selections = {r.selection for r in records}

        # --- STRICT: all records must share the same market type ---
        if any(r.market_type != market_type for r in records):
            self._stats["invalid_mixed_market_type"] += 1
            return False, f"mixed_market_types:{set(r.market_type for r in records)}"

        # --- STRICT: all records must share the same period ---
        periods = {r.period for r in records}
        if len(periods) > 1:
            self._stats["invalid_mixed_period"] += 1
            return False, f"mixed_periods:{periods}"

        if market_type == "moneyline":
            # 2-way: exactly 2 distinct team selections, NO draw
            if "draw" in selections:
                self._stats["invalid_draw_in_2way"] += 1
                return False, "draw_in_2way_moneyline"
            if len(selections) == 2:
                return True, "valid_2way_moneyline"
            return False, f"wrong_selection_count_2way:{len(selections)}"

        if market_type == "moneyline_3way":
            # 3-way: must have draw + exactly 2 non-draw team selections
            # NOT just "3 unique labels" — "team A / team B / under" is nonsense
            if "draw" not in selections:
                self._stats["invalid_missing_draw_3way"] += 1
                return False, f"no_draw_in_3way:{selections}"
            non_draw = selections - {"draw"}
            # Non-draw selections should be team names (not over/under/etc.)
            invalid_non_draw = non_draw.intersection({"over", "under"})
            if invalid_non_draw:
                self._stats["invalid_missing_draw_3way"] += 1
                return False, f"non_team_selections_in_3way:{invalid_non_draw}"
            if len(non_draw) == 2:
                return True, "valid_3way_moneyline"
            return False, f"wrong_non_draw_count_3way:{len(non_draw)}"

        if market_type == "spread":
            return self._validate_complementary_spread(records)

        if market_type == "total":
            return self._validate_complementary_total(records)

        # Unknown market type — reject
        self._stats["invalid_unknown_market_type"] += 1
        return False, f"unknown_market_type:{market_type}"

    def _validate_complementary_spread(
        self, records: list[NormalizedRecord]
    ) -> tuple[bool, str]:
        """
        Validate spread complementary: -X.Y must pair with +X.Y

        Uses the SIGNED line_value from the record (NOT the abs key).

        Rules:
          - Both sides must have line values
          - Lines must be opposite signs, same magnitude
          - Tolerance: 0.01 (float precision only, not fuzzy)

        Failure modes:
          - +3.5 paired with -4.0 → REJECT
          - +3.5 paired with -3.5 → ACCEPT
        """
        # Collect signed line values by selection
        points: dict[str, float] = {}
        for r in records:
            if r.line_value is not None:
                # Use first seen signed line per selection
                if r.selection not in points:
                    points[r.selection] = r.line_value

        if len(points) < 2:
            self._stats["invalid_non_complementary_spread"] += 1
            return False, f"spread_insufficient_lines:{points}"

        # Check all pairs for exact complementary
        point_values = list(points.values())
        for i in range(len(point_values)):
            for j in range(i + 1, len(point_values)):
                # Sum of complementary lines should be 0
                # e.g., +3.5 + (-3.5) = 0
                if abs(point_values[i] + point_values[j]) < LINE_VALUE_TOLERANCE:
                    return True, f"valid_spread:{point_values[i]}/{point_values[j]}"

        self._stats["invalid_non_complementary_spread"] += 1
        return False, f"spread_lines_not_complementary:{points}"

    def _validate_complementary_total(
        self, records: list[NormalizedRecord]
    ) -> tuple[bool, str]:
        """
        Validate total complementary: "over" + "under" at same line.

        Rules:
          - Must have both "over" and "under" selections
          - Line values for over and under MUST match exactly
          - over 220.5 paired with under 221.5 → REJECT

        Failure modes:
          - over 220.5 vs under 221.5 → REJECT (different lines)
          - over 220.5 vs under 220.5 → ACCEPT
        """
        selections = {r.selection for r in records}

        if "over" not in selections or "under" not in selections:
            self._stats["invalid_non_complementary_total"] += 1
            return False, f"total_missing_side:{selections}"

        # Get line values for each side
        over_lines: set[float] = set()
        under_lines: set[float] = set()
        for r in records:
            if r.line_value is not None:
                if r.selection == "over":
                    over_lines.add(r.line_value)
                elif r.selection == "under":
                    under_lines.add(r.line_value)

        if not over_lines or not under_lines:
            self._stats["invalid_non_complementary_total"] += 1
            return False, f"total_missing_line_values:over={over_lines},under={under_lines}"

        # At least one matching line across over/under
        for ol in over_lines:
            for ul in under_lines:
                if abs(ol - ul) < LINE_VALUE_TOLERANCE:
                    return True, f"valid_total:{ol}"

        self._stats["invalid_non_complementary_total"] += 1
        return False, f"total_line_mismatch:over={over_lines},under={under_lines}"

    def clear(self):
        """Reset all market groups."""
        self._markets.clear()
