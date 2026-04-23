"""Entry point for the MEXC Spread Trading Bot."""

import logging
import signal
import sys
import threading
import time
from decimal import Decimal

from config import load_config
from mexc_api import MexcAPI
from order_manager import OrderManager
from pnl_tracker import PnLTracker
from trading_engine import TradingEngine
from models import BotState, BotStatus
from display import print_log, print_summary, print_dashboard, console
from spread_top_pairs import PairRefresher, scan_top_spread_pairs
from pairs_store import save_pairs

# ── Logging setup ────────────────────────────────────────────────────

import os as _os

_log_handlers = [logging.StreamHandler(sys.stdout)]
if _os.getenv("LOG_FILE", "").strip():
    _log_handlers.append(logging.FileHandler(_os.environ["LOG_FILE"], encoding="utf-8"))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    datefmt="%H:%M:%S",
    handlers=_log_handlers,
)
logger = logging.getLogger("main")

# ── Globals for shutdown handler ─────────────────────────────────────

_engine: TradingEngine = None
_state: BotState = None
_api: MexcAPI = None
_tracker: PnLTracker = None
_refresher: PairRefresher = None


def shutdown_handler(signum, frame):
    """Graceful shutdown on Ctrl+C."""
    global _engine, _state, _api, _tracker, _refresher

    if _state is None:
        sys.exit(0)

    if _state.is_shutting_down:
        # Second Ctrl+C → force exit
        print_log("Force exit.", "error")
        sys.exit(1)

    _state.is_shutting_down = True
    _state.status = BotStatus.SHUTTING_DOWN

    # Stop pair refresher
    if _refresher:
        _refresher.stop()

    console.print("\n[bold yellow]Shutting down...[/]")

    # Cancel any open order
    if _state.current_order_id:
        try:
            print_log(f"Cancelling open order {_state.current_order_id}...", "warning")
            _api.cancel_order(_state.pair, _state.current_order_id)
        except Exception:
            pass

    # If holding coin, ask user
    if _state.filled_qty > 0 and _state.avg_buy_price > 0:
        coin_name = _state.pair.replace("USDT", "")
        console.print(
            f"\n[bold yellow]Holding {float(_state.filled_qty)} {coin_name}. "
            f"Sell at market? (y/n, 10s timeout)[/]"
        )

        # Non-blocking input with timeout
        answer = [None]

        def get_input():
            try:
                answer[0] = input().strip().lower()
            except EOFError:
                pass

        t = threading.Thread(target=get_input, daemon=True)
        t.start()
        t.join(timeout=10)

        if answer[0] == "y":
            try:
                print_log(f"Placing market sell for {float(_state.filled_qty)} {coin_name}...", "warning")
                _api.place_market_sell(_state.pair, _state.filled_qty)
                print_log("Market sell placed.", "success")
            except Exception as e:
                print_log(f"Market sell failed: {e}", "error")
        else:
            print_log(f"Keeping {float(_state.filled_qty)} {coin_name}.", "info")

    # Print session summary
    if _tracker:
        print_summary(_tracker)

    print_log("Bot stopped.", "info")
    sys.exit(0)


def main():
    global _engine, _state, _api, _tracker, _refresher

    # Register shutdown handler
    signal.signal(signal.SIGINT, shutdown_handler)
    signal.signal(signal.SIGTERM, shutdown_handler)

    console.print("[bold cyan]MEXC Spread Trading Bot[/]")
    console.print("[dim]Loading configuration...[/]\n")

    # Step 1: Initialize
    config = load_config()

    _api = MexcAPI(config)

    # If no pairs were loaded (first boot, no data/pairs.json, no PAIRS in .env),
    # run a blocking scan now so the engine has something to start with.
    if not config.pairs:
        print_log("No pairs configured — running initial scan (this may take a minute)...", "warning")
        try:
            initial_pairs = scan_top_spread_pairs(
                top_n=15,
                min_volume_1h=30_000,
                ban_pairs=config.ban_pairs,
            )
            if initial_pairs:
                save_pairs(initial_pairs)
                print_log(f"Initial scan complete: {', '.join(initial_pairs[:5])}...", "success")
                # Reload config with the freshly scanned pairs
                config = load_config()
            else:
                print_log("Initial scan returned no pairs. Check network/API.", "error")
                sys.exit(1)
        except Exception as e:
            print_log(f"Initial scan failed: {e}", "error")
            sys.exit(1)

    # Fetch exchange info for all configured pairs
    pairs_info = {}
    print_log(f"Fetching exchange info for {len(config.pairs)} pairs...", "info")
    for symbol in config.pairs:
        try:
            pi = _api.get_exchange_info(symbol)
            pairs_info[symbol] = pi
            print_log(
                f"  {pi.symbol} | StepSize: {pi.step_size} | "
                f"TickSize: {pi.tick_size} | MinNotional: {pi.min_notional}",
                "info",
            )
        except Exception as e:
            print_log(f"  {symbol} — SKIPPED ({e})", "warning")

    if not pairs_info:
        print_log("No valid pairs found. Check your PAIRS config.", "error")
        sys.exit(1)

    print_log(f"Loaded {len(pairs_info)}/{len(config.pairs)} pairs.", "success")

    # Use first valid pair as initial pair
    first_symbol = next(iter(pairs_info))
    pair_info = pairs_info[first_symbol]

    # Fetch initial balance
    print_log("Fetching account balance...", "info")
    try:
        usdt_balance = _api.get_balance("USDT")
    except Exception as e:
        print_log(f"Failed to fetch balance: {e}", "error")
        sys.exit(1)

    if usdt_balance <= 0:
        print_log("No USDT balance available. Fund your account first.", "error")
        sys.exit(1)

    # Initialize state
    _state = BotState(
        pair=first_symbol,
        initial_balance=usdt_balance,
        usdt_balance=usdt_balance,
        coin_balance=Decimal("0"),
    )

    # Initialize tracker
    _tracker = PnLTracker(initial_balance=usdt_balance)

    # Initialize order manager
    order_mgr = OrderManager(_api, config, pair_info)

    # Initialize engine
    _engine = TradingEngine(
        config=config,
        api=_api,
        order_mgr=order_mgr,
        tracker=_tracker,
        state=_state,
        pair_info=pair_info,
        pairs_info=pairs_info,
    )

    print_log(
        f"Bot started | {len(pairs_info)} pairs | "
        f"Balance: ${float(usdt_balance):,.2f} USDT",
        "success",
    )
    print_log(
        f"Config: BuyPct={config.buy_trade_percent}% | SellPct={config.sell_trade_percent}% | "
        f"BuyTimeout={config.buy_retry_timeout}ms | "
        f"SellTimeout={config.sell_retry_timeout}ms | "
        f"MinSpread={config.min_spread_pct}%",
        "info",
    )
    console.print()

    # Start background pair refresher (scans MEXC every 1 hour)
    _refresher = PairRefresher(
        engine=_engine,
        api=_api,
        interval_seconds=3600,   # 1 hour
        top_n=15,                # top 15 pairs by spread
        min_volume_1h=30_000,    # min 1h volume 30k USDT (low enough to catch high-spread small-caps)
        ban_pairs=config.ban_pairs,
    )
    _refresher.start()
    print_log("Pair refresher started (every 1 hour, top 15 by spread, min 1h vol $30K)", "info")

    # Run the bot
    _engine.run()


if __name__ == "__main__":
    main()
