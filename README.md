# MEXC Bid-Ask Spread Trading Bot

Automated trading bot for the MEXC exchange that profits from bid-ask spreads using limit orders (0% maker fee).

## Strategy

1. **Scan** orderbook for bid-ask spread >= minimum threshold
2. **Buy** at best bid price (limit order)
3. **Sell** at best ask price (limit order)
4. **Repeat** — any positive spread = pure profit (0% maker fee)

## Setup

```bash
# Install dependencies
pip install -r requirements.txt

# Configure
cp .env.example .env
# Edit .env with your MEXC API key and secret

# Run
python main.py
```

## Configuration (.env)

| Variable | Default | Description |
|---|---|---|
| `MEXC_API_KEY` | — | Your MEXC API key |
| `MEXC_API_SECRET` | — | Your MEXC API secret |
| `MEXC_BASE_URL` | `https://api.mexc.com` | MEXC API base URL |
| `DEFAULT_PAIR` | `BTCUSDT` | Trading pair |
| `TRADE_PERCENT` | `99.5` | % of balance to use per trade |
| `BUY_RETRY_TIMEOUT` | `5000` | Buy order fill timeout (ms) |
| `SELL_RETRY_TIMEOUT` | `5000` | Sell order fill timeout (ms) |
| `MIN_SPREAD_PCT` | `0.01` | Minimum spread % to enter trade |

## Project Structure

```
├── main.py              # Entry point, shutdown handler
├── config.py            # .env reader and validator
├── mexc_api.py          # MEXC REST API v3 client
├── trading_engine.py    # Core trading logic (scan/buy/sell/hold)
├── order_manager.py     # Order lifecycle management
├── pnl_tracker.py       # PnL tracking per trade and session
├── display.py           # Terminal dashboard (rich)
├── models.py            # Data models
├── .env.example         # Config template
└── requirements.txt     # Python dependencies
```

## Graceful Shutdown

Press `Ctrl+C` to stop. The bot will:
- Cancel any open orders
- Ask whether to market-sell held coins
- Print session summary (trades, PnL, win rate)
