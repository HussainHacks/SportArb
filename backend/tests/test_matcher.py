"""
Tests for the matching engine.

Covers team normalization, event scoring, event resolving,
market matching, and the defensive failure cases from the spec.
"""

import pytest
from datetime import datetime, timezone, timedelta

from backend.matcher.team_normalizer import normalize_team_name, fuzzy_normalize_team
from backend.matcher.league_normalizer import normalize_league
from backend.matcher.market_normalizer import normalize_market, normalize_period
from backend.matcher.selection_normalizer import normalize_selection
from backend.matcher.event_candidate_generator import NormalizedRecord, EventCandidateGenerator
from backend.matcher.event_scorer import EventScorer, AUTO_MATCH_THRESHOLD
from backend.matcher.event_resolver import EventResolver
from backend.matcher.market_matcher import MarketMatcher
from backend.matcher.deduper import Deduper


# =====================================================================
# Team Normalizer Tests
# =====================================================================

class TestTeamNormalizer:
    def test_full_name(self):
        assert normalize_team_name("Los Angeles Lakers") == "los angeles lakers"

    def test_abbreviation(self):
        assert normalize_team_name("LA Lakers") == "los angeles lakers"

    def test_nickname_only(self):
        assert normalize_team_name("Lakers") == "los angeles lakers"

    def test_celtics(self):
        assert normalize_team_name("Celtics") == "boston celtics"
        assert normalize_team_name("Boston Celtics") == "boston celtics"

    def test_case_insensitive(self):
        assert normalize_team_name("BOSTON CELTICS") == "boston celtics"

    def test_nfl_team(self):
        assert normalize_team_name("KC Chiefs") == "kansas city chiefs"
        assert normalize_team_name("Chiefs") == "kansas city chiefs"

    def test_unknown_team_returned_cleaned(self):
        result = normalize_team_name("Some Unknown Team")
        assert result == "some unknown team"

    # --- Defensive failure cases from spec ---
    def test_clippers_vs_lakers_not_confused(self):
        """Case 1: LA Clippers must not be confused with LA Lakers."""
        assert normalize_team_name("LA Clippers") == "los angeles clippers"
        assert normalize_team_name("LA Lakers") == "los angeles lakers"
        assert normalize_team_name("LA Clippers") != normalize_team_name("LA Lakers")


class TestFuzzyNormalize:
    def test_fuzzy_match(self):
        result = fuzzy_normalize_team("Los Angls Lakers")  # Typo
        assert result == "los angeles lakers"


# =====================================================================
# League Normalizer Tests
# =====================================================================

class TestLeagueNormalizer:
    def test_standard(self):
        assert normalize_league("NBA") == "nba"
        assert normalize_league("NFL") == "nfl"

    def test_full_name(self):
        assert normalize_league("National Basketball Association") == "nba"

    def test_odds_api_key(self):
        assert normalize_league("basketball_nba") == "nba"
        assert normalize_league("americanfootball_nfl") == "nfl"

    def test_soccer(self):
        assert normalize_league("Premier League") == "epl"
        assert normalize_league("Champions League") == "ucl"


# =====================================================================
# Market & Period Normalizer Tests
# =====================================================================

class TestMarketNormalizer:
    def test_h2h_to_moneyline(self):
        assert normalize_market("h2h") == "moneyline"

    def test_spreads(self):
        assert normalize_market("spreads") == "spread"
        assert normalize_market("handicap") == "spread"

    def test_totals(self):
        assert normalize_market("totals") == "total"
        assert normalize_market("over/under") == "total"


class TestPeriodNormalizer:
    def test_full_game(self):
        assert normalize_period("full game") == "full_game"
        assert normalize_period("game") == "full_game"
        assert normalize_period("") == "full_game"

    def test_halves(self):
        assert normalize_period("1st half") == "first_half"
        assert normalize_period("1h") == "first_half"

    def test_quarters(self):
        assert normalize_period("1q") == "first_quarter"
        assert normalize_period("4th quarter") == "fourth_quarter"


# =====================================================================
# Selection Normalizer Tests
# =====================================================================

class TestSelectionNormalizer:
    def test_home_maps_to_team(self):
        result = normalize_selection("Home", home_team="Boston Celtics")
        assert result == "boston celtics"

    def test_away_maps_to_team(self):
        result = normalize_selection("Away", away_team="Los Angeles Lakers")
        assert result == "los angeles lakers"

    def test_over_under(self):
        assert normalize_selection("Over") == "over"
        assert normalize_selection("Under") == "under"

    def test_draw(self):
        assert normalize_selection("Draw") == "draw"
        assert normalize_selection("X") == "draw"


# =====================================================================
# Event Scorer Tests
# =====================================================================

def _make_record(
    home="boston celtics", away="los angeles lakers",
    league="nba", source="fanduel", event_id="abc",
    start_time=None, is_live=False,
) -> NormalizedRecord:
    return NormalizedRecord(
        source=source,
        source_event_id=event_id,
        league=league,
        sport="basketball",
        start_time_utc=start_time or datetime(2026, 3, 10, 0, 30, tzinfo=timezone.utc),
        home_team=home,
        away_team=away,
        market_type="moneyline",
        period="full_game",
        selection="Boston Celtics",
        line_value=None,
        odds_decimal=2.15,
        is_live=is_live,
    )


def _make_event(
    home="boston celtics", away="los angeles lakers",
    league="nba", start_time=None, is_live=False,
) -> dict:
    st = start_time or datetime(2026, 3, 10, 0, 30, tzinfo=timezone.utc)
    return {
        "canonical_event_id": f"{league}_{home}_{away}",
        "league": league,
        "start_time_utc": st,
        "home_team": home,
        "away_team": away,
        "participant_set": [home, away],
        "is_live": is_live,
        "source_events": [],
    }


class TestEventScorer:
    def test_perfect_match(self):
        record = _make_record()
        event = _make_event()
        scorer = EventScorer()
        score = scorer.score(record, event)
        assert score.total >= AUTO_MATCH_THRESHOLD
        assert score.is_auto_match

    def test_reversed_home_away(self):
        """Home/away reversed but same teams — should still match."""
        record = _make_record(home="los angeles lakers", away="boston celtics")
        event = _make_event(home="boston celtics", away="los angeles lakers")
        scorer = EventScorer()
        score = scorer.score(record, event)
        assert score.total >= AUTO_MATCH_THRESHOLD

    def test_different_league_low_score(self):
        record = _make_record(league="nba")
        event = _make_event(league="ncaab")
        scorer = EventScorer()
        score = scorer.score(record, event)
        assert score.league_score == 0

    def test_time_5_min(self):
        t1 = datetime(2026, 3, 10, 0, 30, tzinfo=timezone.utc)
        t2 = t1 + timedelta(minutes=3)
        record = _make_record(start_time=t1)
        event = _make_event(start_time=t2)
        scorer = EventScorer()
        score = scorer.score(record, event)
        assert score.time_score == 20

    def test_time_too_far(self):
        t1 = datetime(2026, 3, 10, 0, 30, tzinfo=timezone.utc)
        t2 = t1 + timedelta(hours=5)
        record = _make_record(start_time=t1)
        event = _make_event(start_time=t2)
        scorer = EventScorer()
        score = scorer.score(record, event)
        assert score.time_score == 0

    # --- Defensive case: back-to-back games ---
    def test_back_to_back_games_separated(self):
        """Case 5: Same teams on different dates must not match."""
        day1 = datetime(2026, 3, 10, 0, 30, tzinfo=timezone.utc)
        day2 = datetime(2026, 3, 11, 0, 30, tzinfo=timezone.utc)
        record = _make_record(start_time=day1)
        event = _make_event(start_time=day2)
        scorer = EventScorer()
        score = scorer.score(record, event)
        # Time score is 0 (24 hours apart), so total should be below threshold
        assert score.time_score == 0


# =====================================================================
# Event Resolver Tests
# =====================================================================

class TestEventResolver:
    def test_creates_new_event(self):
        resolver = EventResolver()
        record = _make_record()
        event = resolver.resolve(record)
        assert event["league"] == "nba"
        assert len(resolver.events) == 1

    def test_matches_existing_event(self):
        resolver = EventResolver()
        r1 = _make_record(source="fanduel", event_id="fd1")
        r2 = _make_record(source="draftkings", event_id="dk1")
        e1 = resolver.resolve(r1)
        e2 = resolver.resolve(r2)
        assert e1["canonical_event_id"] == e2["canonical_event_id"]
        assert len(resolver.events) == 1  # Same event, not duplicated

    def test_different_events_stay_separate(self):
        resolver = EventResolver()
        r1 = _make_record(home="boston celtics", away="los angeles lakers")
        r2 = _make_record(home="golden state warriors", away="brooklyn nets")
        resolver.resolve(r1)
        resolver.resolve(r2)
        assert len(resolver.events) == 2


# =====================================================================
# Market Matcher Tests
# =====================================================================

class TestMarketMatcher:
    def test_groups_same_market(self):
        matcher = MarketMatcher()
        r1 = NormalizedRecord(
            source="fanduel", source_event_id="a", league="nba", sport="basketball",
            start_time_utc=datetime(2026, 3, 10, tzinfo=timezone.utc),
            home_team="Celtics", away_team="Lakers",
            market_type="moneyline", period="full_game",
            selection="celtics", line_value=None, odds_decimal=2.15,
        )
        r2 = NormalizedRecord(
            source="draftkings", source_event_id="b", league="nba", sport="basketball",
            start_time_utc=datetime(2026, 3, 10, tzinfo=timezone.utc),
            home_team="Celtics", away_team="Lakers",
            market_type="moneyline", period="full_game",
            selection="lakers", line_value=None, odds_decimal=2.05,
        )
        matcher.add_record("evt1", r1)
        matcher.add_record("evt1", r2)
        groups = matcher.get_actionable_groups()
        assert len(groups) == 1

    def test_different_periods_separated(self):
        """Case 4: full game vs first half must not group together."""
        matcher = MarketMatcher()
        r1 = NormalizedRecord(
            source="fanduel", source_event_id="a", league="nba", sport="basketball",
            start_time_utc=datetime(2026, 3, 10, tzinfo=timezone.utc),
            home_team="Celtics", away_team="Lakers",
            market_type="total", period="full_game",
            selection="over", line_value=220.5, odds_decimal=1.95,
        )
        r2 = NormalizedRecord(
            source="draftkings", source_event_id="b", league="nba", sport="basketball",
            start_time_utc=datetime(2026, 3, 10, tzinfo=timezone.utc),
            home_team="Celtics", away_team="Lakers",
            market_type="total", period="first_half",
            selection="under", line_value=110.5, odds_decimal=1.85,
        )
        matcher.add_record("evt1", r1)
        matcher.add_record("evt1", r2)
        groups = matcher.get_all_market_groups()
        assert len(groups) == 2  # Different periods = different groups

    def test_validate_complementary_spread(self):
        matcher = MarketMatcher()
        r1 = NormalizedRecord(
            source="fanduel", source_event_id="a", league="nba", sport="basketball",
            start_time_utc=datetime(2026, 3, 10, tzinfo=timezone.utc),
            home_team="Celtics", away_team="Lakers",
            market_type="spread", period="full_game",
            selection="celtics", line_value=-3.5, odds_decimal=1.95,
        )
        r2 = NormalizedRecord(
            source="draftkings", source_event_id="b", league="nba", sport="basketball",
            start_time_utc=datetime(2026, 3, 10, tzinfo=timezone.utc),
            home_team="Celtics", away_team="Lakers",
            market_type="spread", period="full_game",
            selection="lakers", line_value=3.5, odds_decimal=1.95,
        )
        assert matcher.validate_complementary([r1, r2]) is True

    def test_reject_mismatched_spread(self):
        matcher = MarketMatcher()
        r1 = NormalizedRecord(
            source="fanduel", source_event_id="a", league="nba", sport="basketball",
            start_time_utc=datetime(2026, 3, 10, tzinfo=timezone.utc),
            home_team="Celtics", away_team="Lakers",
            market_type="spread", period="full_game",
            selection="celtics", line_value=-3.5, odds_decimal=1.95,
        )
        r2 = NormalizedRecord(
            source="draftkings", source_event_id="b", league="nba", sport="basketball",
            start_time_utc=datetime(2026, 3, 10, tzinfo=timezone.utc),
            home_team="Celtics", away_team="Lakers",
            market_type="spread", period="full_game",
            selection="lakers", line_value=4.0, odds_decimal=1.95,
        )
        assert matcher.validate_complementary([r1, r2]) is False


# =====================================================================
# Deduper Tests
# =====================================================================

class TestDeduper:
    def test_first_record_passes(self):
        deduper = Deduper()
        r = _make_record()
        assert deduper.process(r) is not None

    def test_duplicate_discarded(self):
        deduper = Deduper()
        fixed_time = datetime(2026, 3, 9, 23, 0, tzinfo=timezone.utc)
        r1 = NormalizedRecord(
            source="fanduel", source_event_id="a", league="nba", sport="basketball",
            start_time_utc=datetime(2026, 3, 10, tzinfo=timezone.utc),
            home_team="Celtics", away_team="Lakers",
            market_type="moneyline", period="full_game",
            selection="celtics", line_value=None, odds_decimal=2.10,
            timestamp_utc=fixed_time,
        )
        r2 = NormalizedRecord(
            source="fanduel", source_event_id="a", league="nba", sport="basketball",
            start_time_utc=datetime(2026, 3, 10, tzinfo=timezone.utc),
            home_team="Celtics", away_team="Lakers",
            market_type="moneyline", period="full_game",
            selection="celtics", line_value=None, odds_decimal=2.10,
            timestamp_utc=fixed_time,  # Same timestamp = true duplicate
        )
        deduper.process(r1)
        assert deduper.process(r2) is None

    def test_newer_replaces_older(self):
        deduper = Deduper()
        t1 = datetime(2026, 3, 9, 23, 0, tzinfo=timezone.utc)
        t2 = datetime(2026, 3, 9, 23, 5, tzinfo=timezone.utc)
        r1 = NormalizedRecord(
            source="fanduel", source_event_id="a", league="nba", sport="basketball",
            start_time_utc=datetime(2026, 3, 10, tzinfo=timezone.utc),
            home_team="Celtics", away_team="Lakers",
            market_type="moneyline", period="full_game",
            selection="celtics", line_value=None, odds_decimal=2.10,
            timestamp_utc=t1,
        )
        r2 = NormalizedRecord(
            source="fanduel", source_event_id="a", league="nba", sport="basketball",
            start_time_utc=datetime(2026, 3, 10, tzinfo=timezone.utc),
            home_team="Celtics", away_team="Lakers",
            market_type="moneyline", period="full_game",
            selection="celtics", line_value=None, odds_decimal=2.15,
            timestamp_utc=t2,
        )
        deduper.process(r1)
        result = deduper.process(r2)
        assert result is not None
        assert result.odds_decimal == 2.15
