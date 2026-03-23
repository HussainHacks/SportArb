"""
Telegram Alert Service — sends arb opportunity alerts via Telegram Bot API.

Features:
  - MarkdownV2 formatted messages
  - Retry with exponential backoff on HTTP 429
  - Deduplication (don't re-alert the same arb within 5 minutes)
  - Message throttling (max 30 msgs/sec per Telegram limits)
"""

import asyncio
import logging
import time
from typing import Optional
import aiohttp

from backend.config import settings
from backend.normalization.models import ArbOpportunity

logger = logging.getLogger(__name__)

# Telegram Bot API base URL
TELEGRAM_API_BASE = "https://api.telegram.org/bot{token}"

# Sport emoji mapping
SPORT_EMOJI = {
    "americanfootball_nfl": "🏈",
    "americanfootball_ncaaf": "🏈",
    "basketball_nba": "🏀",
    "basketball_ncaab": "🏀",
    "baseball_mlb": "⚾",
    "icehockey_nhl": "🏒",
    "soccer_usa_mls": "⚽",
    "soccer_epl": "⚽",
    "soccer_uefa_champs_league": "⚽",
}


def escape_markdown_v2(text: str) -> str:
    """Escape special characters for Telegram MarkdownV2."""
    special_chars = r"_*[]()~`>#+-=|{}.!"
    escaped = ""
    for char in text:
        if char in special_chars:
            escaped += f"\\{char}"
        else:
            escaped += char
    return escaped


def format_arb_message(arb: ArbOpportunity) -> str:
    """
    Format an ArbOpportunity into a Telegram-ready MarkdownV2 message.
    """
    sport_emoji = SPORT_EMOJI.get(arb.sport_key, "🎯")
    event = escape_markdown_v2(arb.event_name)
    market = escape_markdown_v2(arb.market_key.value.upper())
    profit = escape_markdown_v2(f"{arb.profit_percent:.2f}%")
    payout = escape_markdown_v2(f"${arb.guaranteed_payout:,.2f}")
    stake_total = escape_markdown_v2(f"${arb.total_stake:,.2f}")

    lines = [
        f"🚨 *NEW ARBITRAGE FOUND* 🚨",
        f"",
        f"{sport_emoji} *{event}*",
        f"Market: {market}",
        f"",
    ]

    # Add each leg
    for i, leg in enumerate(arb.legs, 1):
        emoji = ["1️⃣", "2️⃣", "3️⃣"][i - 1] if i <= 3 else f"{i}."
        op = escape_markdown_v2(leg.operator_display or leg.operator)
        sel = escape_markdown_v2(leg.selection)
        odds = escape_markdown_v2(f"{leg.odds:.2f}")
        point_str = ""
        if leg.point is not None:
            point_str = escape_markdown_v2(f" ({leg.point:+.1f})")
        stake_amount = arb.stakes.get(leg.operator, 0)
        stake_str = escape_markdown_v2(f"${stake_amount:,.2f}")
        lines.append(f"{emoji} *{op}*: {sel}{point_str} @ {odds} — Stake: {stake_str}")

    lines.extend([
        f"",
        f"📈 *Profit: {profit}*",
        f"💰 Total Stake: {stake_total} → Payout: {payout}",
    ])

    return "\n".join(lines)


class TelegramAlertService:
    """Handles sending alerts via Telegram Bot API."""

    def __init__(self):
        self._session: Optional[aiohttp.ClientSession] = None
        self._base_url = TELEGRAM_API_BASE.format(token=settings.telegram_bot_token)
        self._chat_id = settings.telegram_chat_id
        # Deduplication: track recently sent arb IDs with timestamps
        self._sent_arbs: dict[str, float] = {}
        self._dedup_window = 300  # 5 minutes
        self._max_retries = 3

    async def _ensure_session(self):
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()

    def _dedup_key(self, arb: ArbOpportunity) -> str:
        """Create a dedup key from the arb's essential properties."""
        legs_key = "|".join(
            f"{l.operator}:{l.selection}:{l.odds}" for l in sorted(arb.legs, key=lambda x: x.operator)
        )
        return f"{arb.event_id}:{arb.market_key.value}:{legs_key}"

    def _is_duplicate(self, arb: ArbOpportunity) -> bool:
        """Check if this arb was already alerted recently."""
        key = self._dedup_key(arb)
        now = time.time()

        # Clean old entries
        self._sent_arbs = {k: v for k, v in self._sent_arbs.items() if now - v < self._dedup_window}

        return key in self._sent_arbs

    def _mark_sent(self, arb: ArbOpportunity):
        """Mark an arb as sent for dedup tracking."""
        key = self._dedup_key(arb)
        self._sent_arbs[key] = time.time()

    async def send_message(self, text: str, parse_mode: str = "MarkdownV2") -> bool:
        """
        Send a message via Telegram Bot API with retry logic.

        Returns True if the message was sent successfully.
        """
        await self._ensure_session()

        url = f"{self._base_url}/sendMessage"
        payload = {
            "chat_id": self._chat_id,
            "text": text,
            "parse_mode": parse_mode,
        }

        for attempt in range(1, self._max_retries + 1):
            try:
                async with self._session.post(url, json=payload, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                    if resp.status == 200:
                        logger.info("Telegram message sent successfully.")
                        return True
                    elif resp.status == 429:
                        # Rate limited — respect Retry-After header
                        body = await resp.json()
                        retry_after = body.get("parameters", {}).get("retry_after", 5)
                        logger.warning(f"Telegram rate limited. Retrying after {retry_after}s (attempt {attempt}).")
                        await asyncio.sleep(retry_after)
                    elif resp.status == 400:
                        # Bad request — likely formatting issue
                        body = await resp.json()
                        logger.error(f"Telegram bad request: {body.get('description', 'Unknown error')}")
                        # Fall back to plain text
                        if parse_mode == "MarkdownV2":
                            return await self.send_message(text.replace("\\", ""), parse_mode="")
                        return False
                    else:
                        body = await resp.text()
                        logger.error(f"Telegram error {resp.status}: {body}")
                        await asyncio.sleep(2 ** attempt)

            except asyncio.TimeoutError:
                logger.error(f"Telegram send timeout (attempt {attempt})")
                await asyncio.sleep(2 ** attempt)
            except aiohttp.ClientError as e:
                logger.error(f"Telegram HTTP error: {e}")
                await asyncio.sleep(2 ** attempt)

        logger.error("Failed to send Telegram message after all retries.")
        return False

    async def send_arb_alert(self, arb: ArbOpportunity) -> bool:
        """
        Send an arb opportunity alert if it hasn't been sent recently.

        Returns True if sent, False if skipped (duplicate) or failed.
        """
        if self._is_duplicate(arb):
            logger.debug(f"Skipping duplicate arb: {arb.event_name} ({arb.market_key.value})")
            return False

        message = format_arb_message(arb)
        success = await self.send_message(message)

        if success:
            self._mark_sent(arb)

        return success

    async def send_batch_alerts(self, arbs: list[ArbOpportunity]) -> int:
        """
        Send alerts for multiple arbs with throttling.

        Returns the number of successfully sent alerts.
        """
        sent_count = 0
        for arb in arbs:
            success = await self.send_arb_alert(arb)
            if success:
                sent_count += 1
            # Throttle: 20 msgs/sec to stay under Telegram's 30/sec limit
            await asyncio.sleep(0.05)

        logger.info(f"Sent {sent_count}/{len(arbs)} arb alerts via Telegram.")
        return sent_count

    async def send_status_message(self, text: str) -> bool:
        """Send a plain-text status/diagnostic message."""
        return await self.send_message(escape_markdown_v2(text))

    async def close(self):
        if self._session and not self._session.closed:
            await self._session.close()
            logger.info("TelegramAlertService session closed.")


# Singleton instance
telegram_service = TelegramAlertService()
