"""
The Odds API collector.

Polls https://api.the-odds-api.com/v4/sports/{sport}/odds
for all configured sports and returns raw JSON events.
"""

import asyncio
import logging
from typing import Any
import aiohttp

from backend.collectors.base import BaseCollector
from backend.config import settings

logger = logging.getLogger(__name__)

# Operator key → display name mapping for The Odds API
OPERATOR_DISPLAY_NAMES = {
    "draftkings": "DraftKings",
    "fanduel": "FanDuel",
    "betmgm": "BetMGM",
    "williamhill_us": "Caesars",
    "fanatics": "Fanatics",
    "betrivers": "BetRivers",
    "bet365": "bet365",
    "circasports": "Circa Sports",
    "hardrockbet": "Hard Rock Sportsbook",
    "espnbet": "ESPN BET",
    "qsportsbook": "Q Sportsbook",
    "sporttrade": "Sporttrade",
}


class OddsApiCollector(BaseCollector):
    """Collector for The Odds API (https://the-odds-api.com)."""

    name = "the_odds_api"

    def __init__(self):
        self._session: aiohttp.ClientSession | None = None
        self._remaining_requests: int | None = None
        self._used_requests: int | None = None

    async def _ensure_session(self):
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()

    async def fetch_odds(self, sport_key: str) -> list[dict[str, Any]]:
        """
        Fetch odds for a single sport from The Odds API.

        Endpoint: GET /v4/sports/{sport}/odds
        """
        await self._ensure_session()

        url = f"{settings.odds_api_base_url}/sports/{sport_key}/odds"
        params = {
            "apiKey": settings.odds_api_key,
            "regions": settings.odds_api_regions,
            "markets": settings.odds_api_markets,
            "oddsFormat": settings.odds_api_odds_format,
            "bookmakers": ",".join(settings.iowa_legal_operators),
        }

        try:
            async with self._session.get(url, params=params, timeout=aiohttp.ClientTimeout(total=15)) as resp:
                # Track API usage from headers
                self._remaining_requests = resp.headers.get("x-requests-remaining")
                self._used_requests = resp.headers.get("x-requests-used")

                if resp.status == 429:
                    retry_after = int(resp.headers.get("Retry-After", 60))
                    logger.warning(f"Rate limited by The Odds API. Retrying after {retry_after}s.")
                    await asyncio.sleep(retry_after)
                    return await self.fetch_odds(sport_key)  # Retry once

                if resp.status == 401:
                    logger.error("Invalid Odds API key.")
                    return []

                if resp.status != 200:
                    body = await resp.text()
                    logger.error(f"Odds API error {resp.status}: {body}")
                    return []

                data = await resp.json()
                logger.info(
                    f"Fetched {len(data)} events for {sport_key} "
                    f"(API usage: {self._used_requests} used, {self._remaining_requests} remaining)"
                )
                return data

        except asyncio.TimeoutError:
            logger.error(f"Timeout fetching odds for {sport_key}")
            return []
        except aiohttp.ClientError as e:
            logger.error(f"HTTP error fetching odds for {sport_key}: {e}")
            return []

    async def fetch_all_sports(self) -> list[dict[str, Any]]:
        """Fetch odds for all configured sports sequentially (to respect rate limits)."""
        all_events = []
        for sport_key in settings.target_sports:
            events = await self.fetch_odds(sport_key)
            all_events.extend(events)
            # Small delay between requests to be respectful
            await asyncio.sleep(0.5)
        logger.info(f"Total events fetched: {len(all_events)}")
        return all_events

    @property
    def api_usage(self) -> dict:
        """Return current API usage stats."""
        return {
            "remaining": self._remaining_requests,
            "used": self._used_requests,
        }

    async def close(self):
        if self._session and not self._session.closed:
            await self._session.close()
            logger.info("OddsApiCollector session closed.")
