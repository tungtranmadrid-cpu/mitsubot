"""Dataclasses for the MEXC Spread Trading Bot."""

from dataclasses import dataclass, field
from decimal import Decimal
from enum import Enum
from typing import Optional
import time


class OrderSide(Enum):
    BUY = "BUY"
    SELL = "SELL"


class OrderStatus(Enum):
    NEW = "NEW"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    FILLED = "FILLED"
    CANCELED = "CANCELED"
    REJECTED = "REJECTED"
    EXPIRED = "EXPIRED"


class BotStatus(Enum):
    SCANNING = "SCANNING"
    BUYING = "BUYING"
    SELLING = "SELLING"
    HOLDING = "HOLDING"
    SHUTTING_DOWN = "SHUTTING_DOWN"


@dataclass
class PairInfo:
    """Exchange info for a trading pair."""
    symbol: str
    step_size: Decimal        # LOT_SIZE stepSize (quantity precision)
    tick_size: Decimal        # PRICE_FILTER tickSize (price precision)
    min_notional: Decimal     # MIN_NOTIONAL minNotional (minimum order value)
    base_asset: str           # e.g. "BTC"
    quote_asset: str          # e.g. "USDT"


@dataclass
class OrderInfo:
    """Represents an order on the exchange."""
    order_id: str
    symbol: str
    side: OrderSide
    price: Decimal
    quantity: Decimal
    status: OrderStatus = OrderStatus.NEW
    executed_qty: Decimal = Decimal("0")
    cumulative_quote_qty: Decimal = Decimal("0")

    @property
    def avg_price(self) -> Decimal:
        if self.executed_qty > 0:
            return self.cumulative_quote_qty / self.executed_qty
        return Decimal("0")

    @property
    def fill_pct(self) -> Decimal:
        if self.quantity > 0:
            return (self.executed_qty / self.quantity) * 100
        return Decimal("0")


@dataclass
class TradeRecord:
    """Record of a completed trade cycle (buy + sell)."""
    trade_number: int
    timestamp: float
    buy_price: Decimal
    buy_qty: Decimal
    buy_cost: Decimal
    sell_price: Optional[Decimal] = None
    sell_qty: Optional[Decimal] = None
    sell_revenue: Optional[Decimal] = None
    pnl: Optional[Decimal] = None
    pnl_pct: Optional[Decimal] = None
    is_partial: bool = False
    is_holding: bool = False

    @property
    def is_complete(self) -> bool:
        return self.sell_price is not None and not self.is_holding


@dataclass
class BotState:
    """Current state of the bot."""
    status: BotStatus = BotStatus.SCANNING
    pair: str = ""
    start_time: float = field(default_factory=time.time)
    initial_balance: Decimal = Decimal("0")

    # Current prices
    best_bid: Decimal = Decimal("0")
    best_ask: Decimal = Decimal("0")
    spread_pct: Decimal = Decimal("0")
    vwap: Decimal = Decimal("0")

    # Current order
    current_order_id: Optional[str] = None

    # Position info (when holding coin after buy)
    filled_qty: Decimal = Decimal("0")
    avg_buy_price: Decimal = Decimal("0")

    # Balances
    usdt_balance: Decimal = Decimal("0")
    coin_balance: Decimal = Decimal("0")

    # Session stats
    total_trades: int = 0
    session_pnl: Decimal = Decimal("0")
    session_pnl_pct: Decimal = Decimal("0")

    # Shutdown flag
    is_shutting_down: bool = False
