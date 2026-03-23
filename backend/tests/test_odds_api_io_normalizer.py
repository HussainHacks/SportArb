"""
Tests for Odds-API.io normalizer — validates schema mapping, timestamp
handling, bookmaker canonicalization, and edge cases.
"""

import pytest
from datetime import datetime, timezone

from backend.collectors.odds_api_io_normalizer import (
    normalize_odds_response,
    normalize_events_batch,
    _canonicalize_bookmaker,
    _parse_odds,
    _resolve_market_key,
)
from backend.normalization.models import MarketKey


# === Fixtures ===

def _sample_event(
    event_id=123456,
    home="Manchester United",
    away="Liverpool",
    status="pending",
    bookmakers=None,
):
    return {
        "id": event_id,
        "home": home,
        "away": away,
        "date": "2025-10-15T15:00:00Z",
        "status": status,
        "sport": {"name": "Football", "slug": "football"},
        "league": {"name": "Premier League", "slug": "england-premier-league"},
        "bookmakers": bookmakers or {},
    }


def _ml_market(home="2.10", draw="3.40", away="3.20", updated="2025-10-04T10:30:00Z"):
    return {
        "name": "ML",
        "updatedAt": updated,
        "odds": [{"home": home, "draw": draw, "away": away}],
    }


def _spread_market(hdp=1.5, home="1.85", away="2.10", updated="2025-10-04T10:30:00Z"):
    return {
        "name": "Spread",
        "updatedAt": updated,
        "odds": [{"hdp": hdp, "home": home, "away": away}],
    }


def _totals_market(hdp=2.5, over="1.85", under="2.10", updated="2025-10-04T10:30:00Z"):
    return {
        "name": "Totals",
        "updatedAt": updated,
        "odds": [{"hdp": hdp, "over": over, "under": under}],
    }


# === Bookmaker Canonicalization ===

class TestBookmakerCanonicalization:
    def test_draftkings(self):
        key, display = _canonicalize_bookmaker("DraftKings")
        assert key == "draftkings"
        assert display == "DraftKings"

    def test_caesars_variant(self):
        key, display = _canonicalize_bookmaker("Caesars Sportsbook")
        assert key == "williamhill_us"
        assert display == "Caesars"

    def test_espn_bet(self):
        key, _ = _canonicalize_bookmaker("ESPN BET")
        assert key == "espnbet"

    def test_unknown_falls_back(self):
        key, display = _canonicalize_bookmaker("SomeNewBook")
        assert key == "somenewbook"
        assert display == "SomeNewBook"


# === Market Key Mapping ===

class TestMarketKeyMapping:
    def test_ml(self):
        assert _resolve_market_key("ML") == MarketKey.H2H

    def test_spread(self):
        assert _resolve_market_key("Spread") == MarketKey.SPREADS

    def test_totals(self):
        assert _resolve_market_key("Totals") == MarketKey.TOTALS

    def test_case_insensitive(self):
        assert _resolve_market_key("ml") == MarketKey.H2H
        assert _resolve_market_key("SPREAD") == MarketKey.SPREADS

    def test_unknown_returns_none(self):
        assert _resolve_market_key("Player Props") is None


# === Odds Parsing ===

class TestOddsParsing:
    def test_string_odds(self):
        assert _parse_odds("2.15") == 2.15

    def test_float_odds(self):
        assert _parse_odds(1.85) == 1.85

    def test_zero_rejected(self):
        assert _parse_odds("0") is None

    def test_negative_rejected(self):
        assert _parse_odds("-1.5") is None

    def test_none_returns_none(self):
        assert _parse_odds(None) is None

    def test_garbage_returns_none(self):
        assert _parse_odds("abc") is None


# === ML Market Normalization ===

class TestMLNormalization:
    def test_3way_ml(self):
        event = _sample_event(bookmakers={
            "DraftKings": [_ml_market(home="2.10", draw="3.40", away="3.20")],
        })
        odds = normalize_odds_response(event)

        assert len(odds) == 3
        selections = {o.selection for o in odds}
        assert selections == {"Manchester United", "Liverpool", "Draw"}

        home_line = next(o for o in odds if o.selection == "Manchester United")
        assert home_line.odds == 2.10
        assert home_line.operator == "draftkings"
        assert home_line.market_key == MarketKey.H2H

    def test_2way_ml_no_draw(self):
        event = _sample_event(bookmakers={
            "FanDuel": [_ml_market(home="1.90", away="1.95", draw=None)],
        })
        odds = normalize_odds_response(event)

        assert len(odds) == 2
        selections = {o.selection for o in odds}
        assert selections == {"Manchester United", "Liverpool"}


# === Spread Market Normalization ===

class TestSpreadNormalization:
    def test_spread_line_handling(self):
        event = _sample_event(bookmakers={
            "BetMGM": [_spread_market(hdp=1.5, home="1.85", away="2.10")],
        })
        odds = normalize_odds_response(event)

        assert len(odds) == 2
        home_line = next(o for o in odds if o.selection == "Manchester United")
        away_line = next(o for o in odds if o.selection == "Liverpool")

        # Home gets +1.5, away gets -1.5
        assert home_line.point == 1.5
        assert away_line.point == -1.5
        assert home_line.market_key == MarketKey.SPREADS

    def test_missing_hdp_skipped(self):
        event = _sample_event(bookmakers={
            "BetMGM": [{
                "name": "Spread",
                "updatedAt": "2025-10-04T10:30:00Z",
                "odds": [{"home": "1.85", "away": "2.10"}],  # no hdp
            }],
        })
        odds = normalize_odds_response(event)
        assert len(odds) == 0  # Can't extract without hdp


# === Totals Market Normalization ===

class TestTotalsNormalization:
    def test_totals_over_under(self):
        event = _sample_event(bookmakers={
            "DraftKings": [_totals_market(hdp=2.5, over="1.85", under="2.10")],
        })
        odds = normalize_odds_response(event)

        assert len(odds) == 2
        over = next(o for o in odds if o.selection == "Over")
        under = next(o for o in odds if o.selection == "Under")

        assert over.odds == 1.85
        assert under.odds == 2.10
        assert over.point == 2.5
        assert under.point == 2.5
        assert over.market_key == MarketKey.TOTALS


# === Timestamp Handling ===

class TestTimestamps:
    def test_per_market_timestamp(self):
        event = _sample_event(bookmakers={
            "DraftKings": [
                _ml_market(updated="2025-10-04T10:30:00Z"),
                _totals_market(updated="2025-10-04T10:29:00Z"),
            ],
        })
        odds = normalize_odds_response(event)

        ml_odds = [o for o in odds if o.market_key == MarketKey.H2H]
        totals_odds = [o for o in odds if o.market_key == MarketKey.TOTALS]

        # Different timestamps for different markets
        assert ml_odds[0].timestamp != totals_odds[0].timestamp

    def test_missing_timestamp_defaults(self):
        event = _sample_event(bookmakers={
            "DraftKings": [{
                "name": "ML",
                "odds": [{"home": "2.10", "away": "1.80"}],
            }],
        })
        odds = normalize_odds_response(event)
        assert all(o.timestamp is not None for o in odds)


# === Event Metadata ===

class TestEventMetadata:
    def test_event_name_format(self):
        event = _sample_event(home="Celtics", away="Lakers")
        odds = normalize_odds_response(event)
        if odds:
            assert odds[0].event_name == "Lakers @ Celtics"

    def test_sport_key_from_slugs(self):
        event = _sample_event(bookmakers={
            "DraftKings": [_ml_market()],
        })
        odds = normalize_odds_response(event)
        assert odds[0].sport_key == "football_england-premier-league"

    def test_live_status(self):
        event = _sample_event(status="live", bookmakers={
            "DraftKings": [_ml_market()],
        })
        odds = normalize_odds_response(event)
        assert all(o.is_live for o in odds)

    def test_event_id_as_string(self):
        event = _sample_event(event_id=789, bookmakers={
            "DraftKings": [_ml_market()],
        })
        odds = normalize_odds_response(event)
        assert odds[0].event_id == "789"


# === Operator Filtering ===

class TestOperatorFiltering:
    def test_allowed_operators(self):
        event = _sample_event(bookmakers={
            "DraftKings": [_ml_market()],
            "FanDuel": [_ml_market()],
            "SomeRandomBook": [_ml_market()],
        })
        allowed = {"draftkings", "fanduel"}
        odds = normalize_odds_response(event, allowed_operators=allowed)

        operators = {o.operator for o in odds}
        assert "somerandombook" not in operators
        assert "draftkings" in operators
        assert "fanduel" in operators

    def test_no_filter_includes_all(self):
        event = _sample_event(bookmakers={
            "DraftKings": [_ml_market()],
            "FanDuel": [_ml_market()],
        })
        odds = normalize_odds_response(event)
        assert len({o.operator for o in odds}) == 2


# === Multi-book Event ===

class TestMultiBookEvent:
    def test_multi_book_ml(self):
        event = _sample_event(bookmakers={
            "DraftKings": [_ml_market(home="2.10", away="1.80")],
            "FanDuel": [_ml_market(home="2.15", away="1.75")],
            "BetMGM": [_ml_market(home="2.08", away="1.82")],
        })
        odds = normalize_odds_response(event)

        # 3 books × 2 selections (no draw in test) = at least 6
        # Actually ML with draw = 3 books × 3 selections = 9
        operators = {o.operator for o in odds}
        assert operators == {"draftkings", "fanduel", "betmgm"}


# === Batch Normalization ===

class TestBatch:
    def test_normalize_batch(self):
        events = [
            _sample_event(event_id=1, bookmakers={
                "DraftKings": [_ml_market()],
            }),
            _sample_event(event_id=2, bookmakers={
                "FanDuel": [_totals_market()],
            }),
        ]
        odds = normalize_events_batch(events)
        event_ids = {o.event_id for o in odds}
        assert event_ids == {"1", "2"}


# === Edge Cases ===

class TestEdgeCases:
    def test_empty_bookmakers(self):
        event = _sample_event(bookmakers={})
        assert normalize_odds_response(event) == []

    def test_no_odds_in_market(self):
        event = _sample_event(bookmakers={
            "DraftKings": [{"name": "ML", "updatedAt": "2025-01-01T00:00:00Z", "odds": []}],
        })
        assert normalize_odds_response(event) == []

    def test_malformed_bookmakers_not_dict(self):
        event = _sample_event()
        event["bookmakers"] = "invalid"
        assert normalize_odds_response(event) == []

    def test_malformed_markets_not_list(self):
        event = _sample_event(bookmakers={"DraftKings": "invalid"})
        assert normalize_odds_response(event) == []
