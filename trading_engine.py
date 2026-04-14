"""Main trading engine implementing the bid-ask spread strategy (Steps 2-7)."""

from __future__ import annotations

import logging
import random
import time
from collections import deque
from decimal import Decimal

from config import Config
from mexc_api import MexcAPI, APIError, RateLimitError
from order_manager import OrderManager
from pnl_tracker import PnLTracker
from models import BotState, BotStatus, OrderStatus, PairInfo
from display import print_dashboard, print_log

logger = logging.getLogger("engine")


class TradingEngine:
    """Orchestrates the buy-scan-sell loop."""

    def __init__(
        self,
        config: Config,
        api: MexcAPI,
        order_mgr: OrderManager,
        tracker: PnLTracker,
        state: BotState,
        pair_info: PairInfo,
        pairs_info: dict[str, PairInfo] = None,
    ):
        self.config = config
        self.api = api
        self.order_mgr = order_mgr
        self.tracker = tracker
        self.state = state
        self.pair_info = pair_info
        self.pairs_info = pairs_info or {pair_info.symbol: pair_info}
        # Rolling spread_pct history per pair (for above-market sell pricing).
        self._spread_history: dict[str, deque] = {}

    def switch_pair(self, symbol: str) -> None:
        """Switch to a different trading pair before a new cycle."""
        new_pair_info = self.pairs_info[symbol]
        self.pair_info = new_pair_info
        self.order_mgr.pair_info = new_pair_info
        self.state.pair = symbol
        self.state.best_bid = Decimal("0")
        self.state.best_ask = Decimal("0")
        self.state.spread_pct = Decimal("0")
        self.state.vwap = Decimal("0")

    def _refresh_dashboard(self) -> None:
        """Redraw the terminal dashboard."""
        print_dashboard(self.state, self.tracker)

    def _update_prices(self, bid: Decimal, ask: Decimal) -> None:
        """Update state with current bid/ask and spread, and record spread history."""
        self.state.best_bid = bid
        self.state.best_ask = ask
        if bid > 0:
            self.state.spread_pct = ((ask - bid) / bid) * Decimal("100")
        else:
            self.state.spread_pct = Decimal("0")

        if self.state.spread_pct > 0:
            symbol = self.pair_info.symbol
            hist = self._spread_history.get(symbol)
            if hist is None:
                hist = deque(maxlen=self.config.spread_history_size)
                self._spread_history[symbol] = hist
            hist.append(self.state.spread_pct)

    def _avg_spread_pct(self) -> Decimal:
        """Average of recorded spread_pct for the current pair. Falls back to min_spread_pct."""
        hist = self._spread_history.get(self.pair_info.symbol)
        if not hist:
            return self.config.min_spread_pct
        total = sum(hist, Decimal("0"))
        return total / Decimal(len(hist))

    def _compute_vwap(self, short_window: int = 50) -> tuple[Decimal, Decimal]:
        """
        Fetch recent trades and compute two VWAPs:
        - long VWAP: all 200 trades (fair price)
        - short VWAP: last N trades (recent momentum)

        Returns (vwap_long, vwap_short). short > long = uptrend.
        """
        trades = self.api.get_recent_trades(self.pair_info.symbol, limit=200)
        if not trades:
            return Decimal("0"), Decimal("0")

        # Long VWAP (all trades)
        long_vol = Decimal("0")
        long_val = Decimal("0")
        for t in trades:
            price = Decimal(str(t["price"]))
            qty = Decimal(str(t["qty"]))
            long_val += price * qty
            long_vol += qty

        if long_vol <= 0:
            return Decimal("0"), Decimal("0")

        vwap_long = long_val / long_vol

        # Short VWAP (recent trades only — tail of the list)
        recent = trades[-short_window:]
        short_vol = Decimal("0")
        short_val = Decimal("0")
        for t in recent:
            price = Decimal(str(t["price"]))
            qty = Decimal(str(t["qty"]))
            short_val += price * qty
            short_vol += qty

        vwap_short = short_val / short_vol if short_vol > 0 else vwap_long

        return vwap_long, vwap_short

    # ── Price improvement ─────────────────────────────────────────────

    def _compute_improved_prices(self, bid: Decimal, ask: Decimal) -> tuple:
        """
        If spread > 2 ticks, improve prices by 1 tick to get price priority.
        If spread <= 2 ticks, keep original prices (FIFO queue).

        Returns (buy_price, sell_price).
        """
        tick = self.pair_info.tick_size
        spread_ticks = (ask - bid) / tick if tick > 0 else Decimal("0")

        if spread_ticks > 2:
            buy_price = bid + tick    # 1 tick above best bid → price priority
            sell_price = ask - tick   # 1 tick below best ask → price priority
            print_log(
                f"Spread={float(spread_ticks):.0f} ticks (>{2}) → "
                f"BUY {float(bid)}+tick={float(buy_price)} | "
                f"SELL {float(ask)}-tick={float(sell_price)}",
                "info",
            )
        else:
            buy_price = bid
            sell_price = ask
            print_log(
                f"Spread={float(spread_ticks):.0f} ticks (≤2) → "
                f"Keep BUY={float(bid)} SELL={float(ask)} (FIFO queue)",
                "info",
            )

        return buy_price, sell_price

    # ── Step 2: Scan with VWAP ──────────────────────────────────────

    def step_scan(self) -> bool:
        """
        Scan orderbook + recent trades for a tradeable opportunity.
        Uses VWAP as fair price reference. Only buy if bid < VWAP (buying below fair value).
        Returns True if opportunity found, False to retry.
        """
        self.state.status = BotStatus.SCANNING
        self.state.current_order_id = None

        # Fetch orderbook
        try:
            bid, ask = self.api.get_best_bid_ask(self.pair_info.symbol)
        except Exception as e:
            print_log(f"Error fetching orderbook: {e}", "error")
            time.sleep(2)
            return False

        self._update_prices(bid, ask)

        # Compute VWAP from recent trades
        try:
            vwap_long, vwap_short = self._compute_vwap()
            self.state.vwap = vwap_long
        except Exception as e:
            print_log(f"Error computing VWAP: {e}", "error")
            time.sleep(2)
            return False

        self._refresh_dashboard()

        if vwap_long <= 0:
            print_log("VWAP unavailable (no recent trades).", "warning")
            time.sleep(3)
            return False

        # Check 1: VWAP trend must be UP (short > long)
        if vwap_short <= vwap_long:
            trend_pct = float(((vwap_short - vwap_long) / vwap_long) * Decimal("100"))
            print_log(
                f"VWAP downtrend: Short({float(vwap_short)}) <= Long({float(vwap_long)}) "
                f"[{trend_pct:.4f}%]. Skipping.",
                "warning",
            )
            time.sleep(3)
            return False

        # Check 2: bid-ask spread must be sufficient
        if self.state.spread_pct < self.config.min_spread_pct:
            print_log(
                f"Spread too low: {float(self.state.spread_pct):.4f}% "
                f"(min: {float(self.config.min_spread_pct)}%)",
                "warning",
            )
            time.sleep(3)
            return False

        # Check 3: bid must be below VWAP (buying at a discount to fair value)
        if bid >= vwap_long:
            discount_pct = float(((bid - vwap_long) / vwap_long) * Decimal("100"))
            print_log(
                f"Bid ({float(bid)}) >= VWAP ({float(vwap_long)}) [+{discount_pct:.4f}%]. "
                f"No discount, skipping.",
                "warning",
            )
            time.sleep(3)
            return False

        # Check 4: ask must be above VWAP (can sell above fair value)
        if ask <= vwap_long:
            print_log(
                f"Ask ({float(ask)}) <= VWAP ({float(vwap_long)}). "
                f"Can't sell above fair value, skipping.",
                "warning",
            )
            time.sleep(3)
            return False

        trend_pct = float(((vwap_short - vwap_long) / vwap_long) * Decimal("100"))
        discount_pct = float(((vwap_long - bid) / vwap_long) * Decimal("100"))
        premium_pct = float(((ask - vwap_long) / vwap_long) * Decimal("100"))
        print_log(
            f"BID: {float(bid)} | VWAP: {float(vwap_long)} | ASK: {float(ask)} | "
            f"Trend: +{trend_pct:.4f}% | "
            f"Discount: -{discount_pct:.4f}% | Premium: +{premium_pct:.4f}%",
            "info",
        )
        return True

    # ── Step 3: Place buy ────────────────────────────────────────────

    def step_buy(self) -> dict:
        """
        Place limit buy orders until the full target quantity is filled.

        Loops on partial fills: cancels the remainder, re-quotes at the latest
        best-bid, and places a new buy for the unfilled portion. Stops early
        if the new buy price drifts more than `buy_max_price_drift_pct` above
        the initial reference price, or after `buy_max_retries` iterations.

        Returns dict with keys: success, filled_qty, avg_price, cost
        (avg_price is the weighted average across all fills).
        """
        self.state.status = BotStatus.BUYING

        # One balance fetch per buy cycle (stays within rate limits).
        try:
            usdt_balance = self.api.get_balance(self.pair_info.quote_asset)
            self.state.usdt_balance = usdt_balance
        except Exception as e:
            print_log(f"Error fetching balance: {e}", "error")
            time.sleep(2)
            return {"success": False}

        # Initial target: size at current bid.
        initial_buy_price, _ = self._compute_improved_prices(
            self.state.best_bid, self.state.best_ask
        )
        target_qty = self.order_mgr.calculate_buy_quantity(usdt_balance, initial_buy_price)
        if target_qty is None:
            print_log("Insufficient balance or quantity too small.", "error")
            time.sleep(3)
            return {"success": False, "skip_pair": True}

        max_drift = self.config.buy_max_price_drift_pct
        price_ceiling = initial_buy_price * (Decimal("1") + max_drift / Decimal("100"))

        total_filled = Decimal("0")
        total_cost = Decimal("0")
        buy_price = initial_buy_price

        def on_buy_poll(o):
            self.state.current_order_id = o.order_id
            # Live-update weighted avg for dashboard during this poll.
            poll_filled = total_filled + (o.executed_qty or Decimal("0"))
            poll_cost = total_cost + (o.cumulative_quote_qty or Decimal("0"))
            if poll_filled > 0:
                self.state.filled_qty = poll_filled
                self.state.avg_buy_price = poll_cost / poll_filled
                self.state.coin_balance = poll_filled
            self._refresh_dashboard()

        for attempt in range(1, self.config.buy_max_retries + 1):
            remaining = target_qty - total_filled
            # Clamp remaining to step size.
            remaining = self.order_mgr.calculate_sell_quantity(remaining) \
                if remaining > 0 else Decimal("0")
            if remaining is None or remaining <= 0:
                break

            # Refresh bid to requote (first attempt uses the scan's bid).
            if attempt > 1:
                try:
                    bid, ask = self.api.get_best_bid_ask(self.pair_info.symbol)
                    self._update_prices(bid, ask)
                    buy_price, _ = self._compute_improved_prices(bid, ask)
                except Exception as e:
                    print_log(f"Error refreshing bid for retry: {e}", "error")
                    time.sleep(2)
                    continue

                if buy_price > price_ceiling:
                    drift_pct = float(
                        ((buy_price - initial_buy_price) / initial_buy_price) * Decimal("100")
                    )
                    print_log(
                        f"BUY STOP | New price {float(buy_price)} drifted +{drift_pct:.4f}% "
                        f"> cap {float(max_drift)}%. Keeping partial.",
                        "warning",
                    )
                    break

            try:
                order = self.order_mgr.place_buy(buy_price, remaining)
                self.state.current_order_id = order.order_id
            except APIError as e:
                print_log(f"Failed to place buy order: {e}", "error")
                time.sleep(2)
                continue

            self._refresh_dashboard()
            print_log(
                f"BUY ORDER #{attempt} | Price: {float(buy_price)} | "
                f"Qty: {float(remaining)} | Total: ${float(buy_price * remaining)} | "
                f"OrderID: {order.order_id}",
                "info",
            )

            result = self.order_mgr.poll_order(
                order.order_id,
                self.config.buy_retry_timeout,
                on_poll=on_buy_poll,
            )

            # Accumulate anything filled.
            if result.executed_qty and result.executed_qty > 0:
                total_filled += result.executed_qty
                total_cost += result.cumulative_quote_qty or Decimal("0")

            if result.status == OrderStatus.FILLED:
                print_log(
                    f"BUY FILLED #{attempt} | Filled slice: {float(result.executed_qty)} "
                    f"@ {float(result.avg_price)}",
                    "success",
                )
                # Check if cumulative reached target.
                if total_filled >= target_qty - self.pair_info.step_size:
                    break
                continue

            if result.status == OrderStatus.PARTIALLY_FILLED:
                self.order_mgr.cancel_order(order.order_id)
                fill_pct = float(result.fill_pct)
                print_log(
                    f"BUY PARTIAL #{attempt} | Slice filled: {float(result.executed_qty)}/"
                    f"{float(remaining)} ({fill_pct:.1f}%) | Retrying remainder.",
                    "warning",
                )
                continue

            if result.status == OrderStatus.NEW:
                self.order_mgr.cancel_order(order.order_id)
                print_log(
                    f"BUY NOT FILLED #{attempt} | Cancelled, retrying with fresh quote.",
                    "warning",
                )
                continue

            # Canceled / Rejected / Expired externally — bail with whatever we have.
            print_log(
                f"Buy slice ended with status: {result.status.value}. Stopping retries.",
                "warning",
            )
            break

        if total_filled <= 0:
            return {"success": False}

        avg_price = total_cost / total_filled
        self.state.filled_qty = total_filled
        self.state.avg_buy_price = avg_price
        self.state.coin_balance = total_filled

        self.tracker.start_trade(avg_price, total_filled, total_cost)

        fill_ratio = float(total_filled / target_qty * Decimal("100"))
        print_log(
            f"BUY COMPLETE | Filled: {float(total_filled)}/{float(target_qty)} "
            f"({fill_ratio:.1f}%) | Avg: {float(avg_price)} | Cost: ${float(total_cost)}",
            "success",
        )

        return {
            "success": True,
            "filled_qty": total_filled,
            "avg_price": avg_price,
            "cost": total_cost,
        }

    # ── Step 5: Place sell ───────────────────────────────────────────

    def step_sell(self, filled_qty: Decimal, avg_buy_price: Decimal) -> dict:
        """
        Place a limit sell at current best ask.
        Returns dict with keys: action (completed/partial/not_filled/holding)
        """
        self.state.status = BotStatus.SELLING

        # Refresh orderbook for current best ask
        try:
            bid, current_ask = self.api.get_best_bid_ask(self.pair_info.symbol)
            self._update_prices(bid, current_ask)
        except Exception as e:
            print_log(f"Error fetching orderbook for sell: {e}", "error")
            time.sleep(2)
            return {"action": "retry"}

        # Compute improved sell price (ask - 1 tick if spread wide enough)
        _, improved_sell = self._compute_improved_prices(bid, current_ask)
        sell_price = self.order_mgr.truncate_price(improved_sell)

        # If the market has moved against us (ask <= buy), don't wait for
        # price to recover. Place a passive limit sell at buy + avg spread
        # and hold the order without cancel/re-place until it fills.
        above_market = False
        if sell_price <= avg_buy_price:
            avg_spread = self._avg_spread_pct()
            target = avg_buy_price * (Decimal("1") + avg_spread / Decimal("100"))
            sell_price = self.order_mgr.truncate_price(target)
            # Ensure strictly above buy price after truncation (bump by 1 tick if needed).
            if sell_price <= avg_buy_price:
                sell_price = self.order_mgr.truncate_price(
                    avg_buy_price + self.pair_info.tick_size
                )
            above_market = True
            print_log(
                f"ABOVE-MARKET SELL | Ask {float(current_ask)} <= Buy {float(avg_buy_price)} | "
                f"Placing passive sell at {float(sell_price)} (buy + avg spread "
                f"{float(avg_spread):.4f}%)",
                "warning",
            )

        # Calculate sell quantity
        sell_qty = self.order_mgr.calculate_sell_quantity(filled_qty)
        if sell_qty is None:
            print_log("Sell quantity too small for limit order. Using market sell.", "warning")
            return {"action": "market_sell_remaining"}

        expected_pnl = (sell_price - avg_buy_price) * sell_qty

        # Place limit sell at truncated price
        try:
            order = self.order_mgr.place_sell(sell_price, sell_qty)
            self.state.current_order_id = order.order_id
        except APIError as e:
            print_log(f"Failed to place sell order: {e}", "error")
            time.sleep(2)
            return {"action": "retry"}

        self._refresh_dashboard()

        print_log(
            f"SELL ORDER | Price: {float(sell_price)} | "
            f"Qty: {float(sell_qty)} | Expected PnL: +${float(expected_pnl):,.4f} | "
            f"OrderID: {order.order_id}",
            "info",
        )

        # ── Step 6: Poll sell order ─────────────────────────────────

        def on_sell_poll(o):
            self.state.current_order_id = o.order_id
            self._refresh_dashboard()

        result = self.order_mgr.poll_order(
            order.order_id,
            self.config.sell_retry_timeout,
            on_poll=on_sell_poll,
        )

        # Above-market sell: keep polling the SAME order indefinitely — don't
        # cancel and re-place. We accept partial fills as they come.
        while above_market and result.status == OrderStatus.NEW:
            if self.state.is_shutting_down:
                break
            print_log(
                f"ABOVE-MARKET SELL | Still open at {float(sell_price)} | "
                f"Continuing to wait without refresh...",
                "info",
            )
            result = self.order_mgr.poll_order(
                order.order_id,
                self.config.sell_retry_timeout,
                on_poll=on_sell_poll,
            )

        # a) Fully filled
        if result.status == OrderStatus.FILLED:
            sold_qty = result.executed_qty
            avg_sell_price = result.avg_price
            revenue = result.cumulative_quote_qty

            pnl = self.tracker.complete_sell(
                avg_sell_price, sold_qty, revenue, avg_buy_price
            )

            pnl_pct = float(((avg_sell_price - avg_buy_price) / avg_buy_price) * 100)
            print_log(
                f"SELL FILLED | Price: {float(avg_sell_price)} | "
                f"PnL: +${float(pnl):,.4f} (+{pnl_pct:.4f}%)",
                "success",
            )

            self.state.filled_qty = Decimal("0")
            self.state.avg_buy_price = Decimal("0")
            self.state.coin_balance = Decimal("0")
            self.state.session_pnl = self.tracker.session_pnl
            self.state.session_pnl_pct = self.tracker.session_pnl_pct
            self.state.total_trades = self.tracker.trade_count

            return {"action": "completed"}

        # b) Partially filled
        if result.status == OrderStatus.PARTIALLY_FILLED:
            self.order_mgr.cancel_order(order.order_id)

            sold_qty = result.executed_qty
            avg_sell_price = result.avg_price
            revenue = result.cumulative_quote_qty
            remaining = filled_qty - sold_qty

            pnl = self.tracker.complete_sell(
                avg_sell_price, sold_qty, revenue, avg_buy_price, is_partial=True
            )

            print_log(
                f"SELL PARTIAL | Sold: {float(sold_qty)}/{float(sell_qty)} | "
                f"Remaining: {float(remaining)} | Rescanning.",
                "warning",
            )

            self.state.filled_qty = remaining
            self.state.coin_balance = remaining
            self.state.session_pnl = self.tracker.session_pnl
            self.state.session_pnl_pct = self.tracker.session_pnl_pct

            return {"action": "partial", "remaining_qty": remaining}

        # c) Not filled
        if result.status == OrderStatus.NEW:
            self.order_mgr.cancel_order(order.order_id)
            print_log("SELL NOT FILLED | Rescanning ask.", "warning")
            return {"action": "not_filled"}

        # Other statuses
        print_log(f"Sell order ended with status: {result.status.value}", "warning")
        return {"action": "not_filled"}

    # ── Step 7: Hold mode ────────────────────────────────────────────

    def step_hold(self, filled_qty: Decimal, avg_buy_price: Decimal) -> bool:
        """
        Wait until ask > buy price AND ask > VWAP. Returns True when conditions met.
        """
        self.state.status = BotStatus.HOLDING
        self.state.current_order_id = None
        self.tracker.mark_holding()

        try:
            bid, ask = self.api.get_best_bid_ask(self.pair_info.symbol)
            self._update_prices(bid, ask)
        except Exception as e:
            print_log(f"Error in hold scan: {e}", "error")
            time.sleep(2)
            return False

        # Refresh VWAP
        try:
            vwap, _ = self._compute_vwap()
            self.state.vwap = vwap
        except Exception:
            vwap = Decimal("0")

        self._refresh_dashboard()

        sell_price = self.order_mgr.truncate_price(ask)

        if sell_price > avg_buy_price:
            gap_pct = float(((sell_price - avg_buy_price) / avg_buy_price) * 100)
            print_log(
                f"Ask ({float(sell_price)}) > Buy ({float(avg_buy_price)}). "
                f"Gap: +{gap_pct:.4f}%. Placing sell.",
                "success",
            )
            return True

        gap_pct = float(((sell_price - avg_buy_price) / avg_buy_price) * 100)
        vwap_str = f" | VWAP: {float(vwap)}" if vwap > 0 else ""
        print_log(
            f"HOLDING | Buy: {float(avg_buy_price)} | "
            f"Ask: {float(sell_price)}{vwap_str} | Gap: {gap_pct:.4f}% | Waiting...",
            "warning",
        )
        time.sleep(3)
        return False

    # ── Market sell for remaining dust ────────────────────────────────

    def _market_sell_remaining(self, filled_qty: Decimal, avg_buy_price: Decimal) -> bool:
        """Force sell remaining coins via market order. Returns True if sold."""
        try:
            print_log(
                f"MARKET SELL remaining {float(filled_qty)} {self.pair_info.base_asset}...",
                "warning",
            )
            self.api.place_market_sell(self.pair_info.symbol, filled_qty)
            print_log("Market sell placed for remaining coins.", "success")

            self.state.filled_qty = Decimal("0")
            self.state.avg_buy_price = Decimal("0")
            self.state.coin_balance = Decimal("0")
            return True
        except Exception as e:
            print_log(f"Market sell failed: {e}", "error")
            time.sleep(2)
            return False

    # ── Main loop ────────────────────────────────────────────────────

    def run_cycle(self) -> bool:
        """Execute one complete trade cycle (scan -> buy -> sell).

        Returns True if the current pair is still suitable (scan passed),
        False if the pair failed scanning (caller should switch pair).
        """

        # Step 2: Scan — if fail, signal caller to switch pair
        if self.state.is_shutting_down:
            return False
        if not self.step_scan():
            return False

        if self.state.is_shutting_down:
            return False

        # Step 3-4: Buy
        buy_result = self.step_buy()
        if not buy_result["success"] or self.state.is_shutting_down:
            # Balance too low for this pair — switch to another
            if buy_result.get("skip_pair"):
                return False
            # Buy failed but scan passed — pair is still suitable, retry it
            return True

        filled_qty = buy_result["filled_qty"]
        avg_buy_price = buy_result["avg_price"]

        # Step 5-7: Sell loop — MUST sell ALL coins before exiting
        while not self.state.is_shutting_down:
            sell_result = self.step_sell(filled_qty, avg_buy_price)
            action = sell_result["action"]

            if action == "completed":
                # All coins sold, cycle complete
                return True

            elif action == "partial":
                # Some coins sold, keep selling the rest
                filled_qty = sell_result["remaining_qty"]
                self.tracker.start_trade(avg_buy_price, filled_qty, avg_buy_price * filled_qty)
                print_log(
                    f"Partial fill — still holding {float(filled_qty)} coins. Retrying sell...",
                    "warning",
                )
                continue

            elif action == "market_sell_remaining":
                # Quantity too small for limit order — use market sell
                if self._market_sell_remaining(filled_qty, avg_buy_price):
                    return True
                # Market sell failed, retry
                continue

            elif action == "holding":
                # Enter hold mode: wait for profitable ask
                while not self.state.is_shutting_down:
                    if self.step_hold(filled_qty, avg_buy_price):
                        break  # Break to retry sell
                continue

            elif action == "not_filled" or action == "retry":
                # Retry sell immediately
                continue

        return False

    def _pick_random_pair(self) -> None:
        """Randomly select a pair from the configured list and switch to it."""
        symbols = list(self.pairs_info.keys())
        symbol = random.choice(symbols)
        self.switch_pair(symbol)
        print_log(f"Selected pair: {symbol}", "info")

    def run(self) -> None:
        """Run the bot in an infinite loop."""
        print_log(
            f"Bot started | {len(self.pairs_info)} pairs | "
            f"Balance: ${float(self.state.usdt_balance)} USDT",
            "success",
        )

        while not self.state.is_shutting_down:
            try:
                self._pick_random_pair()
                # Stay on this pair as long as it passes scan
                while not self.state.is_shutting_down:
                    pair_still_good = self.run_cycle()
                    if not pair_still_good:
                        # Scan failed — switch to another pair
                        print_log(
                            f"Pair {self.pair_info.symbol} no longer suitable, switching...",
                            "info",
                        )
                        break
            except RateLimitError:
                print_log("Rate limited! Pausing 5 seconds...", "error")
                time.sleep(5)
            except Exception as e:
                logger.exception(f"Unhandled error in trade cycle: {e}")
                print_log(f"Error: {e}. Retrying in 10s...", "error")
                time.sleep(10)
