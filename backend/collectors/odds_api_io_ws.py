"""
Odds-API.io WebSocket Collector.

Connects to `wss://api.odds-api.io/v3/ws` to receive real-time odds updates.
Maintains a local event cache (populated via REST) because WebSocket
messages lack team names and sport/league metadata.

Translates `updated`, `created`, `deleted`, and `no_markets` messages into
actionable events for the MarketIndex.
"""

import asyncio
import json
import logging
import time
from datetime import datetime, timezone
from typing import Any, Callable

import websockets
from websockets.exceptions import ConnectionClosed

from backend.config import settings
from backend.collectors.odds_api_io import OddsApiIoCollector
from backend.collectors.odds_api_io_normalizer import normalize_odds_response
from backend.normalization.models import NormalizedOdds

logger = logging.getLogger(__name__)


class OddsApiIoWsCollector:
    """WebSocket client for Odds-API.io with auto-reconnect and event caching."""

    def __init__(
        self,
        api_key: str | None = None,
        bookmakers: list[str] | None = None,
        sports: list[str] | None = None,
        markets: list[str] = ["ML", "Spread", "Totals"],
        allowed_operators: set[str] | None = None,
        on_odds_update: Callable[[list[NormalizedOdds]], None] | None = None,
        on_odds_delete: Callable[[str, str], None] | None = None,
    ):
        self.api_key = api_key or settings.odds_api_io_key
        self.bookmakers = bookmakers or settings.odds_api_io_bookmakers
        self.sports = sports or ["basketball", "ice-hockey", "american-football", "football"]
        self.markets = markets
        self.allowed_operators = allowed_operators
        
        # Callbacks to the engine
        self.on_odds_update = on_odds_update
        self.on_odds_delete = on_odds_delete

        # REST collector for cache backfilling
        self._rest = OddsApiIoCollector(
            api_key=self.api_key,
            bookmakers=self.bookmakers,
        )
        
        # Event cache: event_id -> { "home", "away", "sport_slug", "league_slug", "date", "status" }
        self._event_cache: dict[str, dict[str, Any]] = {}

        # Connection state
        self._ws: websockets.WebSocketClientProtocol | None = None
        self._running = False
        self._reconnect_attempts = 0

        # Metrics
        self.metrics = {
            "messages_recv": 0,
            "updates_processed": 0,
            "deletes_processed": 0,
            "cache_misses": 0,
            "reconnects": 0,
            "last_message_time": None,
        }

    async def start(self):
        """Start the WebSocket connection and message loop."""
        self._running = True
        logger.info("Starting Odds-API.io WebSocket collector...")
        
        # Pre-warm event cache
        await self._warmup_cache()

        # Enter reconnect loop
        asyncio.create_task(self._reconnect_loop())

    async def stop(self):
        """Close connection and stop loops."""
        self._running = False
        if self._ws:
            await self._ws.close()
        await self._rest.close()
        logger.info("🛑 Odds-API.io WebSocket collector stopped.")

    async def _warmup_cache(self):
        """Fetch active events across target sports to populate cache."""
        logger.info("Pre-warming event cache...")
        count = 0
        for sport in self.sports:
            events = await self._rest.fetch_events(sport=sport, limit=100)
            for e in events:
                self._cache_event(e)
                count += 1
            await asyncio.sleep(0.5)
        logger.info(f"Event cache warmed up: {count} events stored.")

    def _cache_event(self, raw_event: dict[str, Any]):
        """Store essential metadata from a REST event dict."""
        eid = str(raw_event.get("id"))
        if not eid or eid == "None":
            return
            
        sport = raw_event.get("sport", {})
        league = raw_event.get("league", {})
        
        self._event_cache[eid] = {
            "home": raw_event.get("home", "Unknown"),
            "away": raw_event.get("away", "Unknown"),
            "date": raw_event.get("date", ""),
            "status": raw_event.get("status", "pending"),
            "sport_slug": sport.get("slug", "unknown"),
            "league_slug": league.get("slug", "unknown"),
        }

    async def _ensure_cached(self, event_id: str):
        """If event isn't in cache, fetch it from REST."""
        if event_id in self._event_cache:
            return

        self.metrics["cache_misses"] += 1
        logger.debug(f"Cache miss for event {event_id}, querying REST...")
        
        # The Odds-API.io doesn't have a GET /events/{id}.
        # But we can query odds for the event, which includes the event metadata.
        # Actually /v3/odds gives the metadata in the root of the response.
        raw = await self._rest.fetch_odds(event_id=event_id)
        if raw:
            self._cache_event(raw)
        else:
            logger.warning(f"Failed to backfill cache for event {event_id}")

    async def _reconnect_loop(self):
        """Maintains the WS connection with exponential backoff on failure."""
        while self._running:
            try:
                await self._connect_and_listen()
            except ConnectionClosed as e:
                logger.warning(f"WebSocket closed unexpectedly: {e}")
            except Exception as e:
                logger.error(f"WebSocket error: {e}")

            if not self._running:
                break

            self._reconnect_attempts += 1
            self.metrics["reconnects"] += 1
            
            # Exponential backoff capped at 60s
            delay = min(60, 2 ** self._reconnect_attempts)
            logger.info(f"Reconnecting in {delay} seconds...")
            await asyncio.sleep(delay)

    def _build_url(self) -> str:
        """Construct the WS connection URL with filters."""
        params = [
            f"apiKey={self.api_key}",
            f"markets={','.join(self.markets)}",
            f"sport={','.join(self.sports)}",
        ]
        return f"wss://api.odds-api.io/v3/ws?{'&'.join(params)}"

    async def _connect_and_listen(self):
        """Connects and processes messages until the connection drops."""
        url = self._build_url()
        
        async with websockets.connect(url) as ws:
            self._ws = ws
            self._reconnect_attempts = 0
            logger.info("🟢 WebSocket connected to Odds-API.io")

            async for message in ws:
                if not self._running:
                    break
                
                self.metrics["messages_recv"] += 1
                self.metrics["last_message_time"] = time.time()
                
                try:
                    data = json.loads(message)
                    await self._handle_message(data)
                except json.JSONDecodeError:
                    logger.warning("Received invalid JSON on WebSocket")
                except Exception as e:
                    logger.error(f"Error handling WS message: {e}", exc_info=True)

    async def _handle_message(self, data: dict[str, Any]):
        """Route WS messages based on 'type'."""
        msg_type = data.get("type")
        
        if msg_type == "welcome":
            logger.info(f"WS Welcome: {data.get('message', '')}")
            logger.info(f"WS Filters applied: {data.get('sport_filter')}")
            
        elif msg_type in ("updated", "created"):
            await self._process_update(data)
            
        elif msg_type in ("deleted", "no_markets"):
            self._process_delete(data)
            
        elif msg_type == "error":
            logger.error(f"WS API Error: {data.get('message')}")
            
        else:
            logger.debug(f"Unhandled WS message type: {msg_type}")

    async def _process_update(self, ws_msg: dict[str, Any]):
        """Convert WS update to NormalizedOdds and trigger callback."""
        event_id = str(ws_msg.get("id"))
        bookie = ws_msg.get("bookie")
        markets = ws_msg.get("markets", [])
        
        if not event_id or not bookie or not markets:
            return

        # Ensure we have metadata to normalize properly
        await self._ensure_cached(event_id)
        
        cache = self._event_cache.get(event_id)
        if not cache:
            # Drop update if we can't augment it
            return

        # Translate WS shape into REST-like shape so we can reuse normalizer
        rest_like = {
            "id": event_id,
            "home": cache["home"],
            "away": cache["away"],
            "date": cache["date"],
            "status": cache["status"],
            "sport": {"slug": cache["sport_slug"]},
            "league": {"slug": cache["league_slug"]},
            "bookmakers": {
                bookie: markets
            }
        }

        # Normalize
        t0 = time.monotonic()
        normalized_list = normalize_odds_response(
            rest_like, allowed_operators=self.allowed_operators
        )
        
        if normalized_list and self.on_odds_update:
            self.on_odds_update(normalized_list)
            self.metrics["updates_processed"] += 1

    def _process_delete(self, ws_msg: dict[str, Any]):
        """Trigger delete callback for a bookie's odds on an event."""
        event_id = str(ws_msg.get("id"))
        bookie = ws_msg.get("bookie")
        
        if event_id and bookie and self.on_odds_delete:
            # We must canonicalize the bookmaker name to match the index
            canonical_bookie, _ = self._canonicalize(bookie)
            self.on_odds_delete(canonical_bookie, event_id)
            self.metrics["deletes_processed"] += 1

    def _canonicalize(self, name: str) -> tuple[str, str]:
        # Borrow canonicalization from normalizer
        from backend.collectors.odds_api_io_normalizer import _canonicalize_bookmaker
        return _canonicalize_bookmaker(name)
