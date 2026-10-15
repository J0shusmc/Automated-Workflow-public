#!/usr/bin/env python3
"""Run the daily stock database update and audit.

Examples:
  python daily_update.py --repair-metadata
  python daily_update.py --list sp500 --list etfs --download-missing
"""

from __future__ import annotations

import argparse
import sys

from stockdb.daily_update import (
    audit_database,
    daily_intervals,
    print_summary,
    remove_retired_data,
    repair_metadata_from_files,
    update_database,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Update and audit daily stock bars only."
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
        "--max-stale-days",
        type=int,
        default=5,
        help="Fail audit when the newest bar is older than this many days.",
    )
    parser.add_argument(
        "--no-fail-on-stale",
        action="store_true",
        default=True,
        help="Report stale files as warnings instead of errors.",
    )
    parser.add_argument(
        "--fail-on-stale",
        dest="no_fail_on_stale",
        action="store_false",
        help="Fail the script when stale files are found.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    intervals = daily_intervals()

    try:
        if not args.keep_retired:
            removed = remove_retired_data(intervals)
            if removed:
                print(f"Removed {len(removed)} retired daily ticker file(s)/metadata entrie(s).")
                for item in removed[:25]:
                    print(f"  {item['ticker']}/{item['interval']}: {item['reason']}")
                if len(removed) > 25:
                    print(f"  ... {len(removed) - 25} more removed")

        if args.repair_metadata:
            print("Repairing metadata from CSV files...")
            repair_issues = repair_metadata_from_files(intervals)
            if repair_issues:
                print(f"Metadata repair found {len(repair_issues)} CSV issue(s).")

        print("Updating daily stock bars...")
        results = update_database(
            intervals=intervals,
            list_names=args.ticker_lists,
            download_missing=args.download_missing,
        )

        print("\nAuditing stock database...")
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

    print("\nOK: database update and audit completed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
