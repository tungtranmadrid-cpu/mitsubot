"""MEXC REST API v3 client with HMAC signature and rate-limit handling."""

from __future__ import annotations

import hashlib
import hmac
import logging
import time
from decimal import Decimal
from typing import Any, Optional

import requests

from config import Config
from models import OrderInfo, OrderSide, OrderStatus, PairInfo

logger = logging.getLogger("mexc_api")

# Minimum gap between API calls (seconds)
MIN_REQUEST_GAP = 0.5


def decimal_to_str(d: Decimal) -> str:
    """Convert Decimal to plain string, never scientific notation (e.g. '1E-5')."""
    return format(d, 'f')


class RateLimitError(Exception):
    """Raised when HTTP 429 is received."""
    pass


class APIError(Exception):
    """Raised on non-200 API responses."""
    def __init__(self, status_code: int, message: str):
        self.status_code = status_code
        super().__init__(f"HTTP {status_code}: {message}")


class MexcAPI:
    """Low-level MEXC REST API v3 client."""

    def __init__(self, config: Config):
        self.config = config
        self.base_url = config.mexc_base_url
        self.api_key = config.mexc_api_key
        self.api_secret = config.mexc_api_secret
        self.session = requests.Session()
        self.session.headers.update({"X-MEXC-APIKEY": self.api_key})
        self._last_request_time: float = 0
        self._backoff_until: float = 0

    # ── Request helpers ──────────────────────────────────────────────

    def _build_signed_query(self, params: dict) -> str:
        """Build query string with timestamp and HMAC-SHA256 signature.

        Returns the full query string (including &signature=...) to be
        appended to the URL directly, so the signed bytes exactly match
        what the server receives.
        """
        params["timestamp"] = int(time.time() * 1000)
        query_string = "&".join(f"{k}={v}" for k, v in sorted(params.items()))
        signature = hmac.new(
            self.api_secret.encode(),
            query_string.encode(),
            hashlib.sha256,
        ).hexdigest()
        return f"{query_string}&signature={signature}"

    def _throttle(self) -> None:
        """Enforce minimum gap between requests and backoff."""
        now = time.time()
        # Respect backoff from 429
        if now < self._backoff_until:
            wait = self._backoff_until - now
            logger.warning(f"Rate-limit backoff: sleeping {wait:.1f}s")
            time.sleep(wait)
        # Enforce minimum gap
        elapsed = time.time() - self._last_request_time
        if elapsed < MIN_REQUEST_GAP:
            time.sleep(MIN_REQUEST_GAP - elapsed)
        self._last_request_time = time.time()

    def _handle_response(self, resp: requests.Response) -> dict:
        """Check response status and handle rate limits."""
        if resp.status_code == 429:
            # Exponential backoff: start at 2s, double each consecutive 429
            current_backoff = max(2.0, self._backoff_until - time.time())
            next_backoff = min(current_backoff * 2, 30.0)
            self._backoff_until = time.time() + next_backoff
            logger.warning(f"HTTP 429 Rate Limited! Backing off {next_backoff:.0f}s")
            raise RateLimitError(f"Rate limited, backing off {next_backoff:.0f}s")

        if resp.status_code == 403:
            # CDN/WAF block (Akamai) — back off aggressively to avoid worsening
            self._backoff_until = time.time() + 60
            logger.warning("HTTP 403 Access Denied — IP may be blocked. Backing off 60s")
            raise RateLimitError("Access denied (403) — IP blocked by CDN. Backing off 60s")

        # Reset backoff on success
        self._backoff_until = 0

        if resp.status_code != 200:
            try:
                body = resp.json()
                msg = body.get("msg", resp.text)
            except Exception:
                msg = resp.text
            raise APIError(resp.status_code, msg)

        return resp.json()

    def _request(
        self,
        method: str,
        path: str,
        params: Optional[dict] = None,
        signed: bool = False,
        max_retries: int = 3,
    ) -> dict:
        """Execute an API request with throttling, signing, and retry logic."""
        params = params or {}
        url = f"{self.base_url}{path}"

        for attempt in range(1, max_retries + 1):
            try:
                self._throttle()

                req_params = dict(params)

                if signed:
                    # Build query string ourselves so signed bytes match exactly
                    qs = self._build_signed_query(req_params)
                    full_url = f"{url}?{qs}"
                    if method == "GET":
                        resp = self.session.get(full_url, timeout=10)
                    elif method == "POST":
                        resp = self.session.post(full_url, timeout=10)
                    elif method == "DELETE":
                        resp = self.session.delete(full_url, timeout=10)
                    else:
                        raise ValueError(f"Unsupported method: {method}")
                else:
                    if method == "GET":
                        resp = self.session.get(url, params=req_params, timeout=10)
                    elif method == "POST":
                        resp = self.session.post(url, params=req_params, timeout=10)
                    elif method == "DELETE":
                        resp = self.session.delete(url, params=req_params, timeout=10)
                    else:
                        raise ValueError(f"Unsupported method: {method}")

                return self._handle_response(resp)

            except RateLimitError:
                if attempt < max_retries:
                    logger.warning(f"Retry {attempt}/{max_retries} after rate limit")
                    continue
                raise

            except requests.RequestException as e:
                logger.error(f"Network error (attempt {attempt}/{max_retries}): {e}")
                if attempt < max_retries:
                    time.sleep(2)
                    continue
                raise

    # ── Public endpoints ─────────────────────────────────────────────

    def get_exchange_info(self, symbol: str) -> PairInfo:
        """GET /api/v3/exchangeInfo — fetch pair details (stepSize, tickSize, etc.)."""
        data = self._request("GET", "/api/v3/exchangeInfo", {"symbol": symbol})

        if not data.get("symbols"):
            raise APIError(404, f"Symbol {symbol} not found on MEXC")

        sym_info = data["symbols"][0]
        base_asset = sym_info.get("baseAsset", "")
        quote_asset = sym_info.get("quoteAsset", "")

        # MEXC v3 uses top-level fields, not Binance-style filters
        # baseSizePrecision = "0.0001" (quantity step as string)
        # quotePrecision = 6 (price decimal places as int)
        # quoteAmountPrecision = "1" (minimum order value as string)

        # Quantity step: baseSizePrecision is a string like "0.0001"
        raw_step = sym_info.get("baseSizePrecision")
        if raw_step and not str(raw_step).isdigit():
            # It's a string like "0.0001" — use directly as step size
            step_size = Decimal(str(raw_step))
        elif raw_step:
            # It's an integer precision like 4
            step_size = Decimal(10) ** -int(raw_step)
        else:
            step_size = Decimal(10) ** -int(sym_info.get("baseAssetPrecision", 5))

        # Price step: quotePrecision is an int (number of decimals)
        quote_prec = int(sym_info.get("quotePrecision", sym_info.get("quoteAssetPrecision", 8)))
        tick_size = Decimal(10) ** -quote_prec

        # Minimum notional: quoteAmountPrecision is a string like "1"
        raw_notional = sym_info.get("quoteAmountPrecision", "1")
        min_notional = Decimal(str(raw_notional))

        return PairInfo(
            symbol=symbol,
            step_size=step_size,
            tick_size=tick_size,
            min_notional=min_notional,
            base_asset=base_asset,
            quote_asset=quote_asset,
        )

    def get_orderbook(self, symbol: str, limit: int = 5) -> dict:
        """GET /api/v3/depth — fetch top N bids/asks."""
        return self._request("GET", "/api/v3/depth", {
            "symbol": symbol,
            "limit": limit,
        })

    def get_klines(self, symbol: str, interval: str = "5m", limit: int = 50) -> list[list]:
        """GET /api/v3/klines — fetch candlestick data.

        Each entry: [openTime, open, high, low, close, volume, closeTime, quoteVolume]
        Returns newest candle last.
        """
        data = self._request("GET", "/api/v3/klines", {
            "symbol": symbol,
            "interval": interval,
            "limit": limit,
        })
        return data

    def get_recent_trades(self, symbol: str, limit: int = 200) -> list[dict]:
        """GET /api/v3/trades — fetch recent public trades."""
        data = self._request("GET", "/api/v3/trades", {
            "symbol": symbol,
            "limit": limit,
        })
        return data

    def get_best_bid_ask(self, symbol: str) -> tuple[Decimal, Decimal]:
        """Return (bestBid, bestAsk) from orderbook."""
        book = self.get_orderbook(symbol, limit=5)
        bids = book.get("bids", [])
        asks = book.get("asks", [])

        if not bids or not asks:
            raise APIError(0, "Empty orderbook — no bids or asks available")

        best_bid = Decimal(str(bids[0][0]))
        best_ask = Decimal(str(asks[0][0]))
        return best_bid, best_ask

    # ── Private endpoints ────────────────────────────────────────────

    def get_account(self) -> dict:
        """GET /api/v3/account — fetch account balances. Rate limit: 2 req/s."""
        return self._request("GET", "/api/v3/account", signed=True)

    def get_balance(self, asset: str) -> Decimal:
        """Get free balance for a specific asset."""
        account = self.get_account()
        for bal in account.get("balances", []):
            if bal["asset"] == asset:
                return Decimal(str(bal["free"]))
        return Decimal("0")

    def place_order(
        self,
        symbol: str,
        side: OrderSide,
        price: Decimal,
        quantity: Decimal,
    ) -> OrderInfo:
        """POST /api/v3/order — place a limit order."""
        params = {
            "symbol": symbol,
            "side": side.value,
            "type": "LIMIT",
            "quantity": decimal_to_str(quantity),
            "price": decimal_to_str(price),
        }
        data = self._request("POST", "/api/v3/order", params, signed=True)

        return OrderInfo(
            order_id=str(data["orderId"]),
            symbol=symbol,
            side=side,
            price=price,
            quantity=quantity,
            status=OrderStatus(data.get("status", "NEW")),
        )

    def get_order(self, symbol: str, order_id: str) -> OrderInfo:
        """GET /api/v3/order — query order status."""
        data = self._request("GET", "/api/v3/order", {
            "symbol": symbol,
            "orderId": order_id,
        }, signed=True)

        return OrderInfo(
            order_id=str(data["orderId"]),
            symbol=symbol,
            side=OrderSide(data["side"]),
            price=Decimal(str(data["price"])),
            quantity=Decimal(str(data["origQty"])),
            status=OrderStatus(data["status"]),
            executed_qty=Decimal(str(data.get("executedQty", "0"))),
            cumulative_quote_qty=Decimal(str(data.get("cummulativeQuoteQty", "0"))),
        )

    def cancel_order(self, symbol: str, order_id: str) -> dict:
        """DELETE /api/v3/order — cancel an open order."""
        return self._request("DELETE", "/api/v3/order", {
            "symbol": symbol,
            "orderId": order_id,
        }, signed=True)

    def place_market_sell(self, symbol: str, quantity: Decimal) -> OrderInfo:
        """POST /api/v3/order — place a market sell (for graceful shutdown)."""
        params = {
            "symbol": symbol,
            "side": "SELL",
            "type": "MARKET",
            "quantity": decimal_to_str(quantity),
        }
        data = self._request("POST", "/api/v3/order", params, signed=True)

        return OrderInfo(
            order_id=str(data["orderId"]),
            symbol=symbol,
            side=OrderSide.SELL,
            price=Decimal("0"),
            quantity=quantity,
            status=OrderStatus(data.get("status", "NEW")),
        )
