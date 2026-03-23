"""
Tests for the normalizer.
"""

import pytest
from datetime import datetime

from backend.normalization.normalizer import american_to_decimal, normalize_event, normalize_all


class TestAmericanToDecimal:
    def test_positive_american(self):
        # +150 → 2.50
        assert abs(american_to_decimal(150) - 2.50) < 0.01

    def test_negative_american(self):
        # -200 → 1.50
        assert abs(american_to_decimal(-200) - 1.50) < 0.01

    def test_even_money(self):
        # +100 → 2.00
        assert abs(american_to_decimal(100) - 2.00) < 0.01

    def test_heavy_favorite(self):
        # -500 → 1.20
        assert abs(american_to_decimal(-500) - 1.20) < 0.01

    def test_zero_returns_one(self):
        assert american_to_decimal(0) == 1.0


class TestNormalizeEvent:
    def test_basic_event(self):
        raw = {
            "id": "test123",
            "sport_key": "basketball_nba",
            "home_team": "Lakers",
            "away_team": "Celtics",
            "commence_time": "2025-01-15T02:00:00Z",
            "bookmakers": [
                {
                    "key": "draftkings",
                    "title": "DraftKings",
                    "last_update": "2025-01-14T23:00:00Z",
                    "markets": [
                        {
                            "key": "h2h",
                            "outcomes": [
                                {"name": "Lakers", "price": 2.10},
                                {"name": "Celtics", "price": 1.80},
                            ],
                        }
                    ],
                }
            ],
        }
        odds = normalize_event(raw)
        assert len(odds) == 2
        assert odds[0].event_name == "Celtics @ Lakers"
        assert odds[0].operator == "draftkings"
        assert odds[0].odds == 2.10 or odds[0].odds == 1.80

    def test_filters_non_iowa_operators(self):
        raw = {
            "id": "test456",
            "sport_key": "basketball_nba",
            "home_team": "Lakers",
            "away_team": "Celtics",
            "commence_time": "2025-01-15T02:00:00Z",
            "bookmakers": [
                {
                    "key": "betfair",  # Not Iowa-legal
                    "title": "Betfair",
                    "last_update": "2025-01-14T23:00:00Z",
                    "markets": [
                        {
                            "key": "h2h",
                            "outcomes": [
                                {"name": "Lakers", "price": 2.20},
                                {"name": "Celtics", "price": 1.75},
                            ],
                        }
                    ],
                }
            ],
        }
        odds = normalize_event(raw)
        assert len(odds) == 0  # Betfair filtered out

    def test_multiple_markets(self):
        raw = {
            "id": "test789",
            "sport_key": "basketball_nba",
            "home_team": "Lakers",
            "away_team": "Celtics",
            "commence_time": "2025-01-15T02:00:00Z",
            "bookmakers": [
                {
                    "key": "fanduel",
                    "title": "FanDuel",
                    "last_update": "2025-01-14T23:00:00Z",
                    "markets": [
                        {
                            "key": "h2h",
                            "outcomes": [
                                {"name": "Lakers", "price": 2.10},
                                {"name": "Celtics", "price": 1.80},
                            ],
                        },
                        {
                            "key": "totals",
                            "outcomes": [
                                {"name": "Over", "price": 1.95, "point": 220.5},
                                {"name": "Under", "price": 1.85, "point": 220.5},
                            ],
                        },
                    ],
                }
            ],
        }
        odds = normalize_event(raw)
        assert len(odds) == 4  # 2 h2h + 2 totals

    def test_empty_bookmakers(self):
        raw = {
            "id": "test_empty",
            "sport_key": "basketball_nba",
            "home_team": "Lakers",
            "away_team": "Celtics",
            "commence_time": "2025-01-15T02:00:00Z",
            "bookmakers": [],
        }
        odds = normalize_event(raw)
        assert len(odds) == 0


class TestNormalizeAll:
    def test_multiple_events(self):
        events = [
            {
                "id": "e1",
                "sport_key": "basketball_nba",
                "home_team": "Lakers",
                "away_team": "Celtics",
                "commence_time": "2025-01-15T02:00:00Z",
                "bookmakers": [
                    {
                        "key": "draftkings",
                        "title": "DraftKings",
                        "last_update": "2025-01-14T23:00:00Z",
                        "markets": [{"key": "h2h", "outcomes": [{"name": "Lakers", "price": 2.0}, {"name": "Celtics", "price": 1.9}]}],
                    }
                ],
            },
            {
                "id": "e2",
                "sport_key": "basketball_nba",
                "home_team": "Warriors",
                "away_team": "Nets",
                "commence_time": "2025-01-15T03:00:00Z",
                "bookmakers": [
                    {
                        "key": "fanduel",
                        "title": "FanDuel",
                        "last_update": "2025-01-14T23:00:00Z",
                        "markets": [{"key": "h2h", "outcomes": [{"name": "Warriors", "price": 1.5}, {"name": "Nets", "price": 2.8}]}],
                    }
                ],
            },
        ]
        all_odds = normalize_all(events)
        assert len(all_odds) == 4
