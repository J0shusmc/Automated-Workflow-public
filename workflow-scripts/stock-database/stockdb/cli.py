"""Click CLI entry point for stockdb."""

import click

from .config import INTERVALS
from .ticker_lists import get_list, all_list_names, TICKER_LISTS


@click.group()
def cli():
    """stockdb — Centralized market data powered by Polygon.io"""
    pass


def _parse_intervals(raw: tuple[str, ...]) -> list[str]:
    """Expand comma-separated interval values: ('daily,1hour', '5min') → ['daily','1hour','5min']."""
    result = []
    for item in raw:
        for part in item.split(","):
            part = part.strip()
            if part:
                result.append(part)
    return result


@cli.command()
@click.argument("tickers", nargs=-1)
@click.option("--interval", "-i", multiple=True, default=["daily"],
              help="Intervals: daily,1hour,5min (comma-separated or repeat flag)")
@click.option("--list", "-l", "ticker_list", default=None,
              help="Use a pre-built ticker list (sp500, nasdaq100, etfs, gp)")
@click.option("--start", "-s", default=None, help="Start date YYYY-MM-DD")
@click.option("--end", "-e", default=None, help="End date YYYY-MM-DD")
def download(tickers, interval, ticker_list, start, end):
    """Download historical data for tickers."""
    from .downloader import download_batch, download_ticker

    # Resolve tickers
    all_tickers = list(tickers)
    if ticker_list:
        all_tickers.extend(get_list(ticker_list))

    if not all_tickers:
        click.echo("No tickers specified. Use positional args or --list.")
        return

    # Deduplicate preserving order
    seen = set()
    unique = []
    for t in all_tickers:
        t = t.upper()
        if t not in seen:
            seen.add(t)
            unique.append(t)

    intervals = _parse_intervals(interval)
    for iv in intervals:
        if iv not in INTERVALS:
            click.echo(f"Unknown interval: {iv}. Use: {list(INTERVALS.keys())}")
            return

    click.echo(f"Downloading {len(unique)} tickers × {len(intervals)} intervals")

    if len(unique) == 1 and len(intervals) == 1:
        result = download_ticker(unique[0], intervals[0], start, end)
        _print_result(result)
    else:
        download_batch(unique, intervals, start, end)


@cli.command()
@click.option("--interval", "-i", multiple=True, default=None,
              help="Only update these intervals (comma-separated or repeat flag)")
def update(interval):
    """Incrementally update all downloaded data."""
    from .downloader import update_all

    intervals = _parse_intervals(interval) if interval else None
    update_all(intervals)


@cli.command("daily-update")
@click.option("--list", "-l", "ticker_lists", multiple=True, default=None,
              help="Ticker list to ensure exists when --download-missing is used")
@click.option("--keep-retired", is_flag=True,
              help="Keep known delisted/ticker-changed CSVs instead of removing them before update")
@click.option("--download-missing", is_flag=True,
              help="Download ticker/list/interval combinations that do not have CSV files yet")
@click.option("--repair-metadata", is_flag=True,
              help="Rebuild metadata entries from existing CSV files before updating")
@click.option("--max-stale-days", default=5, show_default=True,
              help="Fail audit when the newest bar is older than this many days")
@click.option("--no-fail-on-stale", is_flag=True,
              help="Report stale files as warnings instead of errors")
def daily_update(ticker_lists, download_missing, repair_metadata,
                 keep_retired, max_stale_days, no_fail_on_stale):
    """Update and audit daily bars only."""
    from .daily_update import (
        audit_database,
        daily_intervals,
        print_summary,
        remove_retired_data,
        repair_metadata_from_files,
        update_database,
    )

    intervals = daily_intervals()

    if not keep_retired:
        removed = remove_retired_data(intervals)
        if removed:
            click.echo(f"Removed {len(removed)} retired daily ticker file(s)/metadata entrie(s).")
            for item in removed[:25]:
                click.echo(f"  {item['ticker']}/{item['interval']}: {item['reason']}")
            if len(removed) > 25:
                click.echo(f"  ... {len(removed) - 25} more removed")

    if repair_metadata:
        click.echo("Repairing metadata from CSV files...")
        repair_issues = repair_metadata_from_files(intervals)
        if repair_issues:
            click.echo(f"Metadata repair found {len(repair_issues)} CSV issue(s).")

    click.echo("Updating daily stock bars...")
    results = update_database(
        intervals=intervals,
        list_names=list(ticker_lists or []),
        download_missing=download_missing,
    )

    click.echo("\nAuditing stock database...")
    issues = audit_database(
        intervals=intervals,
        max_stale_days=max_stale_days,
        fail_on_stale=not no_fail_on_stale,
    )
    print_summary(results, issues)

    update_errors = [result for result in results if result["status"] == "error"]
    audit_errors = [issue for issue in issues if issue.severity == "error"]
    if update_errors or audit_errors:
        raise click.ClickException(
            f"daily update finished with {len(update_errors)} update error(s) "
            f"and {len(audit_errors)} audit error(s)"
        )


@cli.command()
@click.argument("ticker", required=False)
def status(ticker):
    """Show download status summary."""
    from .metadata import all_entries, get_entry

    if ticker:
        ticker = ticker.upper()
        from .query import coverage
        cov = coverage(ticker)
        if not cov:
            click.echo(f"No data for {ticker}")
            return
        click.echo(f"\n{ticker} coverage:")
        for iv, info in cov.items():
            click.echo(
                f"  {iv:8s}  {info['first_date'][:10]} → {info['last_date'][:10]}"
                f"  ({info['row_count']:,} bars)  updated {info['last_updated'][:10]}"
            )
        return

    entries = all_entries()
    if not entries:
        click.echo("No data downloaded yet.")
        return

    # Group by interval
    by_interval = {}
    for entry in entries.values():
        iv = entry["interval"]
        by_interval.setdefault(iv, []).append(entry)

    click.echo(f"\nStock Database: {len(entries)} ticker/interval combinations\n")

    for iv in ["daily", "1hour", "5min"]:
        if iv not in by_interval:
            continue
        group = sorted(by_interval[iv], key=lambda e: e["ticker"])
        total_rows = sum(e["row_count"] for e in group)
        tickers = [e["ticker"] for e in group]

        click.echo(f"  {iv:8s}  {len(group):3d} tickers  {total_rows:>12,} bars")

        # Show first/last few tickers
        if len(tickers) <= 10:
            click.echo(f"           {', '.join(tickers)}")
        else:
            shown = ', '.join(tickers[:5]) + f" ... +{len(tickers)-5} more"
            click.echo(f"           {shown}")

    click.echo()


@cli.command()
@click.argument("ticker")
@click.option("--interval", "-i", default="daily",
              help="Interval to validate")
def validate(ticker, interval):
    """Run data quality checks on a ticker."""
    from .validation import validate_ticker

    ticker = ticker.upper()
    issues = validate_ticker(ticker, interval)

    if not issues:
        click.echo(f"{ticker}/{interval}: All checks passed ✓")
        return

    click.echo(f"\n{ticker}/{interval}: {len(issues)} issue(s)\n")
    for issue in issues:
        icon = {"error": "✗", "warning": "⚠", "info": "ℹ"}.get(issue["severity"], "?")
        click.echo(f"  {icon} [{issue['check']}] {issue['message']}")
    click.echo()


@cli.command("lists")
def show_lists():
    """Show available pre-built ticker lists."""
    click.echo("\nAvailable ticker lists:\n")
    for name in all_list_names():
        tickers = TICKER_LISTS[name]
        preview = ', '.join(tickers[:6])
        if len(tickers) > 6:
            preview += f" ... ({len(tickers)} total)"
        click.echo(f"  {name:12s}  {preview}")
    click.echo(f"\nUsage: stockdb download --list <name> --interval daily")
    click.echo()


@cli.command()
@click.argument("ticker")
@click.option("--interval", "-i", default="daily", help="Interval to compute features for")
def features(ticker, interval):
    """Compute and display scale-normalized features for a ticker."""
    import pandas as pd
    from .features import compute_and_clean
    from .metadata import csv_path

    ticker = ticker.upper()
    path = csv_path(ticker, interval)
    if not path.exists():
        click.echo(f"No data for {ticker}/{interval}. Download first.")
        return

    df = pd.read_csv(path)
    feat = compute_and_clean(df)

    click.echo(f"\n{ticker}/{interval}: {len(feat)} bars, {len(feat.columns)} features\n")

    # Show last 5 bars
    display_cols = ["timestamp", "body_pct", "gap_atr", "rel_volume", "atr_pct",
                    "dist_swing_high", "dist_swing_low", "trend_strength",
                    "fwd_ret_1", "fwd_ret_5"]
    available = [c for c in display_cols if c in feat.columns]

    click.echo(feat[available].tail(5).to_string(index=False))
    click.echo()


@cli.command()
@click.option("--tickers", "-t", default=None,
              help="Comma-separated tickers to train on (default: 10 liquid names)")
@click.option("--list", "-l", "ticker_list", default=None,
              help="Use a pre-built ticker list")
@click.option("--interval", "-i", default="1hour", help="Interval to train on")
@click.option("--strategy", "-s", default="both",
              type=click.Choice(["scalp", "momentum", "swing", "both"]),
              help="Strategy mode: scalp (40pt), momentum (50pt), swing (100pt), or both")
@click.option("--val-data", "val_path", default=None,
              help="Path to external CSV for validation (e.g. data/1hour/NQ.csv)")
@click.option("--workers", "-w", default=10, help="Number of CPU threads")
@click.option("--pop-size", "-p", default=200, help="Population size per worker")
@click.option("--generations", "-g", default=50, help="Generations to evolve")
@click.option("--min-trades", default=50, help="Minimum trades for valid equation")
def train(tickers, ticker_list, interval, strategy, val_path, workers, pop_size, generations, min_trades):
    """Train GP to discover trading equations using target/stop fitness."""
    import pandas as pd
    from rich.console import Console
    from rich.progress import Progress, SpinnerColumn, TextColumn, BarColumn, TimeElapsedColumn
    from .features import compute_and_clean
    from .gp_engine import GPEngine, STRATEGY_PROFILES, NQ_POINT_VALUE
    from .metadata import csv_path
    from .query import load_external_csv

    console = Console()

    # Show strategy info
    strats = [strategy] if strategy != "both" else ["swing", "momentum"]
    for s in strats:
        p = STRATEGY_PROFILES[s]
        console.print(
            f"[bold cyan]{s.upper()}[/bold cyan]: "
            f"+{p['nq_target_pts']}pt/-{p['nq_stop_pts']}pt "
            f"({p['max_hold']}-bar max) "
            f"[dim]@ ${NQ_POINT_VALUE:.0f}/pt[/dim]"
        )

    # Resolve tickers
    if tickers:
        ticker_names = [t.strip().upper() for t in tickers.split(",")]
    elif ticker_list:
        ticker_names = get_list(ticker_list)
    else:
        ticker_names = ["SPY", "QQQ", "AAPL", "MSFT", "AMZN",
                        "GOOGL", "META", "NVDA", "TSLA", "AMD"]

    console.print(f"\n[bold cyan]Loading data for {len(ticker_names)} tickers...[/bold cyan]")

    # Build combined feature matrix with progress
    all_features = []
    skipped = 0
    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        TextColumn("{task.completed}/{task.total}"),
        TimeElapsedColumn(),
        console=console,
    ) as progress:
        task = progress.add_task("Computing features + trade outcomes", total=len(ticker_names))
        for t in ticker_names:
            path = csv_path(t, interval)
            if not path.exists():
                skipped += 1
                progress.advance(task)
                continue
            df = pd.read_csv(path)
            feat = compute_and_clean(df)
            feat["ticker"] = t
            all_features.append(feat)
            progress.advance(task)

    if not all_features:
        console.print("[bold red]No data available. Download data first.[/bold red]")
        return

    combined = pd.concat(all_features, ignore_index=True)
    console.print(
        f"[green]Loaded {len(all_features)} tickers[/green] "
        f"({skipped} skipped) | "
        f"[bold]{len(combined):,}[/bold] bars total\n"
    )

    # Load external validation data if provided
    val_df = None
    if val_path:
        console.print(f"[bold cyan]Loading external validation data: {val_path}[/bold cyan]")
        raw_val = load_external_csv(val_path)
        val_df = compute_and_clean(raw_val)
        console.print(f"[green]Validation set: {len(val_df):,} bars[/green]\n")

    engine = GPEngine(
        n_workers=workers,
        pop_size=pop_size,
        generations=generations,
        min_trades=min_trades,
    )

    engine.train(combined, val_df=val_df, strategy=strategy)


@cli.command()
@click.option("--interval", "-i", default="daily", help="Interval to scan")
@click.option("--target", default="fwd_ret_5", help="Which GP results to use")
@click.option("--min-pf", default=1.3, help="Minimum profit factor for equations")
@click.option("--top", "-n", default=20, help="Show top N signals")
@click.option("--tickers", "-t", default=None,
              help="Comma-separated tickers to scan (default: all)")
def scan(interval, target, min_pf, top, tickers):
    """Scan universe with trained GP equations. Produces a watchlist."""
    from rich.console import Console
    from rich.table import Table
    from rich.panel import Panel
    from rich.text import Text
    from rich.progress import Progress, SpinnerColumn, TextColumn, BarColumn, TimeElapsedColumn
    from .scanner import Scanner

    console = Console()

    ticker_list = None
    if tickers:
        ticker_list = [t.strip().upper() for t in tickers.split(",")]

    scanner = Scanner(interval=interval, target_col=target, min_pf=min_pf)

    console.print(f"\n[bold cyan]Scanning universe...[/bold cyan]")
    signals = scanner.scan(tickers=ticker_list)

    if not signals:
        console.print("[bold yellow]No signals found.[/bold yellow] Train first with `stockdb train`.")
        return

    # Deduplicate: keep strongest signal per ticker/direction
    by_ticker = {}
    for sig in signals:
        key = (sig.ticker, sig.direction)
        if key not in by_ticker or abs(sig.signal_strength) > abs(by_ticker[key].signal_strength):
            by_ticker[key] = sig
    ranked = sorted(by_ticker.values(), key=lambda s: abs(s.signal_strength), reverse=True)

    # Rich table
    table = Table(
        show_header=True, header_style="bold magenta",
        border_style="cyan", expand=True,
        title=f"[bold cyan]WATCHLIST[/bold cyan] [dim]({len(ranked)} signals)[/dim]",
    )
    table.add_column("#", width=3, justify="right", style="dim")
    table.add_column("Ticker", style="bold white", width=8, justify="right")
    table.add_column("Dir", width=6, justify="center")
    table.add_column("Strength", width=10, justify="right")
    table.add_column("Close", width=10, justify="right")
    table.add_column("ATR%", width=7, justify="right")
    table.add_column("PF", width=6, justify="right")
    table.add_column("WR", width=7, justify="right")

    for i, sig in enumerate(ranked[:top]):
        dir_style = "bold green" if sig.direction == "long" else "bold red"
        pf_style = "bold green" if sig.profit_factor >= 1.5 else (
            "yellow" if sig.profit_factor >= 1.2 else "white"
        )

        table.add_row(
            str(i + 1),
            sig.ticker,
            Text(sig.direction.upper(), style=dir_style),
            f"{sig.signal_strength:.4f}",
            f"${sig.close:.2f}",
            f"{sig.atr_pct:.2%}",
            Text(f"{sig.profit_factor:.2f}", style=pf_style),
            f"{sig.win_rate:.1%}",
        )

    console.print()
    console.print(table)
    console.print()


@cli.command()
@click.option("--data", "-d", default="data/1hour/NQ.csv",
              help="Path to OHLCV CSV for backtest replay")
@click.option("--top-n", "-n", default=1, help="Top N equations per strategy")
@click.option("--min-votes", "-v", default=1, help="Minimum consensus votes to trigger")
@click.option("--run-file", default=None, help="Specific GP results JSON to load")
@click.option("--watch", "-w", is_flag=True, default=False,
              help="Stay open and watch CSV for new bars (live mode)")
@click.option("--poll", default=5.0, help="Seconds between file checks in watch mode")
def live(data, top_n, min_votes, run_file, watch, poll):
    """Run the live ensemble engine — backtest or watch for live signals."""
    from rich.console import Console
    from rich.table import Table
    from rich.panel import Panel
    from rich.text import Text
    from rich.columns import Columns
    from rich.live import Live
    from .live_engine import LiveEngine, NQ_POINT_VALUE

    console = Console()

    # ── Load engine ──────────────────────────────────────────────────────
    engine = LiveEngine(top_n=top_n, min_votes=min_votes, run_file=run_file)
    equations = engine.load()

    if not equations:
        console.print("[bold red]No equations found.[/bold red] Run `stockdb train --strategy both` first.")
        return

    # ── Equation summary panel ───────────────────────────────────────────
    eq_tables = []
    for strat, eqs in sorted(equations.items()):
        t = Table(
            show_header=True, header_style="bold magenta",
            border_style="cyan", title=f"[bold]{strat.upper()}[/bold]",
            min_width=40,
        )
        t.add_column("#", width=2, justify="right", style="dim")
        t.add_column("Val PF", width=7, justify="right")
        t.add_column("Val WR", width=7, justify="right")
        t.add_column("$/Trade", width=9, justify="right")
        t.add_column("Expression", max_width=50, no_wrap=True)

        for i, eq in enumerate(eqs):
            pf_style = "bold green" if eq.val_pf >= 1.8 else (
                "green" if eq.val_pf >= 1.5 else "yellow"
            )
            t.add_row(
                str(i + 1),
                Text(f"{eq.val_pf:.2f}", style=pf_style),
                f"{eq.val_wr:.1%}",
                f"${eq.val_per_trade:,.0f}",
                eq.expression[:50] + ("…" if len(eq.expression) > 50 else ""),
            )
        eq_tables.append(t)

    console.print()
    console.print(Panel(
        Columns(eq_tables, padding=(0, 2)),
        title="[bold cyan]LOADED EQUATIONS[/bold cyan]",
        border_style="cyan",
    ))

    total_eq = sum(len(eqs) for eqs in equations.values())
    strat_summary = " + ".join(
        f"[bold]{s.upper()}[/bold]×{len(e)}" for s, e in sorted(equations.items())
    )
    console.print(
        f"  [dim]{total_eq} equations loaded[/dim] ({strat_summary}) | "
        f"[dim]min votes: {min_votes}[/dim]\n"
    )

    # ── Watch mode or backtest ──────────────────────────────────────────
    if watch:
        _run_watch_mode(engine, data, poll, console)
        return

    console.print(f"[bold cyan]Running backtest on {data}...[/bold cyan]\n")

    trades = engine.backtest(data)

    if not trades:
        console.print("[bold yellow]No trades generated.[/bold yellow] Try lowering --min-votes.")
        return

    # ── Trade log (last 30) ──────────────────────────────────────────────
    trade_table = Table(
        show_header=True, header_style="bold magenta",
        border_style="cyan", expand=True,
        title=f"[bold cyan]TRADE LOG[/bold cyan] [dim](last 30 of {len(trades)})[/dim]",
    )
    trade_table.add_column("#", width=4, justify="right", style="dim")
    trade_table.add_column("Strategy", width=10, justify="center")
    trade_table.add_column("Dir", width=6, justify="center")
    trade_table.add_column("Entry", width=18)
    trade_table.add_column("Entry $", width=10, justify="right")
    trade_table.add_column("Exit $", width=10, justify="right")
    trade_table.add_column("Result", width=10, justify="center")
    trade_table.add_column("PnL Pts", width=9, justify="right")
    trade_table.add_column("PnL $", width=10, justify="right")
    trade_table.add_column("Bars", width=5, justify="right")
    trade_table.add_column("Votes", width=6, justify="center")

    for i, t in enumerate(trades[-30:]):
        idx = len(trades) - 30 + i if len(trades) > 30 else i
        dir_style = "bold green" if t.direction == "LONG" else "bold red"

        if t.result == "WIN":
            result_style = "bold green"
            result_text = "✓ WIN"
        elif t.result == "LOSS":
            result_style = "bold red"
            result_text = "✗ LOSS"
        else:
            result_style = "yellow"
            result_text = "◷ TIME"

        pnl_style = "green" if t.pnl_pts > 0 else ("red" if t.pnl_pts < 0 else "dim")
        pnl_dollars = t.pnl_pts * NQ_POINT_VALUE

        strat_style = "cyan" if t.strategy == "swing" else "magenta"

        trade_table.add_row(
            str(idx + 1),
            Text(t.strategy.upper(), style=strat_style),
            Text(t.direction, style=dir_style),
            t.entry_time[:16] if t.entry_time else "",
            f"${t.entry_price:,.2f}",
            f"${t.exit_price:,.2f}" if t.exit_price else "—",
            Text(result_text, style=result_style),
            Text(f"{t.pnl_pts:+.1f}", style=pnl_style),
            Text(f"${pnl_dollars:+,.0f}", style=pnl_style),
            str(t.bars_held),
            f"{t.votes}/{t.total_eq}",
        )

    console.print(trade_table)

    # ── Performance stats ────────────────────────────────────────────────
    stats = engine.get_stats()

    # Overall stats panel
    wr_style = "bold green" if stats["win_rate"] >= 0.50 else "yellow"
    pf_style = "bold green" if stats["profit_factor"] >= 1.5 else (
        "yellow" if stats["profit_factor"] >= 1.0 else "bold red"
    )
    pnl_style = "bold green" if stats["total_pts"] > 0 else "bold red"

    overall = Table(show_header=False, border_style="cyan", min_width=35)
    overall.add_column("Metric", style="bold white", width=18)
    overall.add_column("Value", justify="right", width=15)
    overall.add_row("Total Trades", f"{stats['total_trades']:,}")
    overall.add_row("Wins", f"[green]{stats['wins']:,}[/green]")
    overall.add_row("Losses", f"[red]{stats['losses']:,}[/red]")
    overall.add_row("Time Exits", f"[yellow]{stats['time_exits']:,}[/yellow]")
    overall.add_row("Win Rate", Text(f"{stats['win_rate']:.1%}", style=wr_style))
    overall.add_row("Profit Factor", Text(f"{stats['profit_factor']:.2f}", style=pf_style))
    overall.add_row("Total Points", Text(f"{stats['total_pts']:+,.1f}", style=pnl_style))
    overall.add_row("Total P&L", Text(f"${stats['total_dollars']:+,.0f}", style=pnl_style))
    overall.add_row("Avg Pts/Trade", f"{stats['avg_pts_per_trade']:+.1f}")
    overall.add_row("Avg $/Trade", f"${stats['avg_dollars_per_trade']:+,.0f}")

    # Per-strategy breakdown
    strat_tables = [overall]
    for s, ss in stats.get("by_strategy", {}).items():
        st = Table(
            show_header=False, border_style="cyan", min_width=30,
            title=f"[bold]{s.upper()}[/bold]",
        )
        st.add_column("Metric", style="bold white", width=15)
        st.add_column("Value", justify="right", width=13)

        s_wr_style = "bold green" if ss["win_rate"] >= 0.50 else "yellow"
        s_pf_style = "bold green" if ss["pf"] >= 1.5 else (
            "yellow" if ss["pf"] >= 1.0 else "bold red"
        )
        s_pnl_style = "bold green" if ss["total_pts"] > 0 else "bold red"

        st.add_row("Trades", f"{ss['trades']:,}")
        st.add_row("Wins", f"[green]{ss['wins']:,}[/green]")
        st.add_row("Losses", f"[red]{ss['losses']:,}[/red]")
        st.add_row("Win Rate", Text(f"{ss['win_rate']:.1%}", style=s_wr_style))
        st.add_row("Profit Factor", Text(f"{ss['pf']:.2f}", style=s_pf_style))
        st.add_row("Total Pts", Text(f"{ss['total_pts']:+,.1f}", style=s_pnl_style))
        st.add_row("Total P&L", Text(f"${ss['total_dollars']:+,.0f}", style=s_pnl_style))
        st.add_row("Avg Pts", f"{ss['avg_pts']:+.1f}")
        strat_tables.append(st)

    console.print()
    console.print(Panel(
        Columns(strat_tables, padding=(0, 3)),
        title="[bold cyan]PERFORMANCE SUMMARY[/bold cyan]",
        subtitle=f"[dim]NQ @ ${NQ_POINT_VALUE:.0f}/pt[/dim]",
        border_style="cyan",
    ))
    console.print()


def _run_watch_mode(engine, csv_path, poll_interval, console):
    """Rich live display for watch mode — stays open waiting for new bars."""
    from rich.table import Table
    from rich.panel import Panel
    from rich.text import Text
    from rich.live import Live
    from rich.layout import Layout
    from .live_engine import NQ_POINT_VALUE
    from datetime import datetime

    console.print(f"[bold cyan]Warming up on existing data in {csv_path}...[/bold cyan]")

    # Shared state for callbacks
    state = {
        "last_bar_time": "",
        "last_close": 0.0,
        "signals": [],       # recent signals (last 20)
        "closed": [],        # recent closed trades (last 20)
    }

    def on_signal(sig, trade):
        dir_style = "bold green" if sig.direction == "LONG" else "bold red"
        console.print(
            f"  [bold yellow]⚡ SIGNAL[/bold yellow] "
            f"[{dir_style}]{sig.direction}[/{dir_style}] "
            f"[cyan]{sig.strategy.upper()}[/cyan] "
            f"@ ${sig.close:,.2f} | "
            f"TP ${trade.target_price:,.2f} / SL ${trade.stop_price:,.2f} | "
            f"z={sig.avg_strength:+.2f} "
            f"[dim]({sig.votes_for}/{sig.votes_total} votes)[/dim]"
        )
        state["signals"].append(sig)
        state["signals"] = state["signals"][-20:]

    def on_trade_close(trade):
        if trade.result == "WIN":
            style = "bold green"
            icon = "✓"
        elif trade.result == "LOSS":
            style = "bold red"
            icon = "✗"
        else:
            style = "yellow"
            icon = "◷"

        pnl_dollars = trade.pnl_pts * NQ_POINT_VALUE
        console.print(
            f"  [{style}]{icon} {trade.result}[/{style}] "
            f"[cyan]{trade.strategy.upper()}[/cyan] "
            f"{trade.direction} "
            f"${trade.entry_price:,.2f} → ${trade.exit_price:,.2f} | "
            f"[{style}]{trade.pnl_pts:+.1f}pts (${pnl_dollars:+,.0f})[/{style}] | "
            f"{trade.bars_held} bars"
        )
        state["closed"].append(trade)
        state["closed"] = state["closed"][-20:]

    def on_bar(timestamp, close, active_trades):
        state["last_bar_time"] = timestamp
        state["last_close"] = close

        active_str = ""
        if active_trades:
            parts = []
            for strat, t in active_trades.items():
                dir_style = "green" if t.direction == "LONG" else "red"
                unrealized = 0
                if t.direction == "LONG":
                    unrealized = (close - t.entry_price) / t.entry_price * 25000
                else:
                    unrealized = (t.entry_price - close) / t.entry_price * 25000
                u_style = "green" if unrealized >= 0 else "red"
                parts.append(
                    f"[{dir_style}]{t.direction}[/{dir_style}] "
                    f"{strat.upper()} [{u_style}]{unrealized:+.0f}pts[/{u_style}] "
                    f"({t.bars_held}/{t.max_hold})"
                )
            active_str = " | ".join(parts)
        else:
            active_str = "[dim]no active trades[/dim]"

        console.print(
            f"  [dim]{timestamp}[/dim] "
            f"${close:,.2f} | "
            f"{active_str}"
        )

    # Display warmup stats
    console.print()
    stats = engine.get_stats()
    if stats:
        console.print(
            f"  [dim]Warmup complete:[/dim] "
            f"{stats.get('total_trades', 0)} historical trades | "
            f"PF {stats.get('profit_factor', 0):.2f} | "
            f"WR {stats.get('win_rate', 0):.1%}"
        )

    active_count = len(engine.active_trades)
    if active_count:
        console.print(f"  [bold yellow]{active_count} active trade(s) carried over[/bold yellow]")
        for strat, t in engine.active_trades.items():
            console.print(
                f"    {t.direction} {strat.upper()} @ ${t.entry_price:,.2f} "
                f"| TP ${t.target_price:,.2f} SL ${t.stop_price:,.2f} "
                f"| {t.bars_held} bars held"
            )

    console.print()
    console.print(Panel(
        f"[bold cyan]WATCHING[/bold cyan] {csv_path}\n"
        f"[dim]Polling every {poll_interval}s for new bars. Ctrl+C to stop.[/dim]",
        border_style="cyan",
    ))
    console.print()

    try:
        engine.watch(
            csv_path,
            poll_interval=poll_interval,
            on_signal=on_signal,
            on_trade_close=on_trade_close,
            on_bar=on_bar,
        )
    except KeyboardInterrupt:
        console.print("\n[bold yellow]Watch stopped.[/bold yellow]")
        stats = engine.get_stats()
        if stats and stats["total_trades"] > 0:
            console.print(
                f"  Session: {stats['total_trades']} trades | "
                f"PF {stats['profit_factor']:.2f} | "
                f"WR {stats['win_rate']:.1%} | "
                f"${stats['total_dollars']:+,.0f}"
            )
        console.print()


def _print_result(result: dict):
    """Pretty-print a single download result."""
    status = result["status"]
    icon = {"ok": "✓", "empty": "∅", "error": "✗", "current": "=", "updated": "↑"}.get(status, "?")
    click.echo(f"  {icon} {result['ticker']}/{result['interval']}: "
               f"{result['message']} ({result['rows']:,} rows)")


if __name__ == "__main__":
    cli()
