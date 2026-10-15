"""Data quality checks: gap detection, OHLCV sanity, staleness."""

from datetime import datetime, timedelta

import pandas as pd

from .config import INTERVALS
from .metadata import csv_path, get_entry


def validate_ticker(ticker: str, interval: str) -> list[dict]:
    """Run all validation checks on a ticker/interval. Returns list of issues."""
    path = csv_path(ticker, interval)
    if not path.exists():
        return [{"check": "exists", "severity": "error", "message": f"No data file found at {path}"}]

    df = pd.read_csv(path)
    issues = []

    if df.empty:
        return [{"check": "empty", "severity": "error", "message": "CSV is empty"}]

    # OHLCV sanity checks
    issues.extend(_check_ohlcv(df, ticker, interval))

    # Gap detection
    issues.extend(_check_gaps(df, ticker, interval))

    # Staleness
    issues.extend(_check_staleness(ticker, interval))

    return issues


def _check_ohlcv(df: pd.DataFrame, ticker: str, interval: str) -> list[dict]:
    """Check OHLCV data sanity."""
    issues = []

    # High >= Low
    bad = df[df["high"] < df["low"]]
    if len(bad) > 0:
        issues.append({
            "check": "high_gte_low",
            "severity": "error",
            "message": f"{len(bad)} bars where high < low",
        })

    # High >= Open and High >= Close
    bad = df[(df["high"] < df["open"]) | (df["high"] < df["close"])]
    if len(bad) > 0:
        issues.append({
            "check": "high_gte_oc",
            "severity": "warning",
            "message": f"{len(bad)} bars where high < open or high < close",
        })

    # Low <= Open and Low <= Close
    bad = df[(df["low"] > df["open"]) | (df["low"] > df["close"])]
    if len(bad) > 0:
        issues.append({
            "check": "low_lte_oc",
            "severity": "warning",
            "message": f"{len(bad)} bars where low > open or low > close",
        })

    # Negative or zero prices
    for col in ["open", "high", "low", "close"]:
        bad = df[df[col] <= 0]
        if len(bad) > 0:
            issues.append({
                "check": f"positive_{col}",
                "severity": "error",
                "message": f"{len(bad)} bars with {col} <= 0",
            })

    # Negative volume
    bad = df[df["volume"] < 0]
    if len(bad) > 0:
        issues.append({
            "check": "positive_volume",
            "severity": "error",
            "message": f"{len(bad)} bars with negative volume",
        })

    # Duplicate timestamps
    dupes = df["timestamp"].duplicated().sum()
    if dupes > 0:
        issues.append({
            "check": "duplicates",
            "severity": "warning",
            "message": f"{dupes} duplicate timestamps",
        })

    return issues


def _check_gaps(df: pd.DataFrame, ticker: str, interval: str) -> list[dict]:
    """Detect gaps in the data (missing trading days/hours)."""
    issues = []

    if interval == "daily":
        dates = pd.to_datetime(df["timestamp"])
        # Check for gaps > 4 days (weekends are 2 days, holidays can be 3)
        diffs = dates.diff().dropna()
        big_gaps = diffs[diffs > timedelta(days=5)]
        if len(big_gaps) > 0:
            gap_dates = [
                f"{dates.iloc[i-1].strftime('%Y-%m-%d')} → {dates.iloc[i].strftime('%Y-%m-%d')} ({d.days}d)"
                for i, d in big_gaps.items()
            ]
            issues.append({
                "check": "daily_gaps",
                "severity": "warning",
                "message": f"{len(big_gaps)} gaps > 5 days: {', '.join(gap_dates[:5])}",
            })

    return issues


def _check_staleness(ticker: str, interval: str) -> list[dict]:
    """Check if data is stale (not recently updated)."""
    issues = []
    entry = get_entry(ticker, interval)
    if entry:
        last_updated = datetime.fromisoformat(entry["last_updated"])
        age = datetime.now() - last_updated
        if age > timedelta(days=7):
            issues.append({
                "check": "staleness",
                "severity": "info",
                "message": f"Last updated {age.days} days ago ({entry['last_updated'][:10]})",
            })
    return issues
