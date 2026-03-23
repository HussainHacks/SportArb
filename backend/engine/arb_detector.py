"""
Arbitrage Detection Engine — Technical Specification Implementation

Pipeline:
  Step 1 — Market data is pre-grouped by event + market + period + line (event_matcher)
  Step 2 — Select best odds per outcome from distinct operators
  Step 3 — Validate market consistency (complementary selections)
  Step 4 — Check implied probability sum < 1
  Step 5 — Compute equal-payout stakes
  Step 6 — Return ArbOpportunity (risk filters run separately)
"""

import logging
from collections import defaultdict

from backend.normalization.models import NormalizedOdds, ArbOpportunity, MarketKey
from backend.engine.stake_calculator import calculate_stakes

logger = logging.getLogger(__name__)


def implied_probability(decimal_odds: float) -> float:
    """Convert decimal odds to implied probability."""
    if decimal_odds <= 0:
        return 1.0
    return 1.0 / decimal_odds


# ---------------------------------------------------------------------------
# Step 2: Select Best Odds Per Outcome (Top-N for fallback resilience)
# ---------------------------------------------------------------------------

def _get_top_n_odds_by_selection(
    odds_list: list[NormalizedOdds],
    top_n: int = 3,
) -> dict[str, list[NormalizedOdds]]:
    """
    For each distinct selection, keep the top-N highest decimal odds
    from DISTINCT operators, sorted best-first.

    Professional scanners keep multiple candidates so that if the
    best operator suspends, there is a fallback.

    Returns: { selection_name: [best, 2nd_best, ...] }
    """
    candidates: dict[str, dict[str, NormalizedOdds]] = defaultdict(dict)
    for odds in odds_list:
        key = odds.selection
        op = odds.operator
        # Keep only the best price per operator within each selection
        if op not in candidates[key] or odds.odds > candidates[key][op].odds:
            candidates[key][op] = odds

    result: dict[str, list[NormalizedOdds]] = {}
    for selection, op_map in candidates.items():
        sorted_odds = sorted(op_map.values(), key=lambda o: o.odds, reverse=True)
        result[selection] = sorted_odds[:top_n]

    return result


def _get_best_odds_by_selection(
    odds_list: list[NormalizedOdds],
) -> dict[str, NormalizedOdds]:
    """Backward-compatible wrapper: returns single best per selection."""
    top_n = _get_top_n_odds_by_selection(odds_list, top_n=1)
    return {sel: odds_list[0] for sel, odds_list in top_n.items() if odds_list}


# ---------------------------------------------------------------------------
# Step 3: Validate Market Consistency
# ---------------------------------------------------------------------------

def _are_complementary_selections(
    sel_a: NormalizedOdds,
    sel_b: NormalizedOdds,
    market_key: MarketKey,
) -> bool:
    """
    Verify that two selections form a valid complementary pair.

    Moneyline: same event, same market, same period → always valid
    Spreads:   point_A must equal -point_B (e.g., +3.5 vs -3.5)
    Totals:    same total value, selections are Over vs Under
    """
    if market_key == MarketKey.H2H:
        # Moneyline: any two distinct selections are complementary
        return sel_a.selection != sel_b.selection

    if market_key == MarketKey.SPREADS:
        if sel_a.point is None or sel_b.point is None:
            return False
        # Points must be equal in magnitude and opposite in sign
        return abs(sel_a.point + sel_b.point) < 0.01

    if market_key == MarketKey.TOTALS:
        if sel_a.point is None or sel_b.point is None:
            return False
        # Same total value, one Over and one Under
        selections = {sel_a.selection.lower(), sel_b.selection.lower()}
        return (
            abs(sel_a.point - sel_b.point) < 0.01
            and "over" in selections
            and "under" in selections
        )

    return False


def _are_complementary_3way(
    sel_a: NormalizedOdds,
    sel_b: NormalizedOdds,
    sel_c: NormalizedOdds,
) -> bool:
    """
    Verify three selections form a valid 3-way market (soccer 1X2).
    All three must have distinct selections.
    """
    names = {sel_a.selection, sel_b.selection, sel_c.selection}
    return len(names) == 3


# ---------------------------------------------------------------------------
# Step 4-5: Detect Arb + Compute Stakes
# ---------------------------------------------------------------------------

def find_2way_arbs(
    odds_list: list[NormalizedOdds],
    min_profit: float = 0.5,
    max_profit: float = 20.0,
    default_stake: float = 1000.0,
    market_group_key: str = "",
) -> list[ArbOpportunity]:
    """
    Detect 2-way arbitrage opportunities.

    1. Group by selection → find top-N odds from distinct operators
    2. Validate complementary selections
    3. Check arb_sum < 1
    4. Compute equal-payout stakes
    """
    arbs = []
    top_n_by_selection = _get_top_n_odds_by_selection(odds_list)
    selections = list(top_n_by_selection.keys())

    if len(selections) < 2:
        return arbs

    # Determine market key once from the data
    market_key = odds_list[0].market_key

    # Rejection counters for diagnostics
    same_operator = 0
    below_threshold = 0
    above_threshold = 0
    found = 0

    # Try all pairs across top-N candidates per selection
    for i in range(len(selections)):
        for j in range(i + 1, len(selections)):
            best_pair_arb = None
            any_same_operator = False

            for best_a in top_n_by_selection[selections[i]]:
                for best_b in top_n_by_selection[selections[j]]:
                    # Must be from distinct operators
                    if best_a.operator == best_b.operator:
                        any_same_operator = True
                        continue

                    # Step 3: Validate complementary selections
                    if not _are_complementary_selections(best_a, best_b, market_key):
                        continue

                    # Step 4: Check implied probability sum
                    arb_sum = implied_probability(best_a.odds) + implied_probability(best_b.odds)
                    arb_percent = arb_sum * 100
                    profit_percent = round((1 - arb_sum) * 100, 4)

                    if profit_percent < min_profit:
                        below_threshold += 1
                        continue
                    if profit_percent > max_profit:
                        above_threshold += 1
                        continue

                    # Step 5: Compute equal-payout stakes
                    stakes = calculate_stakes([best_a.odds, best_b.odds], default_stake)
                    payout = default_stake / arb_sum

                    arb = ArbOpportunity(
                        event_id=best_a.event_id,
                        event_name=best_a.event_name,
                        sport_key=best_a.sport_key,
                        market_key=best_a.market_key,
                        legs=[best_a, best_b],
                        arb_percent=round(arb_percent, 4),
                        profit_percent=profit_percent,
                        stakes={best_a.operator: stakes[0], best_b.operator: stakes[1]},
                        total_stake=default_stake,
                        guaranteed_payout=round(payout, 2),
                        market_group_key=market_group_key,
                    )
                    
                    if best_pair_arb is None or arb.profit_percent > best_pair_arb.profit_percent:
                        best_pair_arb = arb

            if best_pair_arb:
                arbs.append(best_pair_arb)
                found += 1
                logger.info(
                    f"🚨 2-WAY ARB: {best_pair_arb.legs[0].event_name} | {best_pair_arb.market_key.value}:{best_pair_arb.legs[0].period} | "
                    f"{best_pair_arb.legs[0].operator_display} {best_pair_arb.legs[0].selection} @ {best_pair_arb.legs[0].odds} vs "
                    f"{best_pair_arb.legs[1].operator_display} {best_pair_arb.legs[1].selection} @ {best_pair_arb.legs[1].odds} | "
                    f"Profit: {best_pair_arb.profit_percent:.2f}%"
                )
            elif any_same_operator:
                same_operator += 1

    # Log rejection summary for threshold tuning
    rejected = same_operator + below_threshold + above_threshold
    if rejected > 0:
        logger.info(
            f"📉 2-WAY scan: {found} arbs found, "
            f"{below_threshold} below min ({min_profit}%), "
            f"{above_threshold} above max ({max_profit}%), "
            f"{same_operator} same-operator"
        )

    return arbs


def find_3way_arbs(
    odds_list: list[NormalizedOdds],
    min_profit: float = 0.5,
    max_profit: float = 20.0,
    default_stake: float = 1000.0,
    market_group_key: str = "",
) -> list[ArbOpportunity]:
    """
    Detect 3-way arbitrage opportunities (e.g., soccer 1X2).

    Same pipeline: top-N odds → complementary validation → arb check → stakes.
    """
    arbs = []
    top_n_by_selection = _get_top_n_odds_by_selection(odds_list)
    selections = list(top_n_by_selection.keys())

    if len(selections) < 3:
        return arbs

    # Rejection counters
    same_operator = 0
    below_threshold = 0
    above_threshold = 0
    found = 0

    for i in range(len(selections)):
        for j in range(i + 1, len(selections)):
            for k in range(j + 1, len(selections)):
                best_triplet_arb = None
                any_same_operator = False

                for best_a in top_n_by_selection[selections[i]]:
                    for best_b in top_n_by_selection[selections[j]]:
                        for best_c in top_n_by_selection[selections[k]]:
                            # At least 2 distinct operators
                            if len({best_a.operator, best_b.operator, best_c.operator}) < 2:
                                any_same_operator = True
                                continue

                            # Validate complementary selections
                            if not _are_complementary_3way(best_a, best_b, best_c):
                                continue

                            arb_sum = (
                                implied_probability(best_a.odds)
                                + implied_probability(best_b.odds)
                                + implied_probability(best_c.odds)
                            )
                            arb_percent = arb_sum * 100
                            profit_percent = round((1 - arb_sum) * 100, 4)

                            if profit_percent < min_profit:
                                below_threshold += 1
                                continue
                            if profit_percent > max_profit:
                                above_threshold += 1
                                continue

                            stakes = calculate_stakes(
                                [best_a.odds, best_b.odds, best_c.odds], default_stake
                            )
                            payout = default_stake / arb_sum

                            arb = ArbOpportunity(
                                event_id=best_a.event_id,
                                event_name=best_a.event_name,
                                sport_key=best_a.sport_key,
                                market_key=best_a.market_key,
                                legs=[best_a, best_b, best_c],
                                arb_percent=round(arb_percent, 4),
                                profit_percent=profit_percent,
                                stakes={
                                    best_a.operator: stakes[0],
                                    best_b.operator: stakes[1],
                                    best_c.operator: stakes[2],
                                },
                                total_stake=default_stake,
                                guaranteed_payout=round(payout, 2),
                                market_group_key=market_group_key,
                            )
                            
                            if best_triplet_arb is None or arb.profit_percent > best_triplet_arb.profit_percent:
                                best_triplet_arb = arb

                if best_triplet_arb:
                    arbs.append(best_triplet_arb)
                    found += 1
                    logger.info(
                        f"🚨 3-WAY ARB: {best_triplet_arb.legs[0].event_name} | {best_triplet_arb.market_key.value}:{best_triplet_arb.legs[0].period} | "
                        f"Profit: {best_triplet_arb.profit_percent:.2f}%"
                    )
                elif any_same_operator:
                    same_operator += 1

    # Log rejection summary
    rejected = same_operator + below_threshold + above_threshold
    if rejected > 0:
        logger.info(
            f"📉 3-WAY scan: {found} arbs found, "
            f"{below_threshold} below min ({min_profit}%), "
            f"{above_threshold} above max ({max_profit}%), "
            f"{same_operator} same-operator"
        )

    return arbs


# ---------------------------------------------------------------------------
# Main Entry Point
# ---------------------------------------------------------------------------

def detect_arbs(
    event_odds_by_market: dict[str, list[NormalizedOdds]],
    event_name: str = "",
    min_profit: float = 0.5,
    max_profit: float = 20.0,
    default_stake: float = 1000.0,
) -> list[ArbOpportunity]:
    """
    Detect all arbs for one event across all its market groups.

    Each market group key is already scoped to market+period+line
    by the event_matcher, so we only compare apples-to-apples.
    """
    all_arbs = []

    for market_group_key, odds_list in event_odds_by_market.items():
        if not odds_list:
            continue

        selections = {o.selection for o in odds_list}

        if len(selections) == 2:
            arbs = find_2way_arbs(odds_list, min_profit, max_profit, default_stake, market_group_key)
        elif len(selections) >= 3:
            arbs = find_3way_arbs(odds_list, min_profit, max_profit, default_stake, market_group_key)
        else:
            continue

        all_arbs.extend(arbs)

    return all_arbs


def recompute_group(
    market_group,
    min_profit: float = 0.5,
    max_profit: float = 20.0,
    default_stake: float = 1000.0,
) -> list[ArbOpportunity]:
    """
    Incremental arb detection for a single MarketGroup.

    This is the fast path: called when a market group's odds change,
    instead of scanning all markets. Extracts the current odds from
    the group and runs the appropriate 2-way or 3-way detector.

    Args:
        market_group: A MarketGroup from the market index.

    Returns:
        List of arb opportunities found (usually 0 or 1).
    """
    odds_list = market_group.get_all_odds()
    if not odds_list:
        return []

    selections = {o.selection for o in odds_list}

    if len(selections) == 2:
        return find_2way_arbs(
            odds_list, min_profit, max_profit, default_stake,
            market_group_key=market_group.key,
        )
    elif len(selections) >= 3:
        return find_3way_arbs(
            odds_list, min_profit, max_profit, default_stake,
            market_group_key=market_group.key,
        )

    return []
