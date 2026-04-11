"""Order management: place, poll, cancel, handle partial fills."""

import logging
import time
from decimal import Decimal, ROUND_DOWN
from typing import Optional

from config import Config
from mexc_api import MexcAPI, APIError, RateLimitError
from models import OrderInfo, OrderSide, OrderStatus, PairInfo

logger = logging.getLogger("order_mgr")


def truncate_to_step(value: Decimal, step: Decimal) -> Decimal:
    """Truncate a value down to the nearest step increment."""
    if step <= 0:
        return value
    # Use the step's exponent to determine decimal places
    # Decimal('0.001').as_tuple().exponent == -3
    exp = step.as_tuple().exponent
    decimals = -exp if exp < 0 else 0
    result = (value / step).to_integral_value(rounding=ROUND_DOWN) * step
    return result.quantize(Decimal(10) ** -decimals)


class OrderManager:
    """Handles order lifecycle: place, poll, cancel."""

    def __init__(self, api: MexcAPI, config: Config, pair_info: PairInfo):
        self.api = api
        self.config = config
        self.pair_info = pair_info

    def calculate_buy_quantity(
        self, usdt_balance: Decimal, bid_price: Decimal
    ) -> Optional[Decimal]:
        """Calculate buy quantity from USDT balance, respecting step size and min notional."""
        budget = usdt_balance * (self.config.buy_trade_percent / Decimal("100"))
        raw_qty = budget / bid_price
        qty = truncate_to_step(raw_qty, self.pair_info.step_size)

        # Check minimum notional
        notional = qty * bid_price
        if notional < self.pair_info.min_notional:
            logger.error(
                f"Order notional {notional} < min {self.pair_info.min_notional}. "
                f"Balance too low."
            )
            return None

        if qty <= 0:
            logger.error("Calculated buy quantity is 0.")
            return None

        return qty

    def calculate_sell_quantity(
        self, filled_qty: Decimal
    ) -> Optional[Decimal]:
        """Calculate sell quantity from filled buy qty, applying TRADE_PERCENT."""
        raw_qty = filled_qty * (self.config.sell_trade_percent / Decimal("100"))
        qty = truncate_to_step(raw_qty, self.pair_info.step_size)

        if qty <= 0:
            logger.error("Calculated sell quantity is 0.")
            return None

        return qty

    def truncate_price(self, price: Decimal) -> Decimal:
        """Truncate price to tick size."""
        return truncate_to_step(price, self.pair_info.tick_size)

    def place_buy(self, price: Decimal, quantity: Decimal) -> OrderInfo:
        """Place a limit buy order."""
        price = self.truncate_price(price)
        order = self.api.place_order(
            self.pair_info.symbol, OrderSide.BUY, price, quantity
        )
        logger.info(
            f"BUY ORDER | Price: ${price} | Qty: {quantity} | "
            f"Total: ${price * quantity} | OrderID: {order.order_id}"
        )
        return order

    def place_sell(self, price: Decimal, quantity: Decimal) -> OrderInfo:
        """Place a limit sell order."""
        price = self.truncate_price(price)
        order = self.api.place_order(
            self.pair_info.symbol, OrderSide.SELL, price, quantity
        )
        logger.info(
            f"SELL ORDER | Price: ${price} | Qty: {quantity} | "
            f"Total: ${price * quantity} | OrderID: {order.order_id}"
        )
        return order

    def poll_order(
        self, order_id: str, timeout_ms: int, on_poll: Optional[callable] = None
    ) -> OrderInfo:
        """
        Poll order status every 500ms until filled or timeout.

        Args:
            order_id: The order to poll.
            timeout_ms: Max wait time in milliseconds.
            on_poll: Optional callback called with OrderInfo on each poll.

        Returns:
            Final OrderInfo after timeout or fill.
        """
        start = time.time()
        timeout_s = timeout_ms / 1000.0
        last_order = None

        while True:
            try:
                last_order = self.api.get_order(self.pair_info.symbol, order_id)

                if on_poll:
                    on_poll(last_order)

                # If fully filled, return immediately
                if last_order.status == OrderStatus.FILLED:
                    return last_order

                # If canceled externally
                if last_order.status in (OrderStatus.CANCELED, OrderStatus.REJECTED, OrderStatus.EXPIRED):
                    return last_order

            except RateLimitError:
                # Back off handled by api._throttle, just continue
                pass
            except APIError as e:
                if e.status_code == 404 or "Order does not exist" in str(e):
                    logger.warning(f"Order {order_id} not found — may be filled or canceled externally.")
                    if last_order:
                        return last_order
                    # Return a synthetic canceled order
                    return OrderInfo(
                        order_id=order_id,
                        symbol=self.pair_info.symbol,
                        side=OrderSide.BUY,
                        price=Decimal("0"),
                        quantity=Decimal("0"),
                        status=OrderStatus.CANCELED,
                    )
            except Exception as e:
                logger.error(f"Error polling order {order_id}: {e}")

            # Check timeout
            elapsed = time.time() - start
            if elapsed >= timeout_s:
                break

            # Wait 1s before next poll
            time.sleep(1.0)

        return last_order

    def cancel_order(self, order_id: str) -> bool:
        """Cancel an order. Returns True if cancel succeeded or order already gone."""
        try:
            self.api.cancel_order(self.pair_info.symbol, order_id)
            logger.info(f"Order {order_id} cancelled.")
            return True
        except APIError as e:
            if "Unknown order" in str(e) or "Order does not exist" in str(e):
                logger.warning(f"Order {order_id} already gone (filled/canceled).")
                return True
            logger.error(f"Failed to cancel order {order_id}: {e}")
            return False
        except Exception as e:
            logger.error(f"Error cancelling order {order_id}: {e}")
            return False
