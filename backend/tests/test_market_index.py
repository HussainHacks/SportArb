"""
Tests for the incremental market engine (MarketIndex + ActiveArbTracker).

Edge cases tested:
  - Upsert creates new groups
  - Upsert detects changes vs no-ops
  - Operator and event indexes are maintained
  - Stale group eviction
  - Event removal cleans all indexes
  - Active arb tracker deduplication
  - Profit threshold re-alerts
  - recompute_group returns arbs for changed groups only
"""

import pytest
from datetime import datetime, timezone, timedelta

from backend.engine.market_index import MarketIndex, MarketGroup
from backend.engine.active_arb_tracker import ActiveArbTracker
from backend.engine.arb_detector import recompute_group
from backend.normalization.models import NormalizedOdds, MarketKey, MarketStatus, ArbOpportunity


def _make_odds(
    event_id="evt_1",
    event_name="Team A @ Team B",
    sport_key="basketball_nba",
    market_key=MarketKey.H2H,
    selection="Team A",
    odds=2.0,
    operator="draftkings",
    point=None,
    period="full_game",
    timestamp=None,
) -> NormalizedOdds:
    return NormalizedOdds(
        event_id=event_id,
        event_name=event_name,
        sport_key=sport_key,
        market_key=market_key,
        selection=selection,
        odds=odds,
        operator=operator,
        operator_display=operator.title(),
        point=point,
        period=period,
        timestamp=timestamp or datetime.now(timezone.utc),
        commence_time=datetime.now(timezone.utc) + timedelta(hours=1),
        status=MarketStatus.ACTIVE,
    )


class TestMarketIndex:
    def test_upsert_creates_group(self):
        index = MarketIndex()
        odds = [_make_odds(selection="Team A", operator="dk", odds=2.10)]
        changed = index.upsert_odds(odds)

        assert len(changed) == 1
        assert index.market_count == 1

    def test_upsert_same_odds_no_change(self):
        index = MarketIndex()
        odds = [_make_odds(selection="Team A", operator="dk", odds=2.10)]
        index.upsert_odds(odds)

        # Same odds again — no change
        changed = index.upsert_odds(odds)
        assert len(changed) == 0

    def test_upsert_different_odds_detects_change(self):
        index = MarketIndex()
        index.upsert_odds([_make_odds(selection="Team A", operator="dk", odds=2.10)])

        # Different odds — should detect change
        changed = index.upsert_odds([
            _make_odds(selection="Team A", operator="dk", odds=2.15)
        ])
        assert len(changed) == 1

    def test_operator_index_maintained(self):
        index = MarketIndex()
        index.upsert_odds([
            _make_odds(selection="Team A", operator="dk", odds=2.10),
            _make_odds(selection="Team A", operator="fd", odds=2.05),
        ])

        dk_groups = index.get_groups_for_operator("dk")
        fd_groups = index.get_groups_for_operator("fd")
        assert len(dk_groups) == 1
        assert len(fd_groups) == 1

    def test_event_index_maintained(self):
        index = MarketIndex()
        index.upsert_odds([
            _make_odds(event_id="evt_1", selection="Team A"),
            _make_odds(event_id="evt_1", selection="Team B",
                       market_key=MarketKey.SPREADS, point=-3.5),
        ])

        groups = index.get_groups_for_event("evt_1")
        assert len(groups) == 2  # two different market groups

    def test_remove_event_cleans_all_indexes(self):
        index = MarketIndex()
        index.upsert_odds([
            _make_odds(event_id="evt_1", operator="dk"),
            _make_odds(event_id="evt_1", operator="fd"),
        ])

        assert index.market_count == 1
        removed = index.remove_event("evt_1")
        assert removed == 1
        assert index.market_count == 0

    def test_multiple_events_isolated(self):
        index = MarketIndex()
        index.upsert_odds([
            _make_odds(event_id="evt_1", selection="Team A"),
            _make_odds(event_id="evt_2", event_name="Team C @ Team D",
                       selection="Team C"),
        ])

        assert index.market_count == 2
        assert index.event_count == 2

    def test_stats(self):
        index = MarketIndex()
        index.upsert_odds([_make_odds()])
        stats = index.get_stats()
        assert stats["markets"] == 1
        assert stats["total_updates"] == 1
        assert stats["total_changes"] == 1


class TestMarketGroup:
    def test_update_detects_change(self):
        group = MarketGroup(
            key="test", event_id="evt_1", event_name="A @ B",
            sport_key="nba", market_key_str="h2h",
        )
        odds = _make_odds(selection="Team A", operator="dk", odds=2.10)
        assert group.update(odds) is True  # new entry

        # Same odds — no change
        assert group.update(odds) is False

        # Different odds — change
        odds2 = _make_odds(selection="Team A", operator="dk", odds=2.15)
        assert group.update(odds2) is True

    def test_get_all_odds(self):
        group = MarketGroup(
            key="test", event_id="evt_1", event_name="A @ B",
            sport_key="nba", market_key_str="h2h",
        )
        group.update(_make_odds(selection="Team A", operator="dk", odds=2.10))
        group.update(_make_odds(selection="Team B", operator="dk", odds=1.80))
        group.update(_make_odds(selection="Team A", operator="fd", odds=2.05))

        all_odds = group.get_all_odds()
        assert len(all_odds) == 3

    def test_selection_names(self):
        group = MarketGroup(
            key="test", event_id="evt_1", event_name="A @ B",
            sport_key="nba", market_key_str="h2h",
        )
        group.update(_make_odds(selection="Team A", operator="dk"))
        group.update(_make_odds(selection="Team B", operator="dk"))

        assert group.selection_names == {"Team A", "Team B"}

    def test_remove_operator(self):
        group = MarketGroup(
            key="test", event_id="evt_1", event_name="A @ B",
            sport_key="nba", market_key_str="h2h",
        )
        group.update(_make_odds(operator="dk"))
        assert group.remove_operator("dk") is True
        assert group.remove_operator("dk") is False
        assert group.operator_count == 0


class TestActiveArbTracker:
    def _make_arb(self, market_key="mk_1", profit=2.5, event_name="A @ B"):
        return ArbOpportunity(
            event_id="evt_1",
            event_name=event_name,
            sport_key="basketball_nba",
            market_key=MarketKey.H2H,
            legs=[
                _make_odds(selection="Team A", operator="dk", odds=2.50),
                _make_odds(selection="Team B", operator="fd", odds=2.50),
            ],
            arb_percent=95.0,
            profit_percent=profit,
            total_stake=1000.0,
            guaranteed_payout=1050.0,
            market_group_key=market_key,
        )

    def test_new_arb_generates_alert(self):
        tracker = ActiveArbTracker()
        arbs = [self._make_arb()]
        alertable = tracker.process_arbs("mk_1", arbs)

        assert len(alertable) == 1
        assert tracker.active_count == 1
        assert tracker.total_new == 1

    def test_same_arb_suppressed(self):
        tracker = ActiveArbTracker()
        arbs = [self._make_arb(profit=2.5)]
        tracker.process_arbs("mk_1", arbs)

        # Same arb, similar profit — suppress
        alertable = tracker.process_arbs("mk_1", [self._make_arb(profit=2.6)])
        assert len(alertable) == 0
        assert tracker.total_suppressed == 1

    def test_profit_change_triggers_realert(self):
        tracker = ActiveArbTracker(profit_change_threshold=0.5)
        tracker.process_arbs("mk_1", [self._make_arb(profit=2.0)])

        # Big profit change — re-alert
        alertable = tracker.process_arbs("mk_1", [self._make_arb(profit=3.0)])
        assert len(alertable) == 1
        assert tracker.total_updated == 1

    def test_arb_disappears(self):
        tracker = ActiveArbTracker()
        tracker.process_arbs("mk_1", [self._make_arb()])

        # Empty arbs — arb disappeared
        alertable = tracker.process_arbs("mk_1", [])
        assert len(alertable) == 0
        assert tracker.active_count == 0
        assert tracker.total_disappeared == 1

    def test_no_arb_no_effect(self):
        tracker = ActiveArbTracker()
        alertable = tracker.process_arbs("mk_1", [])
        assert len(alertable) == 0
        assert tracker.active_count == 0

    def test_clear_event(self):
        tracker = ActiveArbTracker()
        tracker.process_arbs("mk_1", [self._make_arb()])
        tracker.process_arbs("mk_2", [self._make_arb(market_key="mk_2")])

        removed = tracker.clear_event("evt_1")
        assert removed == 2
        assert tracker.active_count == 0


class TestRecomputeGroup:
    def test_arb_detected_in_group(self):
        """Two books with complementary odds that form an arb."""
        group = MarketGroup(
            key="evt_1:h2h:full_game:none",
            event_id="evt_1",
            event_name="Team A @ Team B",
            sport_key="basketball_nba",
            market_key_str="h2h",
        )
        # DraftKings: Team A @ 2.15, Team B @ 1.85
        group.update(_make_odds(selection="Team A", operator="draftkings", odds=2.15))
        group.update(_make_odds(selection="Team B", operator="draftkings", odds=1.85))
        # FanDuel: Team A @ 1.80, Team B @ 2.20
        group.update(_make_odds(selection="Team A", operator="fanduel", odds=1.80))
        group.update(_make_odds(selection="Team B", operator="fanduel", odds=2.20))

        # Best: DK Team A (2.15) + FD Team B (2.20)
        # 1/2.15 + 1/2.20 = 0.4651 + 0.4545 = 0.9197 → ~8% profit
        arbs = recompute_group(group, min_profit=0.1, max_profit=20.0)
        assert len(arbs) >= 1
        assert arbs[0].profit_percent > 0

    def test_no_arb_in_tight_market(self):
        """Tight market — no arb opportunity."""
        group = MarketGroup(
            key="evt_1:h2h:full_game:none",
            event_id="evt_1",
            event_name="Team A @ Team B",
            sport_key="basketball_nba",
            market_key_str="h2h",
        )
        group.update(_make_odds(selection="Team A", operator="draftkings", odds=1.90))
        group.update(_make_odds(selection="Team B", operator="draftkings", odds=1.90))
        group.update(_make_odds(selection="Team A", operator="fanduel", odds=1.91))
        group.update(_make_odds(selection="Team B", operator="fanduel", odds=1.89))

        arbs = recompute_group(group, min_profit=0.5, max_profit=20.0)
        assert len(arbs) == 0

    def test_empty_group_returns_nothing(self):
        group = MarketGroup(
            key="test", event_id="evt_1", event_name="A @ B",
            sport_key="nba", market_key_str="h2h",
        )
        arbs = recompute_group(group)
        assert arbs == []

class TestMarketIndexDeletions:
    def test_remove_operator_from_event(self):
        index = MarketIndex()
        
        # Add DraftKings ML and Spread for evt_1
        dk_ml = _make_odds(event_id="evt_1", market_key=MarketKey.H2H, operator="draftkings", selection="Team A")
        dk_spread = _make_odds(event_id="evt_1", market_key=MarketKey.SPREADS, operator="draftkings", selection="Team A", point=-3.5)
        
        # Add FanDuel ML for evt_1
        fd_ml = _make_odds(event_id="evt_1", market_key=MarketKey.H2H, operator="fanduel", selection="Team B")
        
        # Add DraftKings ML for evt_2
        dk_evt2 = _make_odds(event_id="evt_2", market_key=MarketKey.H2H, operator="draftkings", selection="Team A")

        index.upsert_odds([dk_ml, dk_spread, fd_ml, dk_evt2])
        
        # Remove DraftKings from evt_1
        changed = index.remove_operator_from_event("draftkings", "evt_1")
        
        # Both DK markets for evt_1 should be changed
        assert len(changed) == 2
        assert any(k.startswith("evt_1:h2h") for k in changed)
        assert any(k.startswith("evt_1:spreads") for k in changed)
        
        # FanDuel ML for evt_1 should still exist
        fd_group = next(g for g in index.get_groups_for_event("evt_1") if g.market_key_str == "h2h")
        assert "fanduel" in fd_group.books
        assert "draftkings" not in fd_group.books
        
        # DraftKings ML for evt_2 should still exist
        evt2_groups = index.get_groups_for_event("evt_2")
        assert len(evt2_groups) == 1
        assert "draftkings" in evt2_groups[0].books
        
        stats = index.get_stats()
        assert stats["total_deletes"] >= 2

