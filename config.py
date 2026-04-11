"""Configuration reader and validator for the MEXC Spread Bot."""

import sys
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

from dotenv import load_dotenv
import os


@dataclass(frozen=True)
class Config:
    """Bot configuration loaded from .env."""
    mexc_api_key: str
    mexc_api_secret: str
    mexc_base_url: str
    default_pair: str
    pairs: tuple                # tuple of pair strings, e.g. ("BTCUSDT", "ETHUSDT")
    buy_trade_percent: Decimal
    sell_trade_percent: Decimal
    buy_retry_timeout: int      # milliseconds
    sell_retry_timeout: int     # milliseconds
    min_spread_pct: Decimal


def load_config(env_path: str = ".env") -> Config:
    """Load and validate configuration from .env file."""
    env_file = Path(env_path)
    if not env_file.exists():
        print(f"[ERROR] .env file not found at: {env_file.resolve()}")
        print("Copy .env.example to .env and fill in your API credentials.")
        sys.exit(1)

    load_dotenv(env_file)

    required_vars = {
        "MEXC_API_KEY": "MEXC API Key",
        "MEXC_API_SECRET": "MEXC API Secret",
    }

    missing = []
    for var, label in required_vars.items():
        if not os.getenv(var):
            missing.append(f"  - {var} ({label})")

    if missing:
        print("[ERROR] Missing required environment variables:")
        print("\n".join(missing))
        sys.exit(1)

    # Parse PAIRS list (comma-separated), fallback to DEFAULT_PAIR
    default_pair = os.getenv("DEFAULT_PAIR", "BTCUSDT").upper()
    pairs_raw = os.getenv("PAIRS", "").strip()
    if pairs_raw:
        pairs = tuple(p.strip().upper() for p in pairs_raw.split(",") if p.strip())
    else:
        pairs = (default_pair,)

    if not pairs:
        print("[ERROR] No trading pairs configured. Set PAIRS or DEFAULT_PAIR in .env.")
        sys.exit(1)

    try:
        config = Config(
            mexc_api_key=os.getenv("MEXC_API_KEY", ""),
            mexc_api_secret=os.getenv("MEXC_API_SECRET", ""),
            mexc_base_url=os.getenv("MEXC_BASE_URL", "https://api.mexc.com").rstrip("/"),
            default_pair=default_pair,
            pairs=pairs,
            buy_trade_percent=Decimal(os.getenv("BUY_TRADE_PERCENT", "99.5")),
            sell_trade_percent=Decimal(os.getenv("SELL_TRADE_PERCENT", "100")),
            buy_retry_timeout=int(os.getenv("BUY_RETRY_TIMEOUT", "5000")),
            sell_retry_timeout=int(os.getenv("SELL_RETRY_TIMEOUT", "5000")),
            min_spread_pct=Decimal(os.getenv("MIN_SPREAD_PCT", "0.01")),
        )
    except (ValueError, TypeError) as e:
        print(f"[ERROR] Invalid config value: {e}")
        sys.exit(1)

    # Validate ranges
    if not (Decimal("0") < config.buy_trade_percent <= Decimal("100")):
        print("[ERROR] BUY_TRADE_PERCENT must be between 0 and 100.")
        sys.exit(1)

    if not (Decimal("0") < config.sell_trade_percent <= Decimal("100")):
        print("[ERROR] SELL_TRADE_PERCENT must be between 0 and 100.")
        sys.exit(1)

    if config.buy_retry_timeout < 1000:
        print("[ERROR] BUY_RETRY_TIMEOUT must be at least 1000ms.")
        sys.exit(1)

    if config.sell_retry_timeout < 1000:
        print("[ERROR] SELL_RETRY_TIMEOUT must be at least 1000ms.")
        sys.exit(1)

    if config.min_spread_pct < Decimal("0"):
        print("[ERROR] MIN_SPREAD_PCT must be >= 0.")
        sys.exit(1)

    return config
