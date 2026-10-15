#!/usr/bin/env python3
"""Run the daily + hourly stock database update and audit.

Examples:
  python intraday_update.py --repair-metadata
  python intraday_update.py --interval 1hour
  python intraday_update.py --interval daily --interval 1hour
  python intraday_update.py --list sp500 --list etfs --download-missing
"""

from __future__ import annotations

import argparse
import sys

from stockdb.daily_update import (
    audit_database,
    normalize_standard_csv_files,
    print_summary,
    remove_retired_data,
    repair_metadata_from_files,
    update_database,
    validate_intervals,
)

UPDATE_INTERVALS = ["daily", "1hour"]


def _parse_intervals(values: list[str] | None) -> list[str]:
    if not values:
        return UPDATE_INTERVALS.copy()
    intervals: list[str] = []
    for value in values:
        intervals.extend(part.strip() for part in value.split(",") if part.strip())
    return intervals


def _validate_update_intervals(intervals: list[str]) -> None:
    validate_intervals(intervals)
    invalid = [interval for interval in intervals if interval not in UPDATE_INTERVALS]
    if invalid:
        raise ValueError(
            f"unsupported interval: {', '.join(invalid)}. Use: {', '.join(UPDATE_INTERVALS)}"
        )


def _positive_int(value: str) -> int:
    parsed = int(value)
    if parsed < 1:
        raise argparse.ArgumentTypeError("must be at least 1")
    return parsed


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Update and audit daily and hourly stock bars."
    )
    parser.add_argument(
        "-i",
        "--interval",
        action="append",
        help="Interval to update/audit: daily, 1hour. Can be repeated or comma-separated.",
    )
    parser.add_argument(
        "-l",
        "--list",
        dest="ticker_lists",
        action="append",
        default=[],
        help="Ticker list to ensure exists when --download-missing is used.",
    )
    parser.add_argument(
        "--keep-retired",
        action="store_true",
        help="Keep known delisted/ticker-changed CSVs instead of removing them before update.",
    )
    parser.add_argument(
        "--download-missing",
        action="store_true",
        help="Download ticker/list/interval combinations that do not have CSV files yet.",
    )
    parser.add_argument(
        "--repair-metadata",
        action="store_true",
        help="Rebuild metadata entries from existing CSV files before updating.",
    )
    parser.add_argument(
        "--overlap-days",
        type=int,
        default=3,
        help="Days of overlap to refetch and dedupe during incremental updates.",
    )
    parser.add_argument(
        "--workers",
        type=_positive_int,
        default=4,
        help="Number of parallel ticker/interval update workers.",
    )
    parser.add_argument(
        "--max-stale-days",
        type=int,
        default=3,
        help="Fail audit when the newest bar is older than this many days.",
    )
    parser.add_argument(
        "--no-fail-on-stale",
        action="store_true",
        help="Report stale files as warnings instead of errors.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    intervals = _parse_intervals(args.interval)

    try:
        _validate_update_intervals(intervals)

        if not args.keep_retired:
            print("Checking retired ticker files...", flush=True)
            removed = remove_retired_data(intervals)
            if removed:
                print(f"Removed {len(removed)} retired ticker file(s)/metadata entrie(s).")
                for item in removed[:25]:
                    print(f"  {item['ticker']}/{item['interval']}: {item['reason']}")
                if len(removed) > 25:
                    print(f"  ... {len(removed) - 25} more removed")
            else:
                print("No retired ticker files found.", flush=True)

        print("Normalizing CSV files before update...", flush=True)
        normalized = normalize_standard_csv_files(intervals)
        if normalized:
            print(f"Normalized {len(normalized)} CSV file(s).")
            for item in normalized[:25]:
                print(
                    f"  {item['ticker']}/{item['interval']}: "
                    f"{item['before_rows']:,} -> {item['after_rows']:,} rows"
                )
            if len(normalized) > 25:
                print(f"  ... {len(normalized) - 25} more normalized")
        else:
            print("No CSV normalization changes needed.", flush=True)

        if args.repair_metadata:
            print("Repairing metadata from CSV files...", flush=True)
            repair_issues = repair_metadata_from_files(intervals)
            if repair_issues:
                print(f"Metadata repair found {len(repair_issues)} CSV issue(s).")

        print(f"Updating daily + hourly stock bars with {args.workers} worker(s)...", flush=True)
        results = update_database(
            intervals=intervals,
            list_names=args.ticker_lists,
            download_missing=args.download_missing,
            overlap_days=args.overlap_days,
            workers=args.workers,
        )

        print("\nAuditing daily + hourly stock bars...", flush=True)
        issues = audit_database(
            intervals=intervals,
            max_stale_days=args.max_stale_days,
            fail_on_stale=not args.no_fail_on_stale,
        )
    except ValueError as exc:
        print(f"ERROR: {exc}")
        return 2

    print_summary(results, issues)

    update_errors = [result for result in results if result["status"] == "error"]
    audit_errors = [issue for issue in issues if issue.severity == "error"]
    if update_errors or audit_errors:
        print(
            f"\nFAILED: {len(update_errors)} update error(s), "
            f"{len(audit_errors)} audit error(s)."
        )
        return 1

    print("\nOK: daily + hourly update and audit completed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
