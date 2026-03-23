"""
Stake Calculator — equal-payout stake sizing for arbitrage.

Given decimal odds for each leg and a total stake, calculates
how much to wager on each leg so that the guaranteed payout
is the same regardless of which outcome wins.
"""

import logging

logger = logging.getLogger(__name__)


def calculate_stakes(
    odds: list[float],
    total_stake: float = 1000.0,
) -> list[float]:
    """
    Calculate equal-payout stake allocation.

    For N-way arb with decimal odds [o1, o2, ..., oN]:
      implied_sum = 1/o1 + 1/o2 + ... + 1/oN
      payout = total_stake / implied_sum
      stake_i = payout / o_i

    Returns a list of stakes in the same order as the odds input.

    Args:
        odds: List of decimal odds for each leg.
        total_stake: Total amount to distribute across all legs.

    Returns:
        List of stake amounts (rounded to 2 decimal places).
    """
    if not odds or total_stake <= 0:
        return [0.0] * len(odds) if odds else []

    # Validate all odds are positive
    if any(o <= 0 for o in odds):
        logger.error(f"Invalid odds (non-positive value): {odds}")
        return [0.0] * len(odds)

    implied_sum = sum(1.0 / o for o in odds)

    if implied_sum == 0:
        return [0.0] * len(odds)

    # Equal payout = the guaranteed return
    payout = total_stake / implied_sum

    # Each leg's stake = payout / odds for that leg
    stakes = [round(payout / o, 2) for o in odds]

    return stakes


def calculate_profit(
    odds: list[float],
    total_stake: float = 1000.0,
) -> dict:
    """
    Calculate full profit breakdown for an arb opportunity.

    Returns dict with:
      - stakes: per-leg stakes
      - total_stake: actual sum of stakes
      - payout: guaranteed payout
      - profit: guaranteed profit
      - profit_percent: profit as % of total stake
      - arb_percent: sum of implied probabilities * 100
    """
    if not odds or any(o <= 0 for o in odds):
        return {
            "stakes": [],
            "total_stake": 0,
            "payout": 0,
            "profit": 0,
            "profit_percent": 0,
            "arb_percent": 100,
        }

    implied_sum = sum(1.0 / o for o in odds)
    arb_percent = implied_sum * 100

    stakes = calculate_stakes(odds, total_stake)
    actual_total = sum(stakes)
    payout = total_stake / implied_sum if implied_sum > 0 else 0
    profit = payout - actual_total

    return {
        "stakes": stakes,
        "total_stake": round(actual_total, 2),
        "payout": round(payout, 2),
        "profit": round(profit, 2),
        "profit_percent": round((profit / actual_total * 100) if actual_total > 0 else 0, 4),
        "arb_percent": round(arb_percent, 4),
    }
