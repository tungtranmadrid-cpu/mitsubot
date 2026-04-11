"""Terminal dashboard rendering using rich."""

import os
import time
from datetime import datetime
from decimal import Decimal

from rich.console import Console
from rich.table import Table
from rich.text import Text
from rich.panel import Panel
from rich.layout import Layout
from rich.live import Live

from models import BotState, BotStatus, TradeRecord
from pnl_tracker import PnLTracker


console = Console()


def format_price(price: Decimal, prefix: str = "$") -> str:
    """Format price with commas and appropriate decimals."""
    p = float(price)
    if p >= 1000:
        return f"{prefix}{p:,.2f}"
    elif p >= 1:
        return f"{prefix}{p:,.4f}"
    else:
        return f"{prefix}{p:,.8f}"


def format_qty(qty: Decimal) -> str:
    """Format quantity."""
    q = float(qty)
    if q >= 1:
        return f"{q:,.4f}"
    else:
        return f"{q:,.8f}"


def format_pnl(pnl: Decimal) -> Text:
    """Format PnL with color."""
    p = float(pnl)
    if p > 0:
        return Text(f"+${p:,.4f}", style="bold green")
    elif p < 0:
        return Text(f"-${abs(p):,.4f}", style="bold red")
    else:
        return Text(f"${p:,.4f}", style="dim")


def format_pnl_str(pnl: Decimal, pnl_pct: Decimal) -> str:
    """Format PnL as plain string with percentage."""
    p = float(pnl)
    pct = float(pnl_pct)
    if p >= 0:
        return f"+${p:,.4f} (+{pct:.3f}%)"
    else:
        return f"-${abs(p):,.4f} ({pct:.3f}%)"


def status_style(status: BotStatus) -> str:
    """Return rich style for bot status."""
    return {
        BotStatus.SCANNING: "yellow",
        BotStatus.BUYING: "cyan",
        BotStatus.SELLING: "magenta",
        BotStatus.HOLDING: "red",
        BotStatus.SHUTTING_DOWN: "bold red",
    }.get(status, "white")


def build_dashboard(state: BotState, tracker: PnLTracker) -> str:
    """Build the dashboard string for display."""
    now = datetime.now().strftime("%H:%M:%S")
    start = datetime.fromtimestamp(state.start_time).strftime("%H:%M:%S")

    lines = []

    # Header
    lines.append("")
    lines.append("[bold white on blue]" + "=" * 64 + "[/]")
    lines.append(
        f"[bold white on blue]  MEXC Spread Bot | {state.pair} | "
        f"Running since {start}  [/]"
    )
    lines.append("[bold white on blue]" + "=" * 64 + "[/]")

    # Prices
    spread_color = "green" if state.spread_pct >= Decimal("0.01") else "red"
    vwap_str = f"    VWAP: [bold white]{format_price(state.vwap)}[/]" if state.vwap > 0 else ""
    lines.append(
        f"  BID: [bold cyan]{format_price(state.best_bid)}[/]    "
        f"ASK: [bold magenta]{format_price(state.best_ask)}[/]    "
        f"SPREAD: [{spread_color}]{float(state.spread_pct):.4f}%[/]"
        f"{vwap_str}"
    )

    # Status
    st_style = status_style(state.status)
    order_str = f"  Order: #{state.current_order_id}" if state.current_order_id else ""
    lines.append(
        f"  Status: [{st_style}]{state.status.value}[/]{order_str}"
    )

    # Balance
    lines.append(
        f"  Balance: [white]{format_price(state.usdt_balance)} USDT[/] | "
        f"[white]{format_qty(state.coin_balance)} {state.pair.replace('USDT', '')}[/]"
    )

    lines.append("[dim]" + "-" * 64 + "[/]")

    # Session PnL
    pnl_val = float(tracker.session_pnl)
    pnl_pct = float(tracker.session_pnl_pct)
    if pnl_val >= 0:
        pnl_str = f"[bold green]+${pnl_val:,.4f} (+{pnl_pct:.3f}%)[/]"
    else:
        pnl_str = f"[bold red]-${abs(pnl_val):,.4f} ({pnl_pct:.3f}%)[/]"

    lines.append(f"  SESSION PnL: {pnl_str}")

    # Trade stats
    completed = sum(1 for t in tracker.trades if t.is_complete)
    cycle_desc = state.status.value
    if state.status == BotStatus.SELLING:
        cycle_desc = "BUY filled -> SELL pending"
    elif state.status == BotStatus.HOLDING:
        cycle_desc = "BUY filled -> HOLDING"
    elif state.status == BotStatus.BUYING:
        cycle_desc = "BUY pending"

    lines.append(
        f"  Trades: [white]{completed}[/] completed | "
        f"Win rate: [white]{float(tracker.win_rate):.1f}%[/] | "
        f"Current: [dim]{cycle_desc}[/]"
    )

    lines.append("[dim]" + "-" * 64 + "[/]")

    # Recent trades
    lines.append("  [bold]RECENT TRADES:[/]")
    recent = tracker.recent_trades
    if not recent:
        lines.append("  [dim]No trades yet...[/]")
    else:
        for t in recent[:10]:
            ts = datetime.fromtimestamp(t.timestamp).strftime("%H:%M:%S")
            buy_str = f"BUY {format_price(t.buy_price)} x {format_qty(t.buy_qty)}"

            if t.is_holding:
                lines.append(
                    f"  [dim]#{t.trade_number}[/] [{ts}] {buy_str} -> [red]HOLDING...[/]"
                )
            elif t.is_complete:
                pnl_f = float(t.pnl) if t.pnl else 0
                sell_str = f"SELL {format_price(t.sell_price)} x {format_qty(t.sell_qty)}"
                partial_tag = " [yellow](partial)[/]" if t.is_partial else ""
                if pnl_f >= 0:
                    pnl_display = f"[green]+${pnl_f:,.4f}[/]"
                else:
                    pnl_display = f"[red]-${abs(pnl_f):,.4f}[/]"
                lines.append(
                    f"  [dim]#{t.trade_number}[/] [{ts}] {buy_str} -> "
                    f"{sell_str} | {pnl_display}{partial_tag}"
                )

    lines.append("[bold white on blue]" + "=" * 64 + "[/]")
    lines.append("")

    return "\n".join(lines)


def print_dashboard(state: BotState, tracker: PnLTracker) -> None:
    """Clear screen and print the dashboard."""
    # Clear terminal
    os.system("cls" if os.name == "nt" else "clear")
    markup = build_dashboard(state, tracker)
    console.print(markup)


def print_log(message: str, level: str = "info") -> None:
    """Print a timestamped log line."""
    ts = datetime.now().strftime("%H:%M:%S")
    style = {
        "info": "white",
        "success": "bold green",
        "warning": "bold yellow",
        "error": "bold red",
    }.get(level, "white")
    console.print(f"[dim][{ts}][/] [{style}]{message}[/]")


def print_summary(tracker: PnLTracker) -> None:
    """Print session summary on shutdown."""
    summary = tracker.get_summary()
    console.print("\n[bold]" + "=" * 50 + "[/]")
    console.print("[bold]  SESSION SUMMARY[/]")
    console.print("[bold]" + "=" * 50 + "[/]")
    console.print(f"  Total trades:     {summary['total_trades']}")
    console.print(f"  Completed trades: {summary['completed_trades']}")
    console.print(f"  Wins / Losses:    {summary['win_count']} / {summary['loss_count']}")
    console.print(f"  Win rate:         {float(summary['win_rate']):.1f}%")

    pnl = float(summary["session_pnl"])
    pnl_pct = float(summary["session_pnl_pct"])
    if pnl >= 0:
        console.print(f"  Session PnL:      [bold green]+${pnl:,.4f} (+{pnl_pct:.3f}%)[/]")
    else:
        console.print(f"  Session PnL:      [bold red]-${abs(pnl):,.4f} ({pnl_pct:.3f}%)[/]")
    console.print("[bold]" + "=" * 50 + "[/]\n")
