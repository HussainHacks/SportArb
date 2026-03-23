"""
Paper Trade Reporter — computes survival metrics and profit decay.

Processes accumulated PaperTrade records to answer three questions:
  1. Was the arb real at alert time?
  2. Did it survive long enough to act? 
  3. How fast did the edge decay?

Outputs debug-friendly console summaries and aggregated metrics.
"""

import logging
from statistics import median, mean
from collections import defaultdict

from backend.paper_trading.paper_trade_models import (
    PaperTrade,
    PaperTradeStatus,
    RECHECK_INTERVALS,
)

logger = logging.getLogger(__name__)


def _pct(count: int, total: int) -> float:
    return round((count / total) * 100, 2) if total > 0 else 0.0


class PaperTradeReporter:
    """Computes summary metrics from paper trades."""

    @staticmethod
    def generate_metrics(trades: list[PaperTrade]) -> dict:
        """
        Generate comprehensive survival and decay metrics.
        Ignores trades that are not yet terminal (still pending 30s check).
        """
        # Only analyze trades that have reached a terminal state
        terminal_trades = [t for t in trades if t.is_terminal]
        total = len(terminal_trades)

        if total == 0:
            return {"total_analyzed": 0}

        # 1. Survival counts
        survived_5s = sum(1 for t in terminal_trades if t.survived_5s)
        survived_10s = sum(1 for t in terminal_trades if t.survived_10s)
        survived_30s = sum(1 for t in terminal_trades if t.survived_30s)

        # 2. Break reasons
        broken_movement = sum(1 for t in terminal_trades if t.status == PaperTradeStatus.BROKEN_BY_MOVEMENT)
        broken_missing = sum(1 for t in terminal_trades if t.status == PaperTradeStatus.BROKEN_BY_MISSING_LINE)
        broken_suspension = sum(1 for t in terminal_trades if t.status == PaperTradeStatus.BROKEN_BY_SUSPENSION)

        # 3. Profit calculations (averages at each stage for survivors)
        profit_alert = mean(t.profit_pct_at_alert for t in terminal_trades)
        
        profit_5s_values = [t.snapshot_5s.profit_pct for t in terminal_trades if t.snapshot_5s and t.snapshot_5s.profit_pct is not None]
        profit_5s = median(profit_5s_values) if profit_5s_values else 0.0

        profit_10s_values = [t.snapshot_10s.profit_pct for t in terminal_trades if t.snapshot_10s and t.snapshot_10s.profit_pct is not None]
        profit_10s = median(profit_10s_values) if profit_10s_values else 0.0

        profit_30s_values = [t.snapshot_30s.profit_pct for t in terminal_trades if t.snapshot_30s and t.snapshot_30s.profit_pct is not None]
        profit_30s = median(profit_30s_values) if profit_30s_values else 0.0

        # 4. Operator pair survival (new user request)
        pair_stats = defaultdict(lambda: {"total": 0, "survived_5s": 0, "survived_10s": 0})
        for t in terminal_trades:
            pair = t.operator_pair
            pair_stats[pair]["total"] += 1
            if t.survived_5s:
                pair_stats[pair]["survived_5s"] += 1
            if t.survived_10s:
                pair_stats[pair]["survived_10s"] += 1

        # Format pairs for output
        operator_survival = {
            pair: {
                "total": stats["total"],
                "survival_5s_pct": _pct(stats["survived_5s"], stats["total"]),
                "survival_10s_pct": _pct(stats["survived_10s"], stats["total"]),
            }
            for pair, stats in pair_stats.items()
        }

        return {
            "total_analyzed": total,
            
            "survival_rates": {
                "5s_pct": _pct(survived_5s, total),
                "10s_pct": _pct(survived_10s, total),
                "30s_pct": _pct(survived_30s, total),
            },
            
            "break_reasons": {
                "movement_pct": _pct(broken_movement, total),
                "missing_line_pct": _pct(broken_missing, total),
                "suspension_pct": _pct(broken_suspension, total),
            },
            
            "profit_decay_median": {
                "alert_mean": round(profit_alert, 2),
                "5s": round(profit_5s, 2),
                "10s": round(profit_10s, 2),
                "30s": round(profit_30s, 2),
            },
            
            "operator_pairs": operator_survival,
        }

    @staticmethod
    def print_summary(trades: list[PaperTrade]):
        """Print a human-readable summary to the logger."""
        metrics = PaperTradeReporter.generate_metrics(trades)
        total = metrics.get("total_analyzed", 0)

        if total == 0:
            logger.info("📊 Paper Trading Summary: No terminal trades to report yet.")
            return

        surv = metrics["survival_rates"]
        reasons = metrics["break_reasons"]
        prof = metrics["profit_decay_median"]

        logger.info(f"{'='*50}")
        logger.info(f"📊 PAPER TRADING SUMMARY ({total} trades analyzed)")
        logger.info(f"{'='*50}")
        
        logger.info(f"SURVIVAL RATES:")
        logger.info(f"  5s  : {surv['5s_pct']}%")
        logger.info(f"  10s : {surv['10s_pct']}%")
        logger.info(f"  30s : {surv['30s_pct']}%")
        logger.info("")
        
        logger.info(f"PROFIT DECAY (Median of survivors):")
        logger.info(f"  Alert : {prof['alert_mean']}% (mean)")
        logger.info(f"  5s    : {prof['5s']}%")
        logger.info(f"  10s   : {prof['10s']}%")
        logger.info(f"  30s   : {prof['30s']}%")
        logger.info("")
        
        logger.info(f"BREAK REASONS:")
        logger.info(f"  Missing Line : {reasons['missing_line_pct']}% (Feed/book pulled it)")
        logger.info(f"  Odds Moved   : {reasons['movement_pct']}% (Edge decayed)")
        logger.info(f"  Suspended    : {reasons['suspension_pct']}% (Market locked)")
        logger.info("")

        logger.info(f"OPERATOR PAIRS (Top 5 by volume):")
        pairs = sorted(
            metrics["operator_pairs"].items(), 
            key=lambda x: x[1]["total"], 
            reverse=True
        )[:5]
        
        for pair, stats in pairs:
            logger.info(
                f"  {pair:<25}: {stats['total']} alerts, "
                f"5s={stats['survival_5s_pct']}%, 10s={stats['survival_10s_pct']}%"
            )
        
        logger.info(f"{'='*50}")
