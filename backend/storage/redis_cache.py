"""
Redis cache for live odds — fast read/write for the arb engine.

Keys follow the pattern:  odds:{event_id}:{market_key}:{operator}:{selection}
Values are JSON-serialized NormalizedOdds.
TTL is set to 2x the stale_odds_seconds so stale entries auto-expire.
"""

import json
import logging
from typing import Optional
import redis.asyncio as redis
from backend.config import settings
from backend.normalization.models import NormalizedOdds

logger = logging.getLogger(__name__)


class RedisCache:
    def __init__(self):
        self._redis: Optional[redis.Redis] = None
        self._ttl = settings.stale_odds_seconds * 2  # Auto-expire stale odds

    async def connect(self):
        """Initialize Redis connection."""
        self._redis = redis.from_url(
            settings.redis_url,
            decode_responses=True,
        )
        await self._redis.ping()
        logger.info("Redis connected.")

    async def close(self):
        """Close Redis connection."""
        if self._redis:
            await self._redis.close()
            logger.info("Redis connection closed.")

    def _make_key(self, odds: NormalizedOdds) -> str:
        """Build a unique cache key for an odds line."""
        point_str = f":{odds.point}" if odds.point is not None else ""
        return f"odds:{odds.event_id}:{odds.market_key.value}:{odds.operator}:{odds.selection}{point_str}"

    async def store_odds(self, odds: NormalizedOdds):
        """Store a single odds line in Redis with TTL."""
        key = self._make_key(odds)
        value = odds.model_dump_json()
        await self._redis.setex(key, self._ttl, value)

    async def store_many(self, odds_list: list[NormalizedOdds]):
        """Store multiple odds lines in a pipeline."""
        if not odds_list:
            return
        async with self._redis.pipeline() as pipe:
            for odds in odds_list:
                key = self._make_key(odds)
                value = odds.model_dump_json()
                pipe.setex(key, self._ttl, value)
            await pipe.execute()
        logger.debug(f"Cached {len(odds_list)} odds lines.")

    async def get_all_odds_for_event(self, event_id: str) -> list[NormalizedOdds]:
        """Retrieve all odds lines for a specific event."""
        pattern = f"odds:{event_id}:*"
        odds_list = []
        async for key in self._redis.scan_iter(match=pattern, count=200):
            raw = await self._redis.get(key)
            if raw:
                odds_list.append(NormalizedOdds.model_validate_json(raw))
        return odds_list

    async def get_all_event_ids(self) -> set[str]:
        """Get all unique event IDs currently in cache."""
        event_ids = set()
        async for key in self._redis.scan_iter(match="odds:*", count=500):
            parts = key.split(":")
            if len(parts) >= 3:
                event_ids.add(parts[1])
        return event_ids

    async def flush_event(self, event_id: str):
        """Remove all cached odds for an event."""
        pattern = f"odds:{event_id}:*"
        keys = []
        async for key in self._redis.scan_iter(match=pattern, count=200):
            keys.append(key)
        if keys:
            await self._redis.delete(*keys)


# Singleton instance
redis_cache = RedisCache()
