"""PnL tracker for individual trades and session totals."""

from __future__ import annotations

import time
from collections import deque
from decimal import Decimal
from typing import Optional

from models import TradeRecord


class PnLTracker:
    """Track profit/loss for each trade and the overall session."""

    def __init__(self, initial_balance: Decimal):
        self.initial_balance = initial_balance
        self.session_pnl = Decimal("0")
        self.trade_count = 0
        self.trades: deque[TradeRecord] = deque(maxlen=50)
        self._current_trade: Optional[TradeRecord] = None

    @property
    def session_pnl_pct(self) -> Decimal:
        if self.initial_balance > 0:
            return (self.session_pnl / self.initial_balance) * 100
        return Decimal("0")

    @property
    def recent_trades(self) -> list[TradeRecord]:
        """Return the 10 most recent trades, newest first."""
        return list(reversed(self.trades))[:10]

    @property
    def win_count(self) -> int:
        return sum(1 for t in self.trades if t.pnl and t.pnl > 0)

    @property
    def loss_count(self) -> int:
        return sum(1 for t in self.trades if t.pnl and t.pnl <= 0)

    @property
    def win_rate(self) -> Decimal:
        completed = sum(1 for t in self.trades if t.is_complete)
        if completed > 0:
            return (Decimal(self.win_count) / Decimal(completed)) * 100
        return Decimal("0")

    def start_trade(self, buy_price: Decimal, buy_qty: Decimal, buy_cost: Decimal) -> TradeRecord:
        """Record a new buy. Returns the trade record."""
        self.trade_count += 1
        trade = TradeRecord(
            trade_number=self.trade_count,
            timestamp=time.time(),
            buy_price=buy_price,
            buy_qty=buy_qty,
            buy_cost=buy_cost,
            is_holding=True,
        )
        self._current_trade = trade
        self.trades.append(trade)
        return trade

    def complete_sell(
        self,
        sell_price: Decimal,
        sell_qty: Decimal,
        sell_revenue: Decimal,
        avg_buy_price: Decimal,
        is_partial: bool = False,
    ) -> Decimal:
        """
        Record a sell completion. Returns PnL for this sell.
        Maker fee = 0%, so PnL = (sell_price - avg_buy_price) * sell_qty.
        """
        pnl = (sell_price - avg_buy_price) * sell_qty

        if self._current_trade:
            self._current_trade.sell_price = sell_price
            self._current_trade.sell_qty = sell_qty
            self._current_trade.sell_revenue = sell_revenue
            self._current_trade.pnl = pnl
            if avg_buy_price > 0:
                self._current_trade.pnl_pct = ((sell_price - avg_buy_price) / avg_buy_price) * 100
            self._current_trade.is_partial = is_partial
            self._current_trade.is_holding = False

        self.session_pnl += pnl
        return pnl

    def mark_holding(self) -> None:
        """Mark current trade as holding (waiting for profitable ask)."""
        if self._current_trade:
            self._current_trade.is_holding = True

    def get_summary(self) -> dict:
        """Return session summary for display/logging."""
        completed = sum(1 for t in self.trades if t.is_complete)
        return {
            "total_trades": self.trade_count,
            "completed_trades": completed,
            "session_pnl": self.session_pnl,
            "session_pnl_pct": self.session_pnl_pct,
            "win_count": self.win_count,
            "loss_count": self.loss_count,
            "win_rate": self.win_rate,
        }
