"""
Abstract base class for odds data collectors.
"""

import logging
from abc import ABC, abstractmethod
from typing import Any

logger = logging.getLogger(__name__)


class BaseCollector(ABC):
    """Base class for all odds data collectors/vendors."""

    name: str = "base"

    @abstractmethod
    async def fetch_odds(self, sport_key: str) -> list[dict[str, Any]]:
        """
        Fetch raw odds data for a given sport.

        Returns a list of raw event dicts from the vendor's API.
        """
        ...

    @abstractmethod
    async def fetch_all_sports(self) -> list[dict[str, Any]]:
        """
        Fetch odds for all configured sports.

        Returns a flat list of all raw event dicts.
        """
        ...

    @abstractmethod
    async def close(self):
        """Clean up any open connections."""
        ...
