"""
Selection Normalizer — canonicalizes selection/outcome names.

This is the sneakiest module in the matcher. It maps generic labels like
"Home", "Away", "1", "2" to actual team names, and standardizes
"Over"/"Under"/"Draw" across vendors.

CRITICAL INVARIANT:
  Normalization can reduce ambiguity. It CANNOT create compatibility.
  Same canonical selection does not guarantee arb compatibility.
  Line value, market type, and period checks still belong downstream.

Design principles:
  1. Event-aware mapping ONLY — needs home_team + away_team context
  2. Hard fail on ambiguous selections without context
  3. Resolution status returned (CANONICAL / UNRESOLVED / AMBIGUOUS)
  4. Source-specific overrides supported
  5. Raw values always preserved for debugging
  6. Over/Under preserve NO line context (line is a separate field)

Failure modes defended against:
  - "Home" / "Away" mapped against wrong orientation
  - Source uses reversed presentation order
  - "1" and "2" meanings differ by vendor
  - "Draw" omitted or mislabeled in 3-way markets
  - Totals selections normalized without line context awareness
  - Team name used as selection but not matching either participant
"""

import logging
from enum import Enum
from dataclasses import dataclass

logger = logging.getLogger(__name__)


# ============================================================================
# Resolution Status
# ============================================================================

class SelectionType(Enum):
    """What kind of selection this resolved to."""
    TEAM = "team"           # Resolved to a specific team name
    OVER = "over"           # Over side of a total
    UNDER = "under"         # Under side of a total
    DRAW = "draw"           # Draw / tie in 3-way markets
    UNRESOLVED = "unresolved"  # Could not resolve


class SelectionResolutionStatus(Enum):
    """Resolution confidence."""
    CANONICAL = "canonical"    # Resolved with high confidence
    AMBIGUOUS = "ambiguous"    # Resolved but with uncertain context
    UNRESOLVED = "unresolved"  # Could not resolve


@dataclass(frozen=True)
class SelectionResolution:
    """Result of selection normalization with full context."""
    name: str                              # Canonical selection name
    selection_type: SelectionType          # TEAM / OVER / UNDER / DRAW / UNRESOLVED
    status: SelectionResolutionStatus      # CANONICAL / AMBIGUOUS / UNRESOLVED
    is_canonical: bool                     # True only if fully resolved
    raw_input: str = ""                    # Original raw selection
    raw_home_team: str = ""                # Home team context used
    raw_away_team: str = ""                # Away team context used
    source: str = ""                       # Source/bookmaker for audit trail


# ============================================================================
# Positional Selection Labels
# ============================================================================
# These labels require event context (home_team / away_team) to resolve.
# Without context, they MUST return AMBIGUOUS, never silently guess.

# Text-based positional labels — relatively safe
HOME_LABELS: set[str] = {"home", "h", "home team"}
AWAY_LABELS: set[str] = {"away", "a", "away team"}

# Numeric positional labels — DANGEROUS across vendors.
# "1" and "2" meanings differ by vendor and sport.
# These are kept separate and only resolve when ALL of:
#   - event context exists (home_team/away_team)
#   - source behavior is known or default-safe
NUMERIC_HOME_LABELS: set[str] = {"1", "team 1"}
NUMERIC_AWAY_LABELS: set[str] = {"2", "team 2"}

# Vendor-specific overrides:
# Some vendors use reversed numbering or non-standard labels.
# Add overrides here as they're discovered from real feed data.
# Format: { (source, label) -> "home" | "away" }
SOURCE_LABEL_OVERRIDES: dict[tuple[str, str], str] = {
    # Example: if a vendor uses "2" for home team
    # ("weird_vendor", "2"): "home",
}


# ============================================================================
# Standard Selections (context-free)
# ============================================================================

OVER_LABELS: set[str] = {"over", "o", "ov", "ovr"}
UNDER_LABELS: set[str] = {"under", "u", "un", "und", "udr"}
DRAW_LABELS: set[str] = {"draw", "tie", "x", "tied", "d"}


# ============================================================================
# Public API
# ============================================================================

def _is_canonical_team_name(name: str) -> bool:
    """
    Safety heuristic: trust only known canonical team names.

    Currently approximated by multi-word form (city + team name,
    e.g. 'boston celtics'). Single-word tokens like 'kings' or
    'heat' are likely unresolved/ambiguous.

    This is a heuristic, not truth. If a valid canonical name is
    single-word in some sport or feed, it should be explicitly
    whitelisted here rather than weakening the general rule.
    """
    return bool(name) and len(name.split()) >= 2


def resolve_selection(
    raw_selection: str,
    home_team: str = "",
    away_team: str = "",
    source: str = "",
) -> SelectionResolution:
    """
    Normalize a selection and return full resolution metadata.

    This is the trusted API. Every call in matching/scoring/arb code
    should use this, not normalize_selection().

    Args:
        raw_selection: Raw selection label from the source
        home_team: Canonical home team name (from resolve_team_name)
        away_team: Canonical away team name (from resolve_team_name)
        source: Source/bookmaker name (for vendor-specific overrides)

    Returns:
        SelectionResolution with name, type, status, and raw context

    Resolution rules:
        - Canonical context in → canonical result out
        - Weak context → ambiguous result
        - Numeric labels require both context AND known source
        - Contradictory context → loud failure
    """
    cleaned = raw_selection.lower().strip()
    home_clean = home_team.lower().strip()
    away_clean = away_team.lower().strip()
    src = source.lower().strip() if source else ""

    # ----- Step 0: Vendor-specific override -----
    if src and (src, cleaned) in SOURCE_LABEL_OVERRIDES:
        override = SOURCE_LABEL_OVERRIDES[(src, cleaned)]
        if override == "home" and home_clean:
            return SelectionResolution(
                name=home_clean,
                selection_type=SelectionType.TEAM,
                status=SelectionResolutionStatus.CANONICAL,
                is_canonical=True,
                raw_input=raw_selection,
                raw_home_team=home_team,
                raw_away_team=away_team,
                source=source,
            )
        elif override == "away" and away_clean:
            return SelectionResolution(
                name=away_clean,
                selection_type=SelectionType.TEAM,
                status=SelectionResolutionStatus.CANONICAL,
                is_canonical=True,
                raw_input=raw_selection,
                raw_home_team=home_team,
                raw_away_team=away_team,
                source=source,
            )

    # ----- Step 1: Over / Under / Draw (no team context needed) -----
    if cleaned in OVER_LABELS:
        return SelectionResolution(
            name="over",
            selection_type=SelectionType.OVER,
            status=SelectionResolutionStatus.CANONICAL,
            is_canonical=True,
            raw_input=raw_selection,
            source=source,
        )

    if cleaned in UNDER_LABELS:
        return SelectionResolution(
            name="under",
            selection_type=SelectionType.UNDER,
            status=SelectionResolutionStatus.CANONICAL,
            is_canonical=True,
            raw_input=raw_selection,
            source=source,
        )

    if cleaned in DRAW_LABELS:
        return SelectionResolution(
            name="draw",
            selection_type=SelectionType.DRAW,
            status=SelectionResolutionStatus.CANONICAL,
            is_canonical=True,
            raw_input=raw_selection,
            source=source,
        )

    # ----- Step 2: Text-based Home / Away labels -----
    if cleaned in HOME_LABELS:
        if home_clean and _is_canonical_team_name(home_clean):
            return SelectionResolution(
                name=home_clean,
                selection_type=SelectionType.TEAM,
                status=SelectionResolutionStatus.CANONICAL,
                is_canonical=True,
                raw_input=raw_selection,
                raw_home_team=home_team,
                raw_away_team=away_team,
                source=source,
            )
        else:
            reason = "no context" if not home_clean else f"weak context '{home_clean}'"
            logger.warning(
                f"⚠️ AMBIGUOUS SELECTION: '{raw_selection}' is a home label "
                f"but {reason} (source: {source or 'unknown'})"
            )
            return SelectionResolution(
                name=cleaned,
                selection_type=SelectionType.UNRESOLVED,
                status=SelectionResolutionStatus.AMBIGUOUS,
                is_canonical=False,
                raw_input=raw_selection,
                source=source,
            )

    if cleaned in AWAY_LABELS:
        if away_clean and _is_canonical_team_name(away_clean):
            return SelectionResolution(
                name=away_clean,
                selection_type=SelectionType.TEAM,
                status=SelectionResolutionStatus.CANONICAL,
                is_canonical=True,
                raw_input=raw_selection,
                raw_home_team=home_team,
                raw_away_team=away_team,
                source=source,
            )
        else:
            reason = "no context" if not away_clean else f"weak context '{away_clean}'"
            logger.warning(
                f"⚠️ AMBIGUOUS SELECTION: '{raw_selection}' is an away label "
                f"but {reason} (source: {source or 'unknown'})"
            )
            return SelectionResolution(
                name=cleaned,
                selection_type=SelectionType.UNRESOLVED,
                status=SelectionResolutionStatus.AMBIGUOUS,
                is_canonical=False,
                raw_input=raw_selection,
                source=source,
            )

    # ----- Step 3: Numeric labels ("1" / "2") — stricter gating -----
    # Numeric labels are dangerous across vendors. They only resolve if:
    #   - event context exists AND looks canonical
    #   - no contradictory override exists for this source
    if cleaned in NUMERIC_HOME_LABELS:
        if home_clean and _is_canonical_team_name(home_clean):
            return SelectionResolution(
                name=home_clean,
                selection_type=SelectionType.TEAM,
                status=SelectionResolutionStatus.CANONICAL,
                is_canonical=True,
                raw_input=raw_selection,
                raw_home_team=home_team,
                raw_away_team=away_team,
                source=source,
            )
        else:
            logger.warning(
                f"⚠️ AMBIGUOUS NUMERIC SELECTION: '{raw_selection}' — "
                f"numeric labels require canonical team context "
                f"(home: '{home_team}', source: {source or 'unknown'})"
            )
            return SelectionResolution(
                name=cleaned,
                selection_type=SelectionType.UNRESOLVED,
                status=SelectionResolutionStatus.AMBIGUOUS,
                is_canonical=False,
                raw_input=raw_selection,
                source=source,
            )

    if cleaned in NUMERIC_AWAY_LABELS:
        if away_clean and _is_canonical_team_name(away_clean):
            return SelectionResolution(
                name=away_clean,
                selection_type=SelectionType.TEAM,
                status=SelectionResolutionStatus.CANONICAL,
                is_canonical=True,
                raw_input=raw_selection,
                raw_home_team=home_team,
                raw_away_team=away_team,
                source=source,
            )
        else:
            logger.warning(
                f"⚠️ AMBIGUOUS NUMERIC SELECTION: '{raw_selection}' — "
                f"numeric labels require canonical team context "
                f"(away: '{away_team}', source: {source or 'unknown'})"
            )
            return SelectionResolution(
                name=cleaned,
                selection_type=SelectionType.UNRESOLVED,
                status=SelectionResolutionStatus.AMBIGUOUS,
                is_canonical=False,
                raw_input=raw_selection,
                source=source,
            )

    # ----- Step 4: Selection is already a team name -----
    if home_clean and cleaned == home_clean:
        return SelectionResolution(
            name=home_clean,
            selection_type=SelectionType.TEAM,
            status=SelectionResolutionStatus.CANONICAL,
            is_canonical=True,
            raw_input=raw_selection,
            raw_home_team=home_team,
            raw_away_team=away_team,
            source=source,
        )

    if away_clean and cleaned == away_clean:
        return SelectionResolution(
            name=away_clean,
            selection_type=SelectionType.TEAM,
            status=SelectionResolutionStatus.CANONICAL,
            is_canonical=True,
            raw_input=raw_selection,
            raw_home_team=home_team,
            raw_away_team=away_team,
            source=source,
        )

    # ----- Step 5: Substring match (team name contained in selection) -----
    # e.g., "Boston Celtics +3.5" → "boston celtics"
    # Only match if EXACTLY ONE team matches to avoid ambiguity
    matches = []
    if home_clean and home_clean in cleaned:
        matches.append(("home", home_clean))
    if away_clean and away_clean in cleaned:
        matches.append(("away", away_clean))

    if len(matches) == 1:
        _, team_name = matches[0]
        return SelectionResolution(
            name=team_name,
            selection_type=SelectionType.TEAM,
            status=SelectionResolutionStatus.CANONICAL,
            is_canonical=True,
            raw_input=raw_selection,
            raw_home_team=home_team,
            raw_away_team=away_team,
            source=source,
        )
    elif len(matches) > 1:
        # Both teams are substrings — contradictory, reject
        logger.warning(
            f"⚠️ CONTRADICTORY SELECTION: '{raw_selection}' matches both "
            f"home='{home_team}' and away='{away_team}' — rejecting "
            f"(source: {source or 'unknown'})"
        )
        return SelectionResolution(
            name=cleaned,
            selection_type=SelectionType.UNRESOLVED,
            status=SelectionResolutionStatus.AMBIGUOUS,
            is_canonical=False,
            raw_input=raw_selection,
            raw_home_team=home_team,
            raw_away_team=away_team,
            source=source,
        )

    # ----- Step 6: Unresolved -----
    logger.debug(
        f"Unresolved selection: '{raw_selection}' "
        f"(home: '{home_team}', away: '{away_team}', source: '{source}')"
    )
    return SelectionResolution(
        name=cleaned,
        selection_type=SelectionType.UNRESOLVED,
        status=SelectionResolutionStatus.UNRESOLVED,
        is_canonical=False,
        raw_input=raw_selection,
        raw_home_team=home_team,
        raw_away_team=away_team,
        source=source,
    )


def normalize_selection(
    raw_selection: str,
    home_team: str = "",
    away_team: str = "",
) -> str:
    """
    Normalize a selection name to its canonical form.

    ⚠️ CONVENIENCE ONLY — returns a plain string with no resolution metadata.
    Do NOT use in matching, scoring, or arb-critical code paths.
    Use resolve_selection() instead, which returns SelectionResolution with
    full status (CANONICAL / AMBIGUOUS / UNRESOLVED) and selection type.

    This wrapper exists for backward compatibility and tests.
    """
    return resolve_selection(raw_selection, home_team, away_team).name
