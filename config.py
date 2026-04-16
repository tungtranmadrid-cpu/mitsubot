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
    ban_pairs: frozenset        # pairs forbidden from trading (filtered out everywhere)
    buy_trade_percent: Decimal
    sell_trade_percent: Decimal
    buy_retry_timeout: int      # milliseconds
    sell_retry_timeout: int     # milliseconds
    min_spread_pct: Decimal
    buy_max_price_drift_pct: Decimal   # stop retrying partial buy if price drifted more than this %
    buy_max_retries: int               # max partial-fill retry iterations (hard safety cap)
    spread_history_size: int           # rolling window for avg bid-ask spread per pair
    max_spread_pct: Decimal            # skip if spread > this % (too wide = illiquid/dangerous)
    min_bid_depth_multiplier: Decimal  # bid depth (top 5) must be >= X × order size
    pair_cooldown_losses: int          # consecutive losses on a pair before cooldown
    pair_cooldown_seconds: int         # how long to ban a pair after consecutive losses
    max_volatility_pct: Decimal        # skip if 5-min price range > this %
    ma_fast_period: int                # MA fast period for trend filter (e.g. 5)
    ma_slow_period: int                # MA slow period for trend filter (e.g. 20)
    ma_kline_interval: str             # kline interval for MA calculation (e.g. "5m")


def load_config(env_path: str = ".env") -> Config:
    """Load and validate configuration.

    Loads `.env` if it exists (local dev). On Railway, env vars are injected
    by the platform and the file is absent — that's fine.
    """
    env_file = Path(env_path)
    if env_file.exists():
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

    # Pairs: persisted JSON store wins over env (auto-refresher writes to it).
    from pairs_store import load_pairs as load_saved_pairs

    default_pair = os.getenv("DEFAULT_PAIR", "BTCUSDT").upper()

    banpair_raw = os.getenv("BANPAIR", "").strip()
    ban_pairs = frozenset(
        p.strip().upper() for p in banpair_raw.split(",") if p.strip()
    )

    saved = load_saved_pairs()
    if saved:
        pairs = tuple(p for p in saved if p not in ban_pairs)
    else:
        pairs_raw = os.getenv("PAIRS", "").strip()
        if pairs_raw:
            pairs = tuple(
                p.strip().upper()
                for p in pairs_raw.split(",")
                if p.strip() and p.strip().upper() not in ban_pairs
            )
        else:
            pairs = () if default_pair in ban_pairs else (default_pair,)

    if not pairs:
        print("[ERROR] No trading pairs configured. Set PAIRS or DEFAULT_PAIR (and check BANPAIR).")
        sys.exit(1)

    try:
        config = Config(
            mexc_api_key=os.getenv("MEXC_API_KEY", ""),
            mexc_api_secret=os.getenv("MEXC_API_SECRET", ""),
            mexc_base_url=os.getenv("MEXC_BASE_URL", "https://api.mexc.com").rstrip("/"),
            default_pair=default_pair,
            pairs=pairs,
            ban_pairs=ban_pairs,
            buy_trade_percent=Decimal(os.getenv("BUY_TRADE_PERCENT", "99.5")),
            sell_trade_percent=Decimal(os.getenv("SELL_TRADE_PERCENT", "100")),
            buy_retry_timeout=int(os.getenv("BUY_RETRY_TIMEOUT", "5000")),
            sell_retry_timeout=int(os.getenv("SELL_RETRY_TIMEOUT", "5000")),
            min_spread_pct=Decimal(os.getenv("MIN_SPREAD_PCT", "0.01")),
            buy_max_price_drift_pct=Decimal(os.getenv("BUY_MAX_PRICE_DRIFT_PCT", "0.3")),
            buy_max_retries=int(os.getenv("BUY_MAX_RETRIES", "10")),
            spread_history_size=int(os.getenv("SPREAD_HISTORY_SIZE", "30")),
            max_spread_pct=Decimal(os.getenv("MAX_SPREAD_PCT", "2.0")),
            min_bid_depth_multiplier=Decimal(os.getenv("MIN_BID_DEPTH_MULTIPLIER", "3.0")),
            pair_cooldown_losses=int(os.getenv("PAIR_COOLDOWN_LOSSES", "3")),
            pair_cooldown_seconds=int(os.getenv("PAIR_COOLDOWN_SECONDS", "300")),
            max_volatility_pct=Decimal(os.getenv("MAX_VOLATILITY_PCT", "5.0")),
            ma_fast_period=int(os.getenv("MA_FAST_PERIOD", "5")),
            ma_slow_period=int(os.getenv("MA_SLOW_PERIOD", "20")),
            ma_kline_interval=os.getenv("MA_KLINE_INTERVAL", "5m"),
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
