"""Batch download engine with resumable, incremental updates."""

import logging
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd
from tqdm import tqdm

from .client import PolygonClient
from .config import DATA_DIR, DEFAULT_START_DATE, DEFAULT_START_DATES, INTERVALS
from .metadata import csv_path, get_entry, update_entry

log = logging.getLogger(__name__)


def download_ticker(
    ticker: str,
    interval: str,
    start: str | None = None,
    end: str | None = None,
    client: PolygonClient | None = None,
) -> dict:
    """Download data for a single ticker/interval and save to CSV.

    Returns dict with status info: {ticker, interval, status, rows, message}.
    """
    client = client or PolygonClient()
    start = start or DEFAULT_START_DATES.get(interval, DEFAULT_START_DATE)
    end = end or datetime.now().strftime("%Y-%m-%d")

    out_path = csv_path(ticker, interval)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    try:
        df = client.get_bars(ticker, interval, start, end)

        if df.empty:
            return {
                "ticker": ticker,
                "interval": interval,
                "status": "empty",
                "rows": 0,
                "message": "No data returned",
            }

        df.to_csv(out_path, index=False)

        update_entry(
            ticker=ticker,
            interval=interval,
            first_date=str(df["timestamp"].iloc[0]),
            last_date=str(df["timestamp"].iloc[-1]),
            row_count=len(df),
        )

        return {
            "ticker": ticker,
            "interval": interval,
            "status": "ok",
            "rows": len(df),
            "message": f"{df['timestamp'].iloc[0]} → {df['timestamp'].iloc[-1]}",
        }

    except Exception as e:
        log.error(f"Failed {ticker}/{interval}: {e}")
        return {
            "ticker": ticker,
            "interval": interval,
            "status": "error",
            "rows": 0,
            "message": str(e),
        }


def download_batch(
    tickers: list[str],
    intervals: list[str],
    start: str | None = None,
    end: str | None = None,
) -> list[dict]:
    """Download multiple tickers × intervals with progress bar."""
    client = PolygonClient()
    results = []

    tasks = [(t, iv) for t in tickers for iv in intervals]
    pbar = tqdm(tasks, desc="Downloading", unit="file")

    for ticker, interval in pbar:
        pbar.set_postfix_str(f"{ticker}/{interval}")
        result = download_ticker(ticker, interval, start, end, client=client)
        results.append(result)

    # Summary
    ok = sum(1 for r in results if r["status"] == "ok")
    empty = sum(1 for r in results if r["status"] == "empty")
    errors = sum(1 for r in results if r["status"] == "error")
    total_rows = sum(r["rows"] for r in results)

    print(f"\nDone: {ok} ok, {empty} empty, {errors} errors, {total_rows:,} total rows")

    if errors:
        print("\nFailed:")
        for r in results:
            if r["status"] == "error":
                print(f"  {r['ticker']}/{r['interval']}: {r['message']}")

    return results


def update_ticker(
    ticker: str,
    interval: str,
    client: PolygonClient | None = None,
    overlap_days: int = 3,
) -> dict:
    """Incrementally update a ticker, fetching a small overlap and deduping."""
    entry = get_entry(ticker, interval)
    if entry is None:
        return download_ticker(ticker, interval, client=client)

    last = entry["last_date"][:10]  # extract date portion
    last_dt = datetime.strptime(last, "%Y-%m-%d")
    if interval == "daily":
        start_dt = last_dt + timedelta(days=1)
    else:
        start_dt = last_dt - timedelta(days=max(overlap_days, 0))
    start = start_dt.strftime("%Y-%m-%d")
    end = datetime.now().strftime("%Y-%m-%d")

    if interval == "daily" and start > end:
        return {
            "ticker": ticker,
            "interval": interval,
            "status": "current",
            "rows": entry["row_count"],
            "message": f"Already up to date through {last}",
        }

    client = client or PolygonClient()

    try:
        df_new = client.get_bars(ticker, interval, start, end)

        if df_new.empty:
            return {
                "ticker": ticker,
                "interval": interval,
                "status": "current",
                "rows": entry["row_count"],
                "message": f"No new data since {last}",
            }

        path = csv_path(ticker, interval)

        # Append to existing CSV
        df_existing = pd.read_csv(path)
        df_combined = pd.concat([df_existing, df_new], ignore_index=True)
        df_combined = df_combined.drop_duplicates(
            subset=["timestamp"], keep="last"
        ).reset_index(drop=True)
        df_combined["timestamp_sort"] = pd.to_datetime(df_combined["timestamp"])
        df_combined = (
            df_combined
            .sort_values("timestamp_sort")
            .drop(columns=["timestamp_sort"])
            .reset_index(drop=True)
        )
        df_combined.to_csv(path, index=False)

        update_entry(
            ticker=ticker,
            interval=interval,
            first_date=str(df_combined["timestamp"].iloc[0]),
            last_date=str(df_combined["timestamp"].iloc[-1]),
            row_count=len(df_combined),
        )

        new_rows = len(df_combined) - entry["row_count"]
        return {
            "ticker": ticker,
            "interval": interval,
            "status": "updated",
            "rows": len(df_combined),
            "message": f"+{new_rows} new bars through {df_combined['timestamp'].iloc[-1]}",
        }

    except Exception as e:
        log.error(f"Update failed {ticker}/{interval}: {e}")
        return {
            "ticker": ticker,
            "interval": interval,
            "status": "error",
            "rows": entry["row_count"],
            "message": str(e),
        }


def update_all(intervals: list[str] | None = None, overlap_days: int = 3) -> list[dict]:
    """Incrementally update all previously downloaded tickers."""
    from .metadata import all_entries

    entries = all_entries()
    if not entries:
        print("No data downloaded yet. Use 'stockdb download' first.")
        return []

    client = PolygonClient()
    results = []

    # Filter by requested intervals
    tasks = []
    for entry in entries.values():
        if intervals and entry["interval"] not in intervals:
            continue
        tasks.append((entry["ticker"], entry["interval"]))

    pbar = tqdm(tasks, desc="Updating", unit="file")
    for ticker, interval in pbar:
        pbar.set_postfix_str(f"{ticker}/{interval}")
        result = update_ticker(ticker, interval, client=client, overlap_days=overlap_days)
        results.append(result)

    updated = sum(1 for r in results if r["status"] == "updated")
    current = sum(1 for r in results if r["status"] == "current")
    errors = sum(1 for r in results if r["status"] == "error")

    print(f"\nDone: {updated} updated, {current} already current, {errors} errors")
    return results
