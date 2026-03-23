"""
Odds-API.io REST Collector — fetches events and odds via polling.

Two main operations:
  1. fetch_events(sport, league) → list of event dicts
  2. fetch_odds(event_id, bookmakers) → single event with odds

For arb scanning: fetch events, then poll odds for each event.
Produces NormalizedOdds via odds_api_io_normalizer.
"""

import logging
import time
from collections import deque
from datetime import datetime, timezone
from typing import Any

import httpx

from backend.config import settings
from backend.collectors.odds_api_io_normalizer import (
    normalize_odds_response,
    normalize_events_batch,
)
from backend.engine.event_ranker import rank_events_for_polling
from backend.normalization.models import NormalizedOdds

logger = logging.getLogger(__name__)

BASE_URL = "https://api.odds-api.io/v3"


class OddsApiIoCollector:
    """
    REST polling collector for Odds-API.io.

    Fetches events and odds at configurable intervals.
    Outputs NormalizedOdds compatible with MarketIndex.
    """

    def __init__(
        self,
        api_key: str | None = None,
        bookmakers: list[str] | None = None,
        allowed_operators: set[str] | None = None,
        timeout: float = 10.0,
    ):
        self.api_key = api_key or settings.odds_api_io_key
        self.bookmakers = bookmakers or settings.odds_api_io_bookmakers
        self.allowed_operators = allowed_operators
        self._client = httpx.AsyncClient(
            timeout=timeout,
            headers={"User-Agent": "SportsArbScanner/1.0"},
        )

        # Latency metrics
        self.total_requests = 0
        self.total_errors = 0
        self.last_fetch_ms: float = 0
        self.last_normalize_ms: float = 0
        
        # Rolling hourly request guard (5,000/hr limit)
        self._request_times: deque[float] = deque()

    def _can_request(self, ceiling: int = 4500) -> bool:
        """Check if we are under the rolling 60-minute request limit."""
        now = time.monotonic()
        while self._request_times and now - self._request_times[0] > 3600:
            self._request_times.popleft()
        return len(self._request_times) < ceiling

    def _record_request(self):
        """Record a successful request timestamp."""
        self._request_times.append(time.monotonic())
        self.total_requests += 1

    async def close(self):
        await self._client.aclose()

    async def fetch_events(
        self,
        sport: str | None = None,
        league: str | None = None,
        status: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        """
        Fetch events from /v3/events.

        Args:
            sport: Sport slug (e.g., "basketball", "football")
            league: League slug (e.g., "usa-nba", "england-premier-league")
            status: Event status filter ("pending", "live", "settled")
            limit: Max events to return
        """
        params: dict[str, Any] = {
            "apiKey": self.api_key,
            "limit": limit,
        }
        if sport:
            params["sport"] = sport
        if league:
            params["league"] = league
        if status:
            params["status"] = status

        if not self._can_request():
            logger.warning("Odds-API.io: Hourly request ceiling reached. Skipping fetch_events.")
            return []

        try:
            self._record_request()
            t0 = time.monotonic()
            resp = await self._client.get(f"{BASE_URL}/events", params=params)
            self.last_fetch_ms = (time.monotonic() - t0) * 1000

            if resp.status_code == 401:
                logger.error("Odds-API.io: Invalid API key")
                self.total_errors += 1
                return []
            if resp.status_code == 429:
                logger.warning("Odds-API.io: Rate limited")
                self.total_errors += 1
                return []
            resp.raise_for_status()

            data = resp.json()
            events = data if isinstance(data, list) else []
            logger.info(
                f"Odds-API.io: Fetched {len(events)} events "
                f"(sport={sport}, league={league}, {self.last_fetch_ms:.0f}ms)"
            )
            return events

        except httpx.HTTPError as e:
            logger.error(f"Odds-API.io events error: {e}")
            self.total_errors += 1
            return []

    async def fetch_odds(
        self,
        event_id: int | str,
        bookmakers: list[str] | None = None,
    ) -> dict[str, Any] | None:
        """
        Fetch odds for a single event from /v3/odds.

        Args:
            event_id: Numeric event ID
            bookmakers: Optional list of bookmaker display names
        """
        bm_list = bookmakers or self.bookmakers
        params: dict[str, Any] = {
            "apiKey": self.api_key,
            "eventId": str(event_id),
        }
        if bm_list:
            params["bookmakers"] = ",".join(bm_list)

        if not self._can_request():
            logger.warning(f"Odds-API.io: Hourly request ceiling reached. Skipping fetch_odds for event {event_id}.")
            return None

        try:
            self._record_request()
            t0 = time.monotonic()
            resp = await self._client.get(f"{BASE_URL}/odds", params=params)
            self.last_fetch_ms = (time.monotonic() - t0) * 1000

            if resp.status_code == 401:
                logger.error("Odds-API.io: Invalid API key")
                self.total_errors += 1
                return None
            if resp.status_code == 429:
                logger.warning("Odds-API.io: Rate limited")
                self.total_errors += 1
                return None
            resp.raise_for_status()

            data = resp.json()
            return data

        except httpx.HTTPError as e:
            logger.error(f"Odds-API.io odds error for event {event_id}: {e}")
            self.total_errors += 1
            return None

    async def fetch_and_normalize(
        self,
        sport: str | None = None,
        league: str | None = None,
        status: str | None = None,
        active_arb_event_ids: set[str] | None = None,
    ) -> list[NormalizedOdds]:
        """
        Full pipeline: fetch events → rank & take top 10 → fetch odds → normalize.
        """
        events = await self.fetch_events(sport=sport, league=league, status=status)
        if not events:
            return []

        active_arbs = active_arb_event_ids or set()
        events = rank_events_for_polling(events, active_arb_event_ids=active_arbs, limit=10)

        all_odds: list[NormalizedOdds] = []

        for event in events:
            event_id = event.get("id")
            if event_id is None:
                continue

            raw_odds = await self.fetch_odds(event_id)
            if raw_odds is None:
                continue

            t0 = time.monotonic()
            normalized = normalize_odds_response(
                raw_odds, allowed_operators=self.allowed_operators,
            )
            self.last_normalize_ms = (time.monotonic() - t0) * 1000

            all_odds.extend(normalized)

        # 3.4 Post-fetch noise reduction
        filtered_odds = []
        for event_id in {o.event_id for o in all_odds}:
            event_odds = [o for o in all_odds if o.event_id == event_id]
            operators = {o.operator for o in event_odds}
            if len(operators) >= 2:
                filtered_odds.extend(event_odds)
            else:
                logger.debug(f"Odds-API.io: Skipping event {event_id} due to low operator overlap ({len(operators)} books)")

        logger.info(
            f"Odds-API.io: {len(filtered_odds)} odds lines from {len(events)} events "
            f"(fetch={self.last_fetch_ms:.0f}ms, normalize={self.last_normalize_ms:.0f}ms)"
        )
        return filtered_odds

    async def fetch_arbs(self) -> list[dict[str, Any]]:
        """
        Fetch pre-computed arbs from /v3/arbitrage-bets.

        Returns raw arb data from Odds-API.io for cross-validation.
        """
        params = {"apiKey": self.api_key}
        if self.bookmakers:
            params["bookmakers"] = ",".join(self.bookmakers)

        try:
            resp = await self._client.get(f"{BASE_URL}/arbitrage-bets", params=params)
            self.total_requests += 1

            if resp.status_code != 200:
                logger.warning(f"Odds-API.io arbs endpoint: {resp.status_code}")
                return []

            data = resp.json()
            return data if isinstance(data, list) else []

        except httpx.HTTPError as e:
            logger.error(f"Odds-API.io arbs error: {e}")
            self.total_errors += 1
            return []

    def get_metrics(self) -> dict:
        return {
            "total_requests": self.total_requests,
            "total_errors": self.total_errors,
            "last_fetch_ms": round(self.last_fetch_ms, 1),
            "last_normalize_ms": round(self.last_normalize_ms, 1),
        }
