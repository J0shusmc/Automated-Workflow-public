"""Data access API — the main interface other projects use."""

from pathlib import Path

import pandas as pd

from .config import DATA_DIR, INTERVALS
from .metadata import csv_path, tickers_for_interval, get_entry, all_entries


def get_bars(
    tickers: str | list[str],
    interval: str,
    start: str | None = None,
    end: str | None = None,
) -> pd.DataFrame | dict[str, pd.DataFrame]:
    """Load OHLCV bars from local CSV files.

    Args:
        tickers: Single ticker string or list of tickers.
        interval: "daily", "1hour", or "5min".
        start: Optional start date filter "YYYY-MM-DD".
        end: Optional end date filter "YYYY-MM-DD".

    Returns:
        Single ticker → DataFrame.
        Multiple tickers → dict of {ticker: DataFrame}.
    """
    single = isinstance(tickers, str)
    if single:
        tickers = [tickers]

    result = {}
    for ticker in tickers:
        path = csv_path(ticker, interval)
        if not path.exists():
            raise FileNotFoundError(
                f"No data for {ticker}/{interval}. "
                f"Run: stockdb download {ticker} --interval {interval}"
            )

        df = pd.read_csv(path)

        if start:
            df = df[df["timestamp"] >= start]
        if end:
            df = df[df["timestamp"] <= end]

        df = df.reset_index(drop=True)
        result[ticker] = df

    if single:
        return result[tickers[0]]
    return result


def get_bars_wide(
    tickers: list[str],
    interval: str,
    fields: list[str] | None = None,
    start: str | None = None,
    end: str | None = None,
) -> pd.DataFrame:
    """Load data as a wide DataFrame (timestamps as index, tickers as columns).

    Args:
        tickers: List of tickers.
        interval: "daily", "1hour", or "5min".
        fields: Columns to include, default ["close"].
        start: Optional start date filter.
        end: Optional end date filter.

    Returns:
        Wide DataFrame with MultiIndex columns (field, ticker).
    """
    fields = fields or ["close"]
    data = get_bars(tickers, interval, start=start, end=end)

    panels = {}
    for field in fields:
        wide = pd.DataFrame()
        for ticker, df in data.items():
            series = df.set_index("timestamp")[field]
            wide[ticker] = series
        panels[field] = wide

    if len(fields) == 1:
        return panels[fields[0]]

    return pd.concat(panels, axis=1)


def available_tickers(interval: str) -> list[str]:
    """Return list of tickers with downloaded data for an interval."""
    return tickers_for_interval(interval)


def load_external_csv(path: str | Path, start: str | None = None,
                      end: str | None = None) -> pd.DataFrame:
    """Load an external CSV (e.g. NinjaTrader export) and normalize columns.

    Handles column name variations and maps them to the standard schema:
    timestamp, open, high, low, close, volume. Extra columns are dropped.

    Args:
        path: Path to CSV file.
        start: Optional start date filter "YYYY-MM-DD".
        end: Optional end date filter "YYYY-MM-DD".

    Returns:
        DataFrame with standard column names, ready for compute_features().
    """
    df = pd.read_csv(path)

    # Build a lowercase mapping for flexible column matching
    col_map = {}
    for col in df.columns:
        lc = col.strip().lower()
        if lc in ("datetime", "date", "time", "date/time"):
            col_map[col] = "timestamp"
        elif lc == "open":
            col_map[col] = "open"
        elif lc == "high":
            col_map[col] = "high"
        elif lc == "low":
            col_map[col] = "low"
        elif lc == "close" or lc == "last":
            col_map[col] = "close"
        elif lc == "volume" or lc == "vol":
            col_map[col] = "volume"

    df = df.rename(columns=col_map)

    # Keep only standard columns
    required = ["timestamp", "open", "high", "low", "close", "volume"]
    missing = [c for c in required if c not in df.columns]
    if missing:
        raise ValueError(f"Missing columns after mapping: {missing}. "
                         f"Original columns: {list(pd.read_csv(path, nrows=0).columns)}")

    df = df[required]

    # Parse timestamps
    df["timestamp"] = pd.to_datetime(df["timestamp"])
    df = df.sort_values("timestamp").reset_index(drop=True)

    if start:
        df = df[df["timestamp"] >= start]
    if end:
        df = df[df["timestamp"] <= end]

    return df.reset_index(drop=True)


def coverage(ticker: str) -> dict:
    """Return download coverage for a ticker across all intervals."""
    result = {}
    for interval in INTERVALS:
        entry = get_entry(ticker, interval)
        if entry:
            result[interval] = {
                "first_date": entry["first_date"],
                "last_date": entry["last_date"],
                "row_count": entry["row_count"],
                "last_updated": entry["last_updated"],
            }
    return result
