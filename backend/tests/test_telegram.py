"""
Tests for Telegram alert formatting and deduplication.
(No real HTTP calls — only tests message formatting and dedup logic.)
"""

import pytest
from datetime import datetime, timezone

from backend.normalization.models import NormalizedOdds, ArbOpportunity, MarketKey, MarketStatus
from backend.alerts.telegram import (
    escape_markdown_v2,
    format_arb_message,
    TelegramAlertService,
)


def make_test_arb() -> ArbOpportunity:
    """Create a test ArbOpportunity."""
    legs = [
        NormalizedOdds(
            event_id="evt1",
            sport_key="basketball_nba",
            event_name="Celtics @ Lakers",
            commence_time=datetime(2025, 1, 15, 2, 0, tzinfo=timezone.utc),
            market_key=MarketKey.H2H,
            selection="Celtics",
            odds=2.15,
            operator="fanduel",
            operator_display="FanDuel",
            timestamp=datetime.now(timezone.utc),
            status=MarketStatus.ACTIVE,
        ),
        NormalizedOdds(
            event_id="evt1",
            sport_key="basketball_nba",
            event_name="Celtics @ Lakers",
            commence_time=datetime(2025, 1, 15, 2, 0, tzinfo=timezone.utc),
            market_key=MarketKey.H2H,
            selection="Lakers",
            odds=2.05,
            operator="draftkings",
            operator_display="DraftKings",
            timestamp=datetime.now(timezone.utc),
            status=MarketStatus.ACTIVE,
        ),
    ]
    return ArbOpportunity(
        event_id="evt1",
        event_name="Celtics @ Lakers",
        sport_key="basketball_nba",
        market_key=MarketKey.H2H,
        legs=legs,
        arb_percent=95.35,
        profit_percent=4.88,
        stakes={"fanduel": 488.10, "draftkings": 511.90},
        total_stake=1000.0,
        guaranteed_payout=1048.80,
    )


class TestEscapeMarkdownV2:
    def test_escapes_special_chars(self):
        assert escape_markdown_v2("Hello!") == "Hello\\!"
        assert escape_markdown_v2("$100.50") == "$100\\.50"
        assert escape_markdown_v2("Team (A)") == "Team \\(A\\)"

    def test_no_escape_needed(self):
        assert escape_markdown_v2("Hello World") == "Hello World"


class TestFormatArbMessage:
    def test_contains_key_info(self):
        arb = make_test_arb()
        msg = format_arb_message(arb)
        assert "ARBITRAGE" in msg
        assert "FanDuel" in msg
        assert "DraftKings" in msg
        assert "4\\.88" in msg  # Escaped profit

    def test_contains_sport_emoji(self):
        arb = make_test_arb()
        msg = format_arb_message(arb)
        assert "🏀" in msg  # NBA emoji

    def test_contains_stakes(self):
        arb = make_test_arb()
        msg = format_arb_message(arb)
        assert "488" in msg
        assert "511" in msg


class TestTelegramDedup:
    def test_first_send_not_duplicate(self):
        service = TelegramAlertService()
        arb = make_test_arb()
        assert not service._is_duplicate(arb)

    def test_marked_as_duplicate_after_send(self):
        service = TelegramAlertService()
        arb = make_test_arb()
        service._mark_sent(arb)
        assert service._is_duplicate(arb)

    def test_different_arbs_not_duplicate(self):
        service = TelegramAlertService()
        arb1 = make_test_arb()
        arb1.event_id = "evt1"
        arb2 = make_test_arb()
        arb2.event_id = "evt2"
        service._mark_sent(arb1)
        assert not service._is_duplicate(arb2)
