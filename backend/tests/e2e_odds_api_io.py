"""
Quick end-to-end test: Odds-API.io → normalize → MarketIndex → recompute_group.

Usage: python -m backend.tests.e2e_odds_api_io
"""

import asyncio
import logging
import sys

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s")
logger = logging.getLogger("e2e_test")

from backend.collectors.odds_api_io import OddsApiIoCollector
from backend.engine.market_index import MarketIndex
from backend.engine.arb_detector import recompute_group
from backend.engine.active_arb_tracker import ActiveArbTracker
from backend.engine.risk_filters import apply_all_filters
from backend.config import settings


async def main():
    collector = OddsApiIoCollector(
        bookmakers=["DraftKings", "FanDuel"],
        allowed_operators=settings.iowa_legal_operators,
    )
    market_index = MarketIndex()
    arb_tracker = ActiveArbTracker()

    try:
        # Step 1: Fetch events
        logger.info("=" * 60)
        logger.info("PHASE 1: Fetching events from Odds-API.io")
        logger.info("=" * 60)

        sports_to_scan = ["basketball", "ice-hockey", "football"]
        all_odds = []

        for sport in sports_to_scan:
            odds = await collector.fetch_and_normalize(sport=sport)
            all_odds.extend(odds)
            logger.info(f"  {sport}: {len(odds)} odds lines")

        logger.info(f"\nTotal odds lines: {len(all_odds)}")
        logger.info(f"Unique events: {len({o.event_id for o in all_odds})}")
        logger.info(f"Unique operators: {sorted({o.operator for o in all_odds})}")

        if not all_odds:
            logger.warning("No odds returned — check API key and bookmakers")
            return

        # Step 2: Feed into MarketIndex
        logger.info("\n" + "=" * 60)
        logger.info("PHASE 2: Feeding into MarketIndex")
        logger.info("=" * 60)

        changed_keys = market_index.upsert_odds(all_odds)
        stats = market_index.get_stats()
        logger.info(f"Market groups: {stats['markets']}")
        logger.info(f"Operators tracked: {stats['operators']}")
        logger.info(f"Events tracked: {stats['events']}")
        logger.info(f"Changed market groups: {len(changed_keys)}")

        # Step 3: Recompute arbs for changed groups
        logger.info("\n" + "=" * 60)
        logger.info("PHASE 3: Incremental arb detection")
        logger.info("=" * 60)

        total_arbs = 0
        for key in changed_keys:
            group = market_index.get_group(key)
            if group is None or group.operator_count < 2:
                continue

            arbs = recompute_group(
                group,
                min_profit=settings.min_arb_percent,
                max_profit=settings.max_arb_percent,
            )

            if arbs:
                # Apply risk filters
                filtered = apply_all_filters(arbs)
                for arb in filtered:
                    alertable = arb_tracker.process_arbs(key, [arb])
                    total_arbs += len(alertable)

        logger.info(f"\nArbs found: {total_arbs}")
        logger.info(f"Active arbs: {arb_tracker.active_count}")
        logger.info(f"Tracker stats: {arb_tracker.get_stats()}")

        # Step 4: Summary
        logger.info("\n" + "=" * 60)
        logger.info("SUMMARY")
        logger.info("=" * 60)
        logger.info(f"Collector metrics: {collector.get_metrics()}")
        logger.info(f"Market index: {market_index.get_stats()}")
        logger.info("Pipeline working ✅" if len(all_odds) > 0 else "No data ❌")

    finally:
        await collector.close()


if __name__ == "__main__":
    asyncio.run(main())
