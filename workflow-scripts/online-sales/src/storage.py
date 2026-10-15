from __future__ import annotations

from pathlib import Path
import json
from typing import Any


def ensure_parent(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)


def load_processed_ids(path: Path) -> set[str]:
    if not path.exists():
        return set()

    data = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(data, list):
        return {str(item) for item in data}
    if isinstance(data, dict):
        return {str(item) for item in data.get("processed_email_ids", [])}
    raise ValueError(f"Unsupported processed email format: {path}")


def save_processed_ids(path: Path, processed_ids: set[str]) -> None:
    ensure_parent(path)
    payload = {"processed_email_ids": sorted(processed_ids)}
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def append_jsonl(path: Path, record: dict[str, Any]) -> None:
    ensure_parent(path)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, sort_keys=True) + "\n")


def write_json(path: Path, payload: Any) -> None:
    ensure_parent(path)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def read_json(path: Path) -> Any:
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []

    records: list[dict[str, Any]] = []
    for line_number, raw_line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        line = raw_line.strip()
        if not line:
            continue

        record = json.loads(line)
        if not isinstance(record, dict):
            raise ValueError(f"Expected object on line {line_number} of {path}")
        records.append(record)
    return records
