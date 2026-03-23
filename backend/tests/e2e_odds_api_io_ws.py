import asyncio
import logging
import sys

from backend.config import settings
from backend.collectors.odds_api_io_ws import OddsApiIoWsCollector

logging.basicConfig(level=logging.INFO, stream=sys.stdout)
logger = logging.getLogger("e2e_ws")

async def main():
    if not settings.odds_api_io_key:
        logger.error("No API key configured.")
        return

    updates_received = 0
    deletes_received = 0

    def on_update(odds_list):
        nonlocal updates_received
        updates_received += len(odds_list)
        if len(odds_list) > 0:
            first = odds_list[0]
            logger.info(f"[UPDATE] {first.operator_display} | {first.event_name} | {first.market_key.value} | {len(odds_list)} selections")

    def on_delete(bookie, event_id):
        nonlocal deletes_received
        deletes_received += 1
        logger.warning(f"[DELETE] {bookie} dropped from event {event_id}")

    ws_collector = OddsApiIoWsCollector(
        on_odds_update=on_update,
        on_odds_delete=on_delete,
    )

    logger.info("Starting WS test for 20 seconds...")
    await ws_collector.start()

    try:
        # Let it stream messages
        for _ in range(20):
            await asyncio.sleep(1)
            logger.info(f"Metrics: {ws_collector.metrics}")
    except KeyboardInterrupt:
        pass
    finally:
        await ws_collector.stop()
        logger.info(f"Test complete. Total Updates: {updates_received}, Total Deletes: {deletes_received}")

if __name__ == "__main__":
    asyncio.run(main())
