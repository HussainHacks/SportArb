"""
Sports Arbitrage Odds Scanner — Configuration

Loads settings from environment variables / .env file.
"""

from pydantic_settings import BaseSettings
from pydantic import Field
from typing import Set


class Settings(BaseSettings):
    # --- API Keys ---
    odds_api_key: str = Field(..., description="The Odds API key")
    telegram_bot_token: str = Field(..., description="Telegram bot token from @BotFather")
    telegram_chat_id: str = Field(..., description="Telegram chat ID to send alerts to")

    # --- Infrastructure ---
    redis_url: str = Field("redis://localhost:6379/0")
    database_url: str = Field(
        "postgresql+asyncpg://sportsarb:sportsarb_dev@localhost:5432/sportsarb"
    )

    # --- Scanner Tuning ---
    poll_interval_seconds: int = Field(30, description="Seconds between polling cycles (legacy flat mode)")
    default_stake: float = Field(1000.0, description="Default total stake for sizing")
    min_arb_percent: float = Field(0.5, description="Minimum arb % to alert on")
    max_arb_percent: float = Field(20.0, description="Maximum arb % (palp filter)")
    stale_odds_seconds: int = Field(60, description="Max age of odds before considered stale")

    # --- Adaptive Scheduler ---
    poll_prematch_seconds: int = Field(60, description="Prematch polling interval")
    poll_live_seconds: int = Field(15, description="Live event polling interval")
    poll_near_start_seconds: int = Field(15, description="Near-start polling interval")
    poll_burst_seconds: int = Field(8, description="Burst mode polling interval")
    poll_backoff_seconds: int = Field(300, description="Backoff interval for empty sports")
    burst_duration_seconds: int = Field(120, description="How long burst mode lasts")
    near_start_threshold_minutes: int = Field(30, description="Minutes before start to escalate polling")

    # --- Odds-API.io (fast feed) ---
    odds_api_io_key: str = Field("", description="Odds-API.io API key")
    odds_api_io_bookmakers: list[str] = Field(
        default=["Circa", "Bally Bet", "DraftKings", "FanDuel", "Bet365"],
        description="Bookmakers to request from Odds-API.io (display names)",
    )
    odds_api_io_mode: str = Field("rest", description="Feed mode: rest or websocket")
    odds_api_io_sports: list[str] = Field(
        default=["basketball"],
        description="Sport slugs to scan via Odds-API.io (e.g. basketball, ice-hockey, football, american-football)",
    )

    # --- Iowa-Legal Operator Whitelist ---
    # Only odds from these operators will be processed
    iowa_legal_operators: Set[str] = Field(
        default={
            "draftkings",
            "fanduel",
            "betmgm",
            "williamhill_us",    # Caesars (API key in The Odds API)
            "fanatics",
            "betrivers",
            "bet365",
            "circasports",       # Circa Sports
            "ballybet",          # Bally Bet
            # "hardrockbet",     # Hard Rock — disabled: 77% phantom arbs, 0% survival
            "espnbet",           # ESPN BET
            "qsportsbook",       # Q Sportsbook (if available)
            "sporttrade",        # Sporttrade exchange
        },
        description="The Odds API bookmaker keys for Iowa-legal operators",
    )

    # --- Iowa College Team Blacklist ---
    # Player props involving these teams must be filtered out
    iowa_college_blacklist: Set[str] = Field(
        default={
            "iowa hawkeyes",
            "iowa state cyclones",
            "northern iowa panthers",
            "uni panthers",
            "drake bulldogs",
        },
        description="Iowa college teams whose player props are prohibited",
    )

    # --- The Odds API Configuration ---
    odds_api_base_url: str = "https://api.the-odds-api.com/v4"
    odds_api_regions: str = "us"
    odds_api_markets: str = "h2h,spreads,totals"
    odds_api_odds_format: str = "decimal"

    # --- Supported Sports ---
    # The Odds API sport keys to poll
    target_sports: list[str] = Field(
        default=[
            "americanfootball_nfl",
            "americanfootball_ncaaf",
            "basketball_nba",
            "basketball_ncaab",
            "baseball_mlb",
            "icehockey_nhl",
            "soccer_usa_mls",
            "soccer_epl",
            "soccer_uefa_champs_league",
        ],
        description="Sports to scan for arbitrage",
    )

    model_config = {
        "env_file": ".env",
        "env_file_encoding": "utf-8",
        "case_sensitive": False,
    }


# Singleton settings instance
settings = Settings()
