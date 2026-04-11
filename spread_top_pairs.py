from __future__ import annotations

"""
Scan exchanges (MEXC / Hyperliquid) for top spread pairs and auto-refresh the bot's trading pairs.

Features:
- Fetches all USDT pairs from MEXC or Hyperliquid
- Filters by estimated 1h volume > threshold (default 150k USDT)
- Samples orderbook spread for candidates
- Returns top N pairs sorted by spread, volume, or combined score
- Can update .env and notify the running bot
- Standalone CLI with rich table output
"""

import ccxt
import logging
import os
import sys
import time
import threading
from pathlib import Path

if os.name == "nt":
    os.system("chcp 65001 > nul")

logger = logging.getLogger("spread_scanner")

# ── Colors for standalone mode ──────────────────────────────────────

GREEN = "\033[92m"
RED = "\033[91m"
CYAN = "\033[96m"
YELLOW = "\033[93m"
BOLD = "\033[1m"
RESET = "\033[0m"


def fmt(v: float) -> str:
    if v is None:
        return "—"
    try:
        v = float(v)
    except Exception:
        return str(v)
    abs_v = abs(v)
    if abs_v >= 1_000_000_000:
        return f"{v / 1_000_000_000:,.3f}B"
    if abs_v >= 1_000_000:
        return f"{v / 1_000_000:,.3f}M"
    if abs_v >= 1_000:
        return f"{v / 1_000:,.3f}K"
    return f"{v:,.8f}".rstrip("0").rstrip(".")


def create_mexc_exchange():
    """Create a ccxt MEXC exchange instance."""
    ex_class = getattr(ccxt, "mexc", None)
    if ex_class is None:
        raise ValueError("ccxt does not support 'mexc' in this environment.")
    return ex_class({"enableRateLimit": True})


def create_hyperliquid_exchange():
    """Create a ccxt Hyperliquid exchange instance."""
    ex_class = getattr(ccxt, "hyperliquid", None)
    if ex_class is None:
        raise ValueError("ccxt does not support 'hyperliquid' in this environment.")
    return ex_class({"enableRateLimit": True})


def create_exchange(name: str = "mexc"):
    """Create exchange instance by name."""
    factories = {
        "mexc": create_mexc_exchange,
        "hyperliquid": create_hyperliquid_exchange,
    }
    factory = factories.get(name.lower())
    if factory is None:
        raise ValueError(f"Unsupported exchange: {name}. Supported: {list(factories.keys())}")
    return factory()


def scan_top_spread_pairs(top_n: int = 15, min_volume_1h: float = 150_000) -> list[str]:
    """
    Scan MEXC for top spread USDT pairs.

    1. Fetch all tickers, filter USDT pairs with estimated 1h volume > min_volume_1h
    2. For candidates, fetch orderbook and compute spread
    3. Return top N pair symbols (e.g. ["BTCUSDT", "ETHUSDT", ...]) sorted by spread desc

    Returns list of symbol strings (without '/'), e.g. ["ARIAUSDT", "PEPEUSDT"]
    """
    ex = create_mexc_exchange()
    ex.load_markets()

    logger.info("Fetching tickers from MEXC...")
    tickers = ex.fetch_tickers()

    # Step 1: Filter USDT pairs with sufficient volume
    # Estimate 1h volume as 24h volume / 24
    min_volume_24h = min_volume_1h * 24
    candidates = []

    for sym, t in tickers.items():
        if sym not in ex.markets:
            continue
        if "/" not in sym:
            continue
        base, quote = sym.split("/", 1)
        if quote != "USDT":
            continue

        qv = t.get("quoteVolume")
        try:
            qv = float(qv) if qv is not None else 0.0
        except Exception:
            qv = 0.0

        if qv >= min_volume_24h:
            candidates.append((sym, qv))

    candidates.sort(key=lambda x: x[1], reverse=True)
    logger.info(f"Found {len(candidates)} USDT pairs with est. 1h volume > ${min_volume_1h:,.0f}")

    if not candidates:
        return []

    # Step 2: Fetch orderbook for each candidate and compute spread
    spread_data = []
    for sym, qv in candidates:
        try:
            ob = ex.fetch_order_book(sym, limit=5)
            bids = ob.get("bids") or []
            asks = ob.get("asks") or []
            if not bids or not asks:
                continue
            bid = bids[0][0]
            ask = asks[0][0]
            if bid <= 0:
                continue
            spread_pct = (ask - bid) / bid * 100.0
            vol_1h_est = qv / 24.0
            spread_data.append({
                "symbol": sym,
                "spread_pct": spread_pct,
                "volume_24h": qv,
                "volume_1h_est": vol_1h_est,
                "bid": bid,
                "ask": ask,
            })
        except Exception as e:
            logger.debug(f"Error fetching orderbook for {sym}: {e}")
            continue

    # Step 3: Sort by spread and take top N
    spread_data.sort(key=lambda x: x["spread_pct"], reverse=True)
    top_pairs = spread_data[:top_n]

    # Convert to MEXC symbol format (no slash): "BTC/USDT" -> "BTCUSDT"
    result = []
    for item in top_pairs:
        mexc_symbol = item["symbol"].replace("/", "")
        result.append(mexc_symbol)
        logger.info(
            f"  {mexc_symbol:<14} Spread: {item['spread_pct']:.4f}%  "
            f"Vol1h: ${item['volume_1h_est']:,.0f}  Vol24h: ${item['volume_24h']:,.0f}"
        )

    return result


def scan_hyperliquid_top_pairs(
    top_n: int = 20,
    min_volume_1h: float = 50_000,
    sort_by: str = "score",
) -> list[dict]:
    """
    Scan Hyperliquid for top spread+volume pairs.

    Hyperliquid is a perp DEX – all pairs are perpetual futures (USDC-settled).

    Args:
        top_n: Number of top pairs to return
        min_volume_1h: Minimum estimated 1h volume in USD
        sort_by: "spread", "volume", or "score" (spread * log(volume))

    Returns list of dicts with full info for display, sorted by chosen metric.
    """
    import math

    ex = create_hyperliquid_exchange()
    ex.load_markets()

    logger.info("Fetching tickers from Hyperliquid...")
    tickers = ex.fetch_tickers()

    # Step 1: Filter pairs with sufficient volume
    min_volume_24h = min_volume_1h * 24
    candidates = []

    for sym, t in tickers.items():
        if sym not in ex.markets:
            continue
        market = ex.markets[sym]

        # Get quote volume (USDC on Hyperliquid)
        qv = t.get("quoteVolume")
        try:
            qv = float(qv) if qv is not None else 0.0
        except Exception:
            qv = 0.0

        if qv >= min_volume_24h:
            candidates.append((sym, qv, market))

    candidates.sort(key=lambda x: x[1], reverse=True)
    logger.info(
        f"Found {len(candidates)} Hyperliquid pairs with est. 1h volume > ${min_volume_1h:,.0f}"
    )

    if not candidates:
        return []

    # Step 2: Fetch orderbook and compute spread
    spread_data = []
    for sym, qv, market in candidates:
        try:
            ob = ex.fetch_order_book(sym, limit=5)
            bids = ob.get("bids") or []
            asks = ob.get("asks") or []
            if not bids or not asks:
                continue
            bid = bids[0][0]
            ask = asks[0][0]
            if bid <= 0:
                continue
            spread_pct = (ask - bid) / bid * 100.0
            vol_1h_est = qv / 24.0

            # Combined score: spread% * log10(volume_1h)
            # This favors pairs that are BOTH high-spread AND high-volume
            score = spread_pct * math.log10(max(vol_1h_est, 1))

            # Bid depth (top 5 levels in USD)
            bid_depth = sum(b[0] * b[1] for b in bids[:5])
            ask_depth = sum(a[0] * a[1] for a in asks[:5])

            base = sym.split("/")[0] if "/" in sym else sym.replace("USDT", "").replace("USDC", "")
            spread_data.append({
                "symbol": sym,
                "base": base,
                "spread_pct": spread_pct,
                "volume_24h": qv,
                "volume_1h_est": vol_1h_est,
                "bid": bid,
                "ask": ask,
                "bid_depth": bid_depth,
                "ask_depth": ask_depth,
                "score": score,
                "market_type": market.get("type", "swap"),
            })
        except Exception as e:
            logger.debug(f"Error fetching orderbook for {sym}: {e}")
            continue

    # Step 3: Sort by chosen metric
    sort_keys = {
        "spread": lambda x: x["spread_pct"],
        "volume": lambda x: x["volume_1h_est"],
        "score": lambda x: x["score"],
    }
    key_fn = sort_keys.get(sort_by, sort_keys["score"])
    spread_data.sort(key=key_fn, reverse=True)

    return spread_data[:top_n]


def scan_exchange_top_pairs(
    exchange: str = "hyperliquid",
    top_n: int = 20,
    min_volume_1h: float = 50_000,
    sort_by: str = "score",
) -> list[dict]:
    """
    Unified scanner that works across exchanges.
    Returns list of dicts with spread/volume info.
    """
    if exchange.lower() == "hyperliquid":
        return scan_hyperliquid_top_pairs(top_n, min_volume_1h, sort_by)

    # Fallback: use the original MEXC scanner and wrap results
    pairs = scan_top_spread_pairs(top_n, min_volume_1h)
    return [{"symbol": p, "base": p.replace("USDT", ""), "spread_pct": 0, "volume_24h": 0,
             "volume_1h_est": 0, "bid": 0, "ask": 0, "score": 0} for p in pairs]


def print_table(data: list[dict], exchange: str, sort_by: str) -> None:
    """Print a formatted table of scan results."""
    if not data:
        print(f"{RED}No pairs found matching criteria.{RESET}")
        return

    sort_label = {"spread": "Spread %", "volume": "Volume 1h", "score": "Score (spread×log_vol)"}
    print(f"\n{BOLD}{CYAN}{'═' * 100}{RESET}")
    print(f"{BOLD}  {exchange.upper()} — Top {len(data)} Pairs by {sort_label.get(sort_by, sort_by)}{RESET}")
    print(f"{CYAN}{'═' * 100}{RESET}")

    # Header
    print(
        f"  {'#':>3}  {'Pair':<16} {'Spread%':>10} {'Bid':>14} {'Ask':>14} "
        f"{'Vol 1h':>12} {'Vol 24h':>12} {'Bid Dep.':>10} {'Ask Dep.':>10} {'Score':>8}"
    )
    print(f"  {CYAN}{'─' * 96}{RESET}")

    for i, d in enumerate(data, 1):
        spread_color = GREEN if d["spread_pct"] >= 0.1 else YELLOW if d["spread_pct"] >= 0.05 else RESET
        vol_color = GREEN if d["volume_1h_est"] >= 500_000 else YELLOW if d["volume_1h_est"] >= 100_000 else RESET

        print(
            f"  {i:>3}  {d['base']:<16} "
            f"{spread_color}{d['spread_pct']:>9.4f}%{RESET} "
            f"{fmt(d['bid']):>14s} {fmt(d['ask']):>14s} "
            f"{vol_color}{'$' + fmt(d['volume_1h_est']):>11s}{RESET} "
            f"{'$' + fmt(d['volume_24h']):>11s} "
            f"{'$' + fmt(d.get('bid_depth', 0)):>9s} "
            f"{'$' + fmt(d.get('ask_depth', 0)):>9s} "
            f"{d.get('score', 0):>8.2f}"
        )

    print(f"  {CYAN}{'─' * 96}{RESET}\n")


def update_env_pairs(pairs: list[str], env_path: str = ".env") -> None:
    """Update the pairs= line in the .env file."""
    env_file = Path(env_path)
    if not env_file.exists():
        logger.error(f".env file not found at {env_file.resolve()}")
        return

    content = env_file.read_text(encoding="utf-8")
    new_pairs_line = f"PAIRS={','.join(pairs)}"

    lines = content.splitlines()
    updated = False
    for i, line in enumerate(lines):
        if line.strip().upper().startswith("PAIRS="):
            lines[i] = new_pairs_line
            updated = True
            break

    if not updated:
        lines.append(new_pairs_line)

    env_file.write_text("\n".join(lines) + "\n", encoding="utf-8")
    logger.info(f"Updated .env pairs: {new_pairs_line}")


class PairRefresher:
    """
    Background thread that scans MEXC for top spread pairs every interval
    and hot-reloads them into the running bot.
    """

    def __init__(
        self,
        engine,
        api,
        interval_seconds: int = 3600,
        top_n: int = 15,
        min_volume_1h: float = 150_000,
    ):
        self.engine = engine
        self.api = api
        self.interval = interval_seconds
        self.top_n = top_n
        self.min_volume_1h = min_volume_1h
        self._stop_event = threading.Event()
        self._thread = None

    def start(self):
        """Start the background refresh thread."""
        self._thread = threading.Thread(target=self._run_loop, daemon=True)
        self._thread.start()
        logger.info(
            f"PairRefresher started: top {self.top_n} pairs, "
            f"min 1h vol ${self.min_volume_1h:,.0f}, interval {self.interval}s"
        )

    def stop(self):
        """Signal the thread to stop."""
        self._stop_event.set()

    def _run_loop(self):
        """Main loop: scan immediately, then every interval."""
        # Run first scan immediately
        self._do_refresh()

        while not self._stop_event.is_set():
            # Wait for next interval (check stop every 10s)
            for _ in range(self.interval // 10):
                if self._stop_event.is_set():
                    return
                time.sleep(10)

            self._do_refresh()

    def _do_refresh(self):
        """Perform one refresh cycle."""
        try:
            from display import print_log
            print_log(
                f"[PairRefresher] Scanning MEXC for top {self.top_n} spread pairs...",
                "info",
            )

            new_pairs = scan_top_spread_pairs(
                top_n=self.top_n,
                min_volume_1h=self.min_volume_1h,
            )

            if not new_pairs:
                print_log("[PairRefresher] No pairs found, keeping current pairs.", "warning")
                return

            # Update .env file
            update_env_pairs(new_pairs)

            # Hot-reload into the running engine
            self._reload_engine_pairs(new_pairs)

            print_log(
                f"[PairRefresher] Refreshed {len(new_pairs)} pairs: {', '.join(new_pairs[:5])}...",
                "success",
            )

        except Exception as e:
            logger.exception(f"PairRefresher error: {e}")
            try:
                from display import print_log
                print_log(f"[PairRefresher] Error: {e}", "error")
            except Exception:
                pass

    def _reload_engine_pairs(self, new_pairs: list[str]):
        """Fetch exchange info for new pairs and update the engine."""
        from display import print_log

        new_pairs_info = {}
        for symbol in new_pairs:
            try:
                pi = self.api.get_exchange_info(symbol)
                new_pairs_info[symbol] = pi
            except Exception as e:
                print_log(f"[PairRefresher] Skip {symbol}: {e}", "warning")

        if not new_pairs_info:
            print_log("[PairRefresher] No valid pairs after exchange info fetch.", "warning")
            return

        # Update engine's pairs_info (thread-safe: dict replacement is atomic in CPython)
        self.engine.pairs_info = new_pairs_info
        print_log(
            f"[PairRefresher] Engine updated with {len(new_pairs_info)} pairs.",
            "success",
        )


# ── Standalone CLI mode ─────────────────────────────────────────────

def main():
    """Run as standalone script to scan exchanges for top spread+volume pairs."""
    import argparse

    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
        datefmt="%H:%M:%S",
    )

    parser = argparse.ArgumentParser(
        description="Scan exchanges for top spread + volume pairs",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=f"""
Examples:
  python spread_top_pairs.py                          # Hyperliquid, top 20, sort by score
  python spread_top_pairs.py --exchange mexc           # MEXC instead
  python spread_top_pairs.py --sort spread --top 30    # Top 30 by spread
  python spread_top_pairs.py --min-vol-1h 500000       # Higher volume filter
  python spread_top_pairs.py --sort volume             # Sort by 1h volume
  python spread_top_pairs.py --update-env              # Write results to .env
""",
    )
    parser.add_argument(
        "--exchange", "-e", type=str, default="hyperliquid",
        choices=["hyperliquid", "mexc"],
        help="Exchange to scan (default: hyperliquid)"
    )
    parser.add_argument("--top", "-n", type=int, default=20, help="Number of top pairs (default: 20)")
    parser.add_argument(
        "--min-vol-1h", type=float, default=50_000,
        help="Minimum estimated 1h volume in USD (default: 50,000)"
    )
    parser.add_argument(
        "--sort", "-s", type=str, default="score",
        choices=["spread", "volume", "score"],
        help="Sort by: spread, volume, or score (spread*log_vol, default: score)"
    )
    parser.add_argument(
        "--update-env", action="store_true",
        help="Update .env file with the top pairs"
    )
    args = parser.parse_args()

    print(f"\n{BOLD}Scanning {args.exchange.upper()} for top {args.top} pairs...{RESET}")
    print(f"Sort: {args.sort} | Min 1h vol: ${args.min_vol_1h:,.0f} USD\n")

    if args.exchange.lower() == "hyperliquid":
        data = scan_hyperliquid_top_pairs(
            top_n=args.top, min_volume_1h=args.min_vol_1h, sort_by=args.sort
        )
        print_table(data, args.exchange, args.sort)

        if data:
            pair_names = [d["base"] + "USDT" for d in data]
            print(f"  {BOLD}Symbols:{RESET} {', '.join(pair_names)}")

            if args.update_env:
                update_env_pairs(pair_names)
                print(f"\n  {GREEN}.env updated with {len(pair_names)} pairs.{RESET}")
            else:
                print(f"\n  {YELLOW}Use --update-env to write these to .env{RESET}")
    else:
        # Original MEXC mode
        pairs = scan_top_spread_pairs(top_n=args.top, min_volume_1h=args.min_vol_1h)
        if not pairs:
            print(f"{RED}No pairs found matching criteria.{RESET}")
            return
        print(f"\n{GREEN}Top {len(pairs)} pairs by spread:{RESET}")
        print(f"  {', '.join(pairs)}")
        if args.update_env:
            update_env_pairs(pairs)
            print(f"\n{GREEN}.env updated with new pairs.{RESET}")
        else:
            print(f"\n{YELLOW}Use --update-env to write these to .env{RESET}")


if __name__ == "__main__":
    main()
