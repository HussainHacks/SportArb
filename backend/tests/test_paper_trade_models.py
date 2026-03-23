import pytest
from datetime import datetime, timezone
from backend.paper_trading.paper_trade_models import (
    PaperTrade,
    PaperTradeStatus,
    OddsSnapshot,
)

def test_paper_trade_survival_properties():
    trade = PaperTrade()
    
    # Initially alive
    assert trade.status == PaperTradeStatus.ALERTED
    assert not trade.is_terminal
    
    # Mark valid at 5s
    trade.mark_valid(5)
    assert trade.survived_5s
    assert not trade.survived_10s
    assert not trade.is_terminal
    
    # Break at 10s
    trade.mark_broken(PaperTradeStatus.BROKEN_BY_MOVEMENT, "decay", 10)
    assert trade.status == PaperTradeStatus.BROKEN_BY_MOVEMENT
    assert trade.is_terminal
    assert trade.survived_5s
    assert not trade.survived_10s

def test_operator_pair_sorted():
    trade = PaperTrade(operator_a="fanduel", operator_b="draftkings")
    assert trade.operator_pair == "draftkings|fanduel"

def test_profit_decay():
    trade = PaperTrade(profit_pct_at_alert=2.5)
    
    snap5 = OddsSnapshot(timestamp=datetime.now(timezone.utc), interval_seconds=5, profit_pct=2.1)
    trade.record_snapshot(snap5)
    
    decay = trade.profit_decay
    assert decay["alert"] == 2.5
    assert decay["5s"] == 2.1
    assert decay["10s"] is None
