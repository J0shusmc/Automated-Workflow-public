"""Daily maintenance runner for the local stock database."""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd

from .config import DATA_DIR, INTERVALS
from .metadata import all_entries, csv_path, remove_entry, update_entry
from .retired_tickers import is_retired, replacement_for, retired_reason
from .ticker_lists import get_list

DAILY_INTERVALS = ["daily"]


@dataclass
class AuditIssue:
    severity: str
    ticker: str
    interval: str
    message: str


def _parse_timestamp_column(df: pd.DataFrame) -> str | None:
    if "timestamp" in df.columns:
        return "timestamp"
    if "DateTime" in df.columns:
        return "DateTime"
    return None


def _is_standard_polygon_csv(path: Path) -> bool:
    try:
        columns = pd.read_csv(path, nrows=0).columns
    except Exception:
        return False
    return "timestamp" in columns


def _inspect_csv(path: Path, ticker: str, interval: str) -> tuple[dict | None, list[AuditIssue]]:
    issues: list[AuditIssue] = []

    try:
        df = pd.read_csv(path)
    except Exception as exc:
        return None, [AuditIssue("error", ticker, interval, f"cannot read CSV: {exc}")]

    if df.empty:
        return None, [AuditIssue("error", ticker, interval, "CSV is empty")]

    ts_col = _parse_timestamp_column(df)
    if ts_col is None:
        return None, [AuditIssue("error", ticker, interval, "missing timestamp or DateTime column")]

    parsed_ts = pd.to_datetime(df[ts_col], errors="coerce")
    if parsed_ts.isna().any():
        issues.append(
            AuditIssue("error", ticker, interval, f"{int(parsed_ts.isna().sum())} invalid timestamps")
        )

    duplicate_count = int(df[ts_col].duplicated().sum())
    if duplicate_count:
        issues.append(
            AuditIssue("error", ticker, interval, f"{duplicate_count} duplicate timestamps")
        )

    valid_ts = parsed_ts.dropna()
    if not valid_ts.empty and not valid_ts.is_monotonic_increasing:
        issues.append(AuditIssue("error", ticker, interval, "timestamps are not sorted ascending"))

    lower_cols = {col.lower(): col for col in df.columns}
    required_ohlcv = ["open", "high", "low", "close", "volume"]
    if all(col in lower_cols for col in required_ohlcv):
        open_col = lower_cols["open"]
        high_col = lower_cols["high"]
        low_col = lower_cols["low"]
        close_col = lower_cols["close"]
        volume_col = lower_cols["volume"]

        bad_high_low = int((df[high_col] < df[low_col]).sum())
        if bad_high_low:
            issues.append(AuditIssue("error", ticker, interval, f"{bad_high_low} bars have high < low"))

        bad_high = int(((df[high_col] < df[open_col]) | (df[high_col] < df[close_col])).sum())
        if bad_high:
            issues.append(AuditIssue("error", ticker, interval, f"{bad_high} bars have high below open/close"))

        bad_low = int(((df[low_col] > df[open_col]) | (df[low_col] > df[close_col])).sum())
        if bad_low:
            issues.append(AuditIssue("error", ticker, interval, f"{bad_low} bars have low above open/close"))

        nonpositive_prices = 0
        for col in [open_col, high_col, low_col, close_col]:
            nonpositive_prices += int((df[col] <= 0).sum())
        if nonpositive_prices:
            issues.append(AuditIssue("error", ticker, interval, f"{nonpositive_prices} non-positive OHLC values"))

        negative_volume = int((df[volume_col] < 0).sum())
        if negative_volume:
            issues.append(AuditIssue("error", ticker, interval, f"{negative_volume} negative volume values"))

    if valid_ts.empty:
        return None, issues

    first = valid_ts.iloc[0]
    last = valid_ts.iloc[-1]
    info = {
        "ticker": ticker,
        "interval": interval,
        "first_date": _format_timestamp(first, interval),
        "last_date": _format_timestamp(last, interval),
        "row_count": int(len(df)),
    }
    return info, issues


def _format_timestamp(ts: pd.Timestamp, interval: str) -> str:
    if interval == "daily":
        return ts.strftime("%Y-%m-%d")
    return ts.strftime("%Y-%m-%d %H:%M:%S")


def validate_intervals(intervals: list[str] | None) -> list[str] | None:
    if intervals is None:
        return None
    invalid = [interval for interval in intervals if interval not in INTERVALS]
    if invalid:
        raise ValueError(f"unknown interval(s): {', '.join(invalid)}. Use: {', '.join(INTERVALS)}")
    return intervals


def daily_intervals() -> list[str]:
    return DAILY_INTERVALS.copy()


def discover_csv_files(intervals: list[str] | None = None) -> list[tuple[str, str, Path]]:
    """Return ticker/interval/path tuples for existing CSV files."""
    validate_intervals(intervals)
    selected = intervals or daily_intervals()
    discovered: list[tuple[str, str, Path]] = []
    for interval in selected:
        interval_dir = DATA_DIR / interval
        if not interval_dir.exists():
            continue
        for path in sorted(interval_dir.glob("*.csv")):
            discovered.append((path.stem.upper(), interval, path))
    return discovered


def repair_metadata_from_files(intervals: list[str] | None = None) -> list[AuditIssue]:
    """Create or refresh metadata entries from CSV contents."""
    issues: list[AuditIssue] = []
    for ticker, interval, path in discover_csv_files(intervals):
        info, file_issues = _inspect_csv(path, ticker, interval)
        issues.extend(file_issues)
        if info is None or not _is_standard_polygon_csv(path):
            continue
        update_entry(
            ticker=ticker,
            interval=interval,
            first_date=info["first_date"],
            last_date=info["last_date"],
            row_count=info["row_count"],
        )
    return issues


def normalize_standard_csv_files(
    intervals: list[str] | None = None,
    progress_every: int = 50,
) -> list[dict]:
    """Sort and dedupe standard timestamp CSV files in place."""
    validate_intervals(intervals)
    normalized: list[dict] = []
    files = discover_csv_files(intervals)

    for idx, (ticker, interval, path) in enumerate(files, start=1):
        if progress_every and (idx == 1 or idx % progress_every == 0 or idx == len(files)):
            print(f"Normalizing CSV files: {idx}/{len(files)} {ticker}/{interval}", flush=True)
        if is_retired(ticker) or not _is_standard_polygon_csv(path):
            continue

        df = pd.read_csv(path)
        if df.empty or "timestamp" not in df.columns:
            continue

        before_rows = len(df)
        before_first = str(df["timestamp"].iloc[0])
        before_last = str(df["timestamp"].iloc[-1])
        before_ts = pd.to_datetime(df["timestamp"], errors="coerce")
        had_invalid_or_unsorted = bool(before_ts.isna().any() or not before_ts.is_monotonic_increasing)

        df["timestamp_sort"] = pd.to_datetime(df["timestamp"], errors="coerce")
        df = df.dropna(subset=["timestamp_sort"])
        df = (
            df
            .drop_duplicates(subset=["timestamp"], keep="last")
            .sort_values("timestamp_sort")
            .drop(columns=["timestamp_sort"])
            .reset_index(drop=True)
        )

        changed = (
            had_invalid_or_unsorted
            or
            len(df) != before_rows
            or str(df["timestamp"].iloc[0]) != before_first
            or str(df["timestamp"].iloc[-1]) != before_last
        )

        if changed:
            df.to_csv(path, index=False)
            normalized.append({
                "ticker": ticker,
                "interval": interval,
                "before_rows": before_rows,
                "after_rows": len(df),
            })

    return normalized


def remove_retired_data(intervals: list[str] | None = None) -> list[dict]:
    """Delete CSV files and metadata entries for known retired symbols."""
    validate_intervals(intervals)
    selected = intervals or daily_intervals()
    removed: list[dict] = []

    for ticker, interval, path in discover_csv_files(selected):
        if not is_retired(ticker):
            continue
        if path.exists():
            path.unlink()
        remove_entry(ticker, interval)
        removed.append({
            "ticker": ticker,
            "interval": interval,
            "reason": retired_reason(ticker),
        })

    for key, entry in all_entries().items():
        ticker = entry["ticker"]
        interval = entry["interval"]
        if interval in selected and is_retired(ticker):
            remove_entry(ticker, interval)
            removed.append({
                "ticker": ticker,
                "interval": interval,
                "reason": retired_reason(ticker),
            })

    return removed


def _targets_from_metadata(intervals: list[str] | None) -> list[tuple[str, str]]:
    entries = all_entries()
    targets: list[tuple[str, str]] = []
    for entry in entries.values():
        if is_retired(entry["ticker"]):
            continue
        interval = entry["interval"]
        if intervals and interval not in intervals:
            continue
        targets.append((entry["ticker"], interval))
    return sorted(set(targets))


def _targets_from_lists(list_names: list[str], intervals: list[str]) -> list[tuple[str, str]]:
    targets: list[tuple[str, str]] = []
    for list_name in list_names:
        for ticker in get_list(list_name):
            ticker = replacement_for(ticker)
            if ticker is None:
                continue
            for interval in intervals:
                targets.append((ticker.upper(), interval))
    return sorted(set(targets))


def update_database(
    intervals: list[str] | None = None,
    list_names: list[str] | None = None,
    download_missing: bool = False,
    overlap_days: int = 3,
    workers: int = 1,
) -> list[dict]:
    """Update known data and optionally expand missing ticker/list combinations."""
    validate_intervals(intervals)
    if workers < 1:
        raise ValueError("workers must be at least 1")
    intervals = intervals or daily_intervals()
    list_names = list_names or []
    results: list[dict] = []

    targets = _targets_from_metadata(intervals)
    if download_missing and list_names:
        targets = sorted(set(targets) | set(_targets_from_lists(list_names, intervals)))
    if not targets:
        return results

    from .client import PolygonClient
    from .downloader import download_ticker, update_ticker

    thread_state = threading.local()

    def worker_client() -> PolygonClient:
        client = getattr(thread_state, "client", None)
        if client is None:
            client = PolygonClient()
            thread_state.client = client
        return client

    def update_target(ticker: str, interval: str, client: PolygonClient | None = None) -> dict:
        path = csv_path(ticker, interval)
        if is_retired(ticker):
            return {
                "ticker": ticker,
                "interval": interval,
                "status": "retired",
                "rows": 0,
                "message": retired_reason(ticker),
            }
        elif path.exists():
            if not _is_standard_polygon_csv(path):
                return {
                    "ticker": ticker,
                    "interval": interval,
                    "status": "skipped",
                    "rows": 0,
                    "message": "non-Polygon CSV schema; audit only",
                }
            else:
                client = client or worker_client()
                return update_ticker(ticker, interval, client=client, overlap_days=overlap_days)
        elif download_missing:
            client = client or worker_client()
            return download_ticker(ticker, interval, client=client)
        else:
            return {
                "ticker": ticker,
                "interval": interval,
                "status": "missing",
                "rows": 0,
                "message": "CSV missing and --download-missing was not set",
            }

    if workers == 1:
        client = PolygonClient()
        for idx, (ticker, interval) in enumerate(targets, start=1):
            print(f"[{idx}/{len(targets)}] {ticker}/{interval}")
            result = update_target(ticker, interval, client=client)
            results.append(result)
            print(f"  {result['status']}: {result['message']}")
        return results

    print(f"Using {workers} update worker(s) for {len(targets)} ticker/interval target(s).")
    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = {
            executor.submit(update_target, ticker, interval): (ticker, interval)
            for ticker, interval in targets
        }
        for idx, future in enumerate(as_completed(futures), start=1):
            ticker, interval = futures[future]
            print(f"[{idx}/{len(targets)}] {ticker}/{interval}")
            try:
                result = future.result()
            except Exception as exc:
                result = {
                    "ticker": ticker,
                    "interval": interval,
                    "status": "error",
                    "rows": 0,
                    "message": str(exc),
                }
            results.append(result)
            print(f"  {result['status']}: {result['message']}")

    return results


def audit_database(
    intervals: list[str] | None = None,
    max_stale_days: int = 5,
    fail_on_stale: bool = True,
) -> list[AuditIssue]:
    """Audit CSV integrity, metadata sync, and freshness after an update."""
    issues: list[AuditIssue] = []
    metadata = all_entries()
    now = datetime.now()

    for ticker, interval, path in discover_csv_files(intervals):
        info, file_issues = _inspect_csv(path, ticker, interval)
        issues.extend(file_issues)
        if info is None:
            continue

        key = f"{ticker}|{interval}"
        entry = metadata.get(key)
        if is_retired(ticker):
            issues.append(
                AuditIssue("info", ticker, interval, f"inactive symbol retained historically: {retired_reason(ticker)}")
            )
        elif _is_standard_polygon_csv(path):
            if entry is None:
                issues.append(AuditIssue("error", ticker, interval, "missing metadata entry"))
            else:
                if int(entry.get("row_count", -1)) != info["row_count"]:
                    issues.append(
                        AuditIssue(
                            "error",
                            ticker,
                            interval,
                            f"metadata row_count {entry.get('row_count')} != CSV rows {info['row_count']}",
                        )
                    )
                if str(entry.get("last_date")) != info["last_date"]:
                    issues.append(
                        AuditIssue(
                            "error",
                            ticker,
                            interval,
                            f"metadata last_date {entry.get('last_date')} != CSV last {info['last_date']}",
                        )
                    )

        last_dt = pd.to_datetime(info["last_date"]).to_pydatetime()
        stale_cutoff = now - timedelta(days=max_stale_days)
        if last_dt < stale_cutoff and not is_retired(ticker):
            severity = "error" if fail_on_stale else "warning"
            issues.append(
                AuditIssue(
                    severity,
                    ticker,
                    interval,
                    f"last bar {info['last_date']} is older than {max_stale_days} days",
                )
            )

    selected_intervals = set(intervals or daily_intervals())
    csv_keys = {f"{ticker}|{interval}" for ticker, interval, _ in discover_csv_files(intervals)}
    for key, entry in metadata.items():
        if is_retired(entry["ticker"]):
            continue
        if entry["interval"] in selected_intervals and key not in csv_keys:
            issues.append(
                AuditIssue("error", entry["ticker"], entry["interval"], "metadata exists but CSV is missing")
            )

    return issues


def print_summary(results: list[dict], issues: list[AuditIssue]) -> None:
    status_counts: dict[str, int] = {}
    total_rows = 0
    for result in results:
        status_counts[result["status"]] = status_counts.get(result["status"], 0) + 1
        total_rows += int(result.get("rows") or 0)

    print("\nUpdate summary")
    print("--------------")
    for status, count in sorted(status_counts.items()):
        print(f"{status:>10}: {count}")
    print(f"{'rows':>10}: {total_rows:,}")

    errors = [issue for issue in issues if issue.severity == "error"]
    warnings = [issue for issue in issues if issue.severity == "warning"]
    infos = [issue for issue in issues if issue.severity == "info"]

    print("\nAudit summary")
    print("-------------")
    print(f"{'errors':>10}: {len(errors)}")
    print(f"{'warnings':>10}: {len(warnings)}")
    print(f"{'info':>10}: {len(infos)}")

    visible_issues = errors + warnings
    for issue in visible_issues[:50]:
        print(f"  [{issue.severity.upper()}] {issue.ticker}/{issue.interval}: {issue.message}")
    if len(visible_issues) > 50:
        print(f"  ... {len(visible_issues) - 50} more errors/warnings not shown")
