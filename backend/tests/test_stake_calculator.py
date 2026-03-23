"""
Tests for the stake calculator.
"""

import pytest
from backend.engine.stake_calculator import calculate_stakes, calculate_profit


class TestCalculateStakes:
    def test_2way_equal_odds(self):
        """Equal odds → equal stakes."""
        stakes = calculate_stakes([2.0, 2.0], total_stake=1000.0)
        assert len(stakes) == 2
        assert abs(stakes[0] - 500.0) < 0.01
        assert abs(stakes[1] - 500.0) < 0.01

    def test_2way_unequal_odds(self):
        """Higher odds → lower stake (inversely proportional)."""
        stakes = calculate_stakes([2.15, 2.05], total_stake=1000.0)
        assert len(stakes) == 2
        # Higher odds (2.15) should get LESS stake
        assert stakes[0] < stakes[1]
        # Sum should approximately equal total_stake
        assert abs(sum(stakes) - 1000.0) < 1.0

    def test_3way_stakes(self):
        """3-way bet stakes."""
        stakes = calculate_stakes([3.5, 4.0, 3.0], total_stake=1000.0)
        assert len(stakes) == 3
        # Each stake should be positive
        assert all(s > 0 for s in stakes)
        # Sum should approximately equal total_stake
        assert abs(sum(stakes) - 1000.0) < 1.0

    def test_zero_stake_returns_zeros(self):
        stakes = calculate_stakes([2.0, 2.0], total_stake=0.0)
        assert stakes == [0.0, 0.0]

    def test_empty_odds(self):
        stakes = calculate_stakes([], total_stake=1000.0)
        assert stakes == []

    def test_negative_odds_returns_zeros(self):
        stakes = calculate_stakes([2.0, -1.5], total_stake=1000.0)
        assert stakes == [0.0, 0.0]


class TestCalculateProfit:
    def test_arb_has_positive_profit(self):
        """When arb exists, profit should be positive."""
        result = calculate_profit([2.15, 2.05], total_stake=1000.0)
        assert result["profit"] > 0
        assert result["profit_percent"] > 0
        assert result["arb_percent"] < 100

    def test_no_arb_negative_profit(self):
        """Standard vig → negative profit."""
        result = calculate_profit([1.90, 1.90], total_stake=1000.0)
        assert result["profit"] < 0
        assert result["arb_percent"] > 100

    def test_payout_exceeds_stake_for_arb(self):
        result = calculate_profit([2.15, 2.05], total_stake=1000.0)
        assert result["payout"] > result["total_stake"]

    def test_invalid_odds(self):
        result = calculate_profit([0, 2.0], total_stake=1000.0)
        assert result["profit"] == 0
        assert result["arb_percent"] == 100
