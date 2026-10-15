"""Track download status per ticker/interval in metadata.json."""

import json
import threading
from datetime import datetime
from pathlib import Path

from .config import DATA_DIR, METADATA_FILE

_METADATA_LOCK = threading.RLock()


def _load() -> dict:
    if METADATA_FILE.exists():
        return json.loads(METADATA_FILE.read_text())
    return {}


def _save(data: dict):
    METADATA_FILE.parent.mkdir(parents=True, exist_ok=True)
    METADATA_FILE.write_text(json.dumps(data, indent=2))


def _key(ticker: str, interval: str) -> str:
    return f"{ticker}|{interval}"


def get_entry(ticker: str, interval: str) -> dict | None:
    """Get metadata for a ticker/interval, or None."""
    with _METADATA_LOCK:
        data = _load()
    return data.get(_key(ticker, interval))


def update_entry(
    ticker: str,
    interval: str,
    first_date: str,
    last_date: str,
    row_count: int,
):
    """Update or create metadata entry after a download."""
    with _METADATA_LOCK:
        data = _load()
        data[_key(ticker, interval)] = {
            "ticker": ticker,
            "interval": interval,
            "first_date": first_date,
            "last_date": last_date,
            "row_count": row_count,
            "last_updated": datetime.now().isoformat(),
        }
        _save(data)


def remove_entry(ticker: str, interval: str):
    """Remove metadata for a ticker/interval if present."""
    with _METADATA_LOCK:
        data = _load()
        data.pop(_key(ticker, interval), None)
        _save(data)


def all_entries() -> dict:
    """Return all metadata entries."""
    with _METADATA_LOCK:
        return _load()


def tickers_for_interval(interval: str) -> list[str]:
    """Return list of tickers that have data for a given interval."""
    with _METADATA_LOCK:
        data = _load()
    return sorted(
        entry["ticker"]
        for entry in data.values()
        if entry["interval"] == interval
    )


def csv_path(ticker: str, interval: str) -> Path:
    """Return the CSV file path for a ticker/interval."""
    return DATA_DIR / interval / f"{ticker}.csv"
