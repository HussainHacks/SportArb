"""
Sports Arbitrage Odds Scanner — Main Orchestrator

Async main loop with adaptive per-sport scheduling:
  1. Scheduler decides which sports are due for polling
  2. Collect odds per-sport from The Odds API
  3. Normalize to canonical format
  4. Cache in Redis
  5. Detect arbitrage opportunities
  6. Apply risk filters
  7. Record paper trades
  8. Send Telegram alerts
  9. Persist to PostgreSQL
  10. Report back to scheduler with fetch results
"""

import asyncio
import logging
import signal
import sys
import uuid
from datetime import datetime, timezone, timedelta

from backend.config import settings
from backend.collectors.odds_api import OddsApiCollector
from backend.normalization.normalizer import normalize_all
from backend.normalization.event_matcher import group_odds_by_event
from backend.normalization.models import NormalizedOdds
from backend.engine.arb_detector import detect_arbs, recompute_group
from backend.engine.risk_filters import apply_all_filters
from backend.engine.poll_scheduler import PollScheduler, FetchResult, PollTier
from backend.engine.market_index import MarketIndex
from backend.engine.active_arb_tracker import ActiveArbTracker
from backend.collectors.odds_api_io import OddsApiIoCollector
from backend.collectors.odds_api_io_ws import OddsApiIoWsCollector
from backend.alerts.telegram import telegram_service
from backend.storage.redis_cache import redis_cache
from backend.storage.db import init_db, close_db, async_session_factory
from backend.storage.models import ArbRecord
from backend.paper_trading.paper_trade_recorder import PaperTradeRecorder
from backend.paper_trading.paper_trade_rechecker import PaperTradeRechecker
from backend.paper_trading.paper_trade_reporter import PaperTradeReporter

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("sportsarb")

# Graceful shutdown flag
_shutdown = False

# Paper Trading Globals
paper_recorder = PaperTradeRecorder(min_arb_threshold=settings.min_arb_percent)
paper_rechecker = PaperTradeRechecker(paper_recorder, check_interval_sec=1.0)




def handle_shutdown(sig, frame):
    global _shutdown
    logger.info(f"Received signal {sig}. Shutting down gracefully...")
    _shutdown = True


async def persist_arbs(arbs):
    """Save arb opportunities to PostgreSQL."""
    if not arbs:
        return

    try:
        async with async_session_factory() as session:
            for arb in arbs:
                record = ArbRecord(
                    id=arb.id,
                    event_id=arb.event_id,
                    event_name=arb.event_name,
                    sport_key=arb.sport_key,
                    market_key=arb.market_key.value,
                    arb_percent=arb.arb_percent,
                    profit_percent=arb.profit_percent,
                    total_stake=arb.total_stake,
                    guaranteed_payout=arb.guaranteed_payout,
                    legs=[leg.model_dump(mode="json") for leg in arb.legs],
                    stakes=arb.stakes,
                    detected_at=arb.detected_at,
                    alerted=True,
                    is_live=True,
                )
                session.add(record)
            await session.commit()
            logger.info(f"Persisted {len(arbs)} arb records to database.")
    except Exception as e:
        logger.error(f"Failed to persist arbs: {e}")


def _analyze_raw_events(raw_events: list[dict]) -> tuple[bool, float | None]:
    """
    Analyze raw API events to determine live status and nearest commence time.

    Returns: (has_live_events, nearest_commence_minutes)
    """
    has_live = False
    nearest_minutes = None
    now = datetime.now(timezone.utc)

    for event in raw_events:
        # Check for live/in-play
        if event.get("commence_time"):
            try:
                commence = datetime.fromisoformat(
                    event["commence_time"].replace("Z", "+00:00")
                )
                minutes_until = (commence - now).total_seconds() / 60

                # Event has started (negative minutes) → live
                if minutes_until <= 0:
                    has_live = True

                # Track nearest future event
                if minutes_until > 0:
                    if nearest_minutes is None or minutes_until < nearest_minutes:
                        nearest_minutes = minutes_until
            except (ValueError, TypeError):
                pass

    return has_live, nearest_minutes


async def process_sport_data(
    raw_events: list[dict],
    sport_key: str,
    scheduler: PollScheduler,
):
    """
    Process fetched data for a sport: normalize → detect → filter → alert.

    This is the per-sport equivalent of the old run_scan_cycle.
    """
    # Normalize to canonical format
    normalized_odds = normalize_all(raw_events)
    if not normalized_odds:
        return

    # Cache in Redis
    await redis_cache.store_many(normalized_odds)

    # Group by event and detect arbs
    event_groups = group_odds_by_event(normalized_odds)
    all_arbs = []

    for group in event_groups:
        arbs = detect_arbs(
            event_odds_by_market=group.odds_by_market,
            event_name=group.event_name,
            min_profit=settings.min_arb_percent,
            max_profit=settings.max_arb_percent,
            default_stake=settings.default_stake,
        )
        all_arbs.extend(arbs)

    if all_arbs:
        logger.info(f"Raw arbs found for {sport_key}: {len(all_arbs)}")

    # Apply risk filters
    valid_arbs = apply_all_filters(all_arbs)

    if not valid_arbs:
        return

    # Paper trading recording
    # Include the scheduler tier at alert time for later analysis
    sport_state = scheduler.sport_states.get(sport_key)
    tier_at_alert = sport_state.tier.value if sport_state else "unknown"
    interval_at_alert = sport_state.interval_seconds if sport_state else 0

    keys_map = {arb.id: arb.market_group_key for arb in valid_arbs}
    paper_recorder.record_batch(valid_arbs, market_group_keys=keys_map)

    # Send Telegram alerts
    sent = await telegram_service.send_batch_alerts(valid_arbs)
    logger.info(
        f"Sent {sent} Telegram alerts for {sport_key} "
        f"(tier={tier_at_alert}, interval={interval_at_alert}s)"
    )

    # Persist to database
    await persist_arbs(valid_arbs)


import time
from dataclasses import dataclass, field as dc_field


@dataclass
class CycleDiagnostics:
    """Per-cycle metrics for the Odds-API.io scan loop."""
    cycle: int = 0
    # Per-sport counters (accumulated across sports)
    events_with_odds: int = 0   # unique events that returned odds (not total discovered)
    odds_lines: int = 0
    bookmakers_seen: set = dc_field(default_factory=set)
    sport_details: dict = dc_field(default_factory=dict)  # sport → {events, odds, books}
    # Market-level
    changed_groups: int = 0     # groups whose odds changed this cycle (not total groups)
    groups_2plus_books: int = 0
    groups_complementary: int = 0  # potentially usable pairs (not guaranteed valid)
    candidate_arbs: int = 0
    filtered_arbs: int = 0
    rejection_breakdown: dict[str, int] = dc_field(default_factory=dict)
    # Timing
    recompute_ms: float = 0.0
    alert_ms: float = 0.0
    elapsed_sec: float = 0.0

    def one_line(self) -> str:
        books = ",".join(sorted(self.bookmakers_seen)) or "none"
        return (
            f"cycle={self.cycle} "
            f"events_w_odds={self.events_with_odds} odds={self.odds_lines} "
            f"books=[{books}] "
            f"changed={self.changed_groups} groups_2plus={self.groups_2plus_books} "
            f"complementary={self.groups_complementary} "
            f"candidates={self.candidate_arbs} filtered={self.filtered_arbs} "
            f"recompute={self.recompute_ms:.1f}ms alert={self.alert_ms:.1f}ms "
            f"elapsed={self.elapsed_sec:.1f}s"
        )

    def full_block(self) -> str:
        lines = [f"📊 Cycle #{self.cycle} diagnostics:"]
        for sport, info in self.sport_details.items():
            books_str = ",".join(sorted(info["books"])) or "none"
            lines.append(
                f"  {sport}: {info['events_with_odds']} events (with odds), "
                f"{info['odds']} odds lines, books=[{books_str}]"
            )
        lines.append(
            f"  Changed groups: {self.changed_groups} | "
            f"Groups with 2+ books: {self.groups_2plus_books} | "
            f"Potentially complementary: {self.groups_complementary}"
        )
        lines.append(
            f"  Candidate arbs: {self.candidate_arbs} | "
            f"Filtered arbs: {self.filtered_arbs}"
        )
        if self.rejection_breakdown:
            parts = [f"{k}={v}" for k, v in self.rejection_breakdown.items() if v > 0]
            if parts:
                lines.append(f"  Rejections: {', '.join(parts)}")
        lines.append(
            f"  Timing — recompute: {self.recompute_ms:.1f}ms | "
            f"alert: {self.alert_ms:.1f}ms | "
            f"total: {self.elapsed_sec:.1f}s"
        )
        return "\n".join(lines)


def _has_complementary_sides(group) -> bool:
    """
    Quick check if a MarketGroup is *potentially* usable for arb detection.

    This is a diagnostic-only heuristic, NOT a proof of real complementarity.
    Totals: checks for Over + Under.
    H2H/Spreads: checks for 2+ selections from 2+ operators (necessary but
    not sufficient — does not verify point symmetry or selection pairing).
    """
    selections = group.selection_names
    if len(selections) < 2:
        return False
    # For totals, need Over + Under
    lower = {s.lower() for s in selections}
    if "over" in lower and "under" in lower:
        return True
    # For H2H/spreads, 2+ distinct selections from 2+ operators is necessary
    if group.operator_count >= 2 and len(selections) >= 2:
        return True
    return False


async def run_odds_api_io_loop():
    """
    Incremental scan loop using Odds-API.io.

    Flow: fetch events → normalize → MarketIndex.upsert → recompute changed groups → alert
    """
    scan_sports = settings.odds_api_io_sports
    io_collector = OddsApiIoCollector(
        bookmakers=settings.odds_api_io_bookmakers,
        allowed_operators=settings.iowa_legal_operators,
    )
    market_index = MarketIndex()
    arb_tracker = ActiveArbTracker()
    cycle = 0
    # Poll math for 3 sports, 10 events/sport, 45s interval:
    # 3 sports * (1 events req + 10 odds reqs) / 45 * 3600 = 2640 requests/hr
    poll_interval = 45

    logger.info("🔄 Odds-API.io incremental mode active")
    logger.info(f"   Bookmakers: {io_collector.bookmakers}")
    logger.info(f"   Sports: {scan_sports}")
    logger.info(f"   Poll interval: {poll_interval}s")

    # ── Startup Telegram message (Priority 3) ──
    try:
        sports_str = ", ".join(scan_sports)
        books_str = ", ".join(io_collector.bookmakers)
        await telegram_service.send_status_message(
            "✅ Sports Arb Scanner started!\n"
            f"Mode: Odds-API.io (incremental)\n"
            f"Sports: {sports_str}\n"
            f"Bookmakers: {books_str}\n"
            f"Poll interval: {poll_interval}s\n"
            f"Iowa-legal operators: {len(settings.iowa_legal_operators)}"
        )
    except Exception:
        logger.warning("Could not send startup message to Telegram.")

    try:
        while not _shutdown:
            cycle += 1
            t0 = datetime.now(timezone.utc)
            diag = CycleDiagnostics(cycle=cycle)

            for sport in scan_sports:
                if _shutdown:
                    break

                try:
                    active_arbs_for_sport = {
                        a.event_id
                        for a in arb_tracker.active_arbs.values()
                        if a.sport_key == sport
                    }
                    odds = await io_collector.fetch_and_normalize(
                        sport=sport, 
                        active_arb_event_ids=active_arbs_for_sport
                    )
                    sport_books = {o.operator for o in odds} if odds else set()
                    # Count unique events from the odds lines
                    sport_event_ids = {o.event_id for o in odds} if odds else set()
                    diag.sport_details[sport] = {
                        "events_with_odds": len(sport_event_ids),
                        "odds": len(odds) if odds else 0,
                        "books": sport_books,
                    }
                    diag.events_with_odds += len(sport_event_ids)
                    diag.odds_lines += len(odds) if odds else 0
                    diag.bookmakers_seen.update(sport_books)

                    if not odds:
                        continue

                    changed_keys = market_index.upsert_odds(odds)

                    # 1.4 REST operator reconciliation
                    for event_id in sport_event_ids:
                        event_odds = [o for o in odds if o.event_id == event_id]
                        current_ops = {o.operator for o in event_odds}
                        indexed_ops = {op for op in market_index._operator_index
                                       if any(k.startswith(f"{event_id}:") for k in market_index._operator_index[op])}
                        vanished = indexed_ops - current_ops
                        for op in vanished:
                            removed = market_index.remove_operator_from_event(op, event_id)
                            changed_keys.update(removed)

                    diag.changed_groups += len(changed_keys)

                    for key in changed_keys:
                        group = market_index.get_group(key)
                        if group is None:
                            continue

                        if group.operator_count >= 2:
                            diag.groups_2plus_books += 1

                        if _has_complementary_sides(group):
                            diag.groups_complementary += 1

                        if group.operator_count < 2:
                            continue

                        t_rec_start = time.monotonic()
                        arbs = recompute_group(
                            group,
                            min_profit=settings.min_arb_percent,
                            max_profit=settings.max_arb_percent,
                        )
                        diag.recompute_ms += (time.monotonic() - t_rec_start) * 1000
                        diag.candidate_arbs += len(arbs)

                        if arbs:
                            t_alert_start = time.monotonic()
                            filtered, rejection_counts = apply_all_filters(arbs)
                            diag.filtered_arbs += len(filtered)
                            
                            for reason, count in rejection_counts.items():
                                diag.rejection_breakdown[reason] = diag.rejection_breakdown.get(reason, 0) + count
                                
                            alertable = arb_tracker.process_arbs(key, filtered)
                            for a in alertable:
                                paper_recorder.record_arb(a)
                                try:
                                    await telegram_service.send_arb_alert(a)
                                except Exception:
                                    pass
                            diag.alert_ms += (time.monotonic() - t_alert_start) * 1000
                        else:
                            # Market recomputed, no arb — remove from tracker
                            arb_tracker.process_arbs(key, [])

                except Exception as e:
                    logger.error(f"Odds-API.io error ({sport}): {e}", exc_info=True)

            elapsed = (datetime.now(timezone.utc) - t0).total_seconds()
            diag.elapsed_sec = elapsed

            # ── Diagnostic logging ──
            # One-line every cycle, full block every 5 cycles
            logger.info(diag.one_line())
            if cycle % 5 == 0:
                logger.info(diag.full_block())
                stats = market_index.get_stats()
                logger.info(
                    f"   Index totals — Markets: {stats['markets']} | "
                    f"Events: {stats['events']} | "
                    f"Change rate: {stats['change_rate']} | "
                    f"Active arbs: {arb_tracker.active_count}"
                )
                PaperTradeReporter.print_summary(paper_recorder.trades)

            # Clean stale groups every 60 cycles
            if cycle % 60 == 0:
                market_index.remove_stale_groups(max_age_seconds=600)

            sleep_time = max(0, poll_interval - elapsed)
            await asyncio.sleep(sleep_time)

    finally:
        await io_collector.close()


async def run_odds_api_io_ws_loop():
    """
    WebSocket-based scan loop using Odds-API.io.
    """
    market_index = MarketIndex()
    arb_tracker = ActiveArbTracker()
    
    def _process_changed_keys(changed_keys: set[str]):
        for key in changed_keys:
            group = market_index.get_group(key)
            if not group or not _has_complementary_sides(group):
                arb_tracker.process_arbs(key, [])
                continue
                
            arbs = recompute_group(group, min_profit=settings.min_arb_percent, max_profit=settings.max_arb_percent)
            if arbs:
                filtered, _ = apply_all_filters(arbs)
                alertable = arb_tracker.process_arbs(key, filtered)
                for a in alertable:
                    paper_recorder.record_arb(a)
                    asyncio.create_task(telegram_service.send_arb_alert(a))
            else:
                arb_tracker.process_arbs(key, [])

    def handle_odds_update(odds: list[NormalizedOdds]):
        if not odds:
            return
        changed_keys = market_index.upsert_odds(odds)
        _process_changed_keys(changed_keys)

    def handle_odds_delete(operator: str, event_id: str):
        changed_keys = market_index.remove_operator_from_event(operator, event_id)
        _process_changed_keys(changed_keys)

    ws_collector = OddsApiIoWsCollector(
        bookmakers=settings.odds_api_io_bookmakers,
        sports=settings.odds_api_io_sports,
        allowed_operators=settings.iowa_legal_operators,
        on_odds_update=handle_odds_update,
        on_odds_delete=handle_odds_delete,
    )

    logger.info("🔄 Odds-API.io WEBSOCKET mode active")
    logger.info(f"   Sports: {settings.odds_api_io_sports}")
    logger.info(f"   Bookmakers: {settings.odds_api_io_bookmakers}")
    
    # Startup Telegram message
    try:
        sports_str = ", ".join(settings.odds_api_io_sports)
        books_str = ", ".join(settings.odds_api_io_bookmakers)
        await telegram_service.send_status_message(
            "✅ Sports Arb Scanner started!\n"
            f"Mode: Odds-API.io (WebSocket)\n"
            f"Sports: {sports_str}\n"
            f"Bookmakers: {books_str}\n"
            f"Iowa-legal operators: {len(settings.iowa_legal_operators)}"
        )
    except Exception:
        logger.warning("Could not send startup message to Telegram.")
    
    try:
        await ws_collector.start()
        
        cycle = 0
        while not _shutdown:
            cycle += 1
            await asyncio.sleep(60)
            
            # Diagnostic logging every minute
            logger.info(f"====== WS Heartbeat (Min {cycle}) ======")
            logger.info(f"   Recv: {ws_collector.metrics['messages_recv']} | Updated: {ws_collector.metrics['updates_processed']} | Deleted: {ws_collector.metrics['deletes_processed']}")
            logger.info(f"   Cache misses: {ws_collector.metrics['cache_misses']} | Reconnects: {ws_collector.metrics['reconnects']}")
            stats = market_index.get_stats()
            logger.info(f"   Index — Markets: {stats['markets']} | Events: {stats['events']} | Active arbs: {arb_tracker.active_count}")
            if cycle % 5 == 0:
                PaperTradeReporter.print_summary(paper_recorder.trades)
                        
            # Clean stale groups
            market_index.remove_stale_groups(max_age_seconds=600)
            
    finally:
        await ws_collector.stop()


async def main():
    """Main entry point — initialize services and run adaptive scan loop."""
    use_odds_api_io = bool(settings.odds_api_io_key)
    mode_label = "Odds-API.io (incremental)" if use_odds_api_io else "The Odds API (legacy)"

    logger.info("🚀 Sports Arbitrage Odds Scanner starting...")
    logger.info(f"   Mode: {mode_label}")
    logger.info(f"   Default stake: ${settings.default_stake}")
    logger.info(f"   Min arb profit: {settings.min_arb_percent}%")
    logger.info(f"   Max arb profit: {settings.max_arb_percent}%")
    logger.info(f"   Iowa-legal operators: {len(settings.iowa_legal_operators)}")

    # Register signal handlers for graceful shutdown
    signal.signal(signal.SIGINT, handle_shutdown)
    signal.signal(signal.SIGTERM, handle_shutdown)

    # Initialize services
    try:
        await redis_cache.connect()
        await init_db()
    except Exception as e:
        logger.error(f"Failed to initialize services: {e}")
        logger.info("Continuing without Redis/PostgreSQL (scan-only mode)...")

    # Start Paper Trade Rechecker
    paper_rechecker.start()

    # Initialize adaptive scheduler
    scheduler = PollScheduler(
        sport_keys=settings.target_sports,
        prematch_interval=settings.poll_prematch_seconds,
        live_interval=settings.poll_live_seconds,
        near_start_interval=settings.poll_near_start_seconds,
        burst_interval=settings.poll_burst_seconds,
        backoff_interval=settings.poll_backoff_seconds,
        burst_duration=settings.burst_duration_seconds,
        near_start_threshold_minutes=settings.near_start_threshold_minutes,
    )

    # Route to Odds-API.io incremental mode if configured
    if use_odds_api_io:
        try:
            if settings.odds_api_io_mode.lower() == "websocket":
                await run_odds_api_io_ws_loop()
            else:
                await run_odds_api_io_loop()
        finally:
            logger.info("Shutting down services...")
            PaperTradeReporter.print_summary(paper_recorder.trades)
            logger.info(f"Rechecker metrics: {paper_rechecker.metrics}")
            await paper_rechecker.stop()
            await telegram_service.close()
            try:
                await redis_cache.close()
                await close_db()
            except Exception:
                pass
            logger.info("🛑 Scanner stopped.")
        return

    # Legacy: The Odds API polling mode
    collector = OddsApiCollector()
    cycle = 0
    last_status_log = datetime.now(timezone.utc)

    # Send startup notification
    try:
        await telegram_service.send_status_message(
            "✅ Sports Arb Scanner started!\n"
            f"Monitoring {len(settings.target_sports)} sports across "
            f"{len(settings.iowa_legal_operators)} Iowa-legal operators.\n"
            f"Scheduler: adaptive per-sport polling"
        )
    except Exception:
        logger.warning("Could not send startup message to Telegram.")

    try:
        while not _shutdown:
            # Get sports that are due for polling
            due_sports = scheduler.get_due_sports()

            if due_sports:
                cycle += 1
                logger.info(f"{'='*60}")
                logger.info(
                    f"SCAN CYCLE #{cycle} — polling {len(due_sports)} sport(s): "
                    f"{', '.join(due_sports)}"
                )
                logger.info(f"{'='*60}")

                for sport_key in due_sports:
                    if _shutdown:
                        break

                    sport_state = scheduler.sport_states.get(sport_key)
                    tier = sport_state.tier.value if sport_state else "?"
                    logger.info(f"⏱️ Polling {sport_key} (tier={tier})")

                    try:
                        raw_events = await collector.fetch_odds(sport_key)

                        # Analyze for live/near-start detection
                        has_live, nearest_minutes = _analyze_raw_events(raw_events)

                        # Report back to scheduler
                        scheduler.report_fetch(FetchResult(
                            sport_key=sport_key,
                            events=raw_events,
                            success=True,
                            has_live_events=has_live,
                            nearest_commence_minutes=nearest_minutes,
                        ))

                        # Process the data if we got events
                        if raw_events:
                            await process_sport_data(raw_events, sport_key, scheduler)

                    except Exception as e:
                        logger.error(f"Error fetching {sport_key}: {e}", exc_info=True)
                        scheduler.report_fetch(FetchResult(
                            sport_key=sport_key,
                            success=False,
                            error_message=str(e),
                        ))

                    # Small delay between sport fetches to be respectful
                    await asyncio.sleep(0.5)

                # Print paper trade summary every 5 cycles
                if cycle % 5 == 0:
                    PaperTradeReporter.print_summary(paper_recorder.trades)

            # Log scheduler status every 5 minutes
            now = datetime.now(timezone.utc)
            if (now - last_status_log).total_seconds() >= 300:
                logger.info(scheduler.get_status_summary())
                last_status_log = now

            # 1-second loop tick — the scheduler decides per-sport readiness
            await asyncio.sleep(1)

    finally:
        logger.info("Shutting down services...")

        # Dump final stats
        PaperTradeReporter.print_summary(paper_recorder.trades)
        logger.info(f"Rechecker metrics: {paper_rechecker.metrics}")
        logger.info(scheduler.get_status_summary())

        await paper_rechecker.stop()
        await collector.close()
        await telegram_service.close()
        try:
            await redis_cache.close()
            await close_db()
        except Exception:
            pass
        logger.info("🛑 Scanner stopped.")


if __name__ == "__main__":
    asyncio.run(main())
