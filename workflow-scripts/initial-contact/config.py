from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import os


def _load_env_file(path: Path = Path(".env")) -> None:
    if not path.exists():
        return

    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue

        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)


    unfilled = sorted(k for k, v in os.environ.items() if v.startswith("ENTER_") and v.endswith("_HERE"))
    if unfilled:
        raise SystemExit(f"Fill in {', '.join(unfilled)} in {path} before running this workflow.")


def _bool_env(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "y", "on"}


def _int_env(name: str, default: int) -> int:
    value = os.getenv(name)
    if value is None or not value.strip():
        return default
    return int(value)


def _csv_env(name: str, default: str = "") -> frozenset[str]:
    value = os.getenv(name, default)
    return frozenset(item.strip() for item in value.split(",") if item.strip())


@dataclass(frozen=True)
class MailReaderConfig:
    email_host: str
    email_port: int
    email_username: str
    email_password: str
    email_folder: str
    email_poll_seconds: int
    email_search_criteria: str
    email_lookback_hours: int
    email_mark_read: bool
    email_from_filter: str
    email_subject_filter: str
    contact_output_path: Path
    latest_contact_path: Path
    processed_emails_path: Path
    blocked_contacts_path: Path
    blocked_contact_ips: frozenset[str]
    blocked_contact_names: frozenset[str]
    mail_reader_log_path: Path

    @classmethod
    def from_env(cls) -> "MailReaderConfig":
        _load_env_file()

        return cls(
            email_host=os.getenv("EMAIL_HOST", "imap.gmail.com"),
            email_port=_int_env("EMAIL_PORT", 993),
            email_username=os.getenv("EMAIL_USERNAME", ""),
            email_password="".join(os.getenv("EMAIL_PASSWORD", "").split()),
            email_folder=os.getenv("EMAIL_FOLDER", os.getenv("EMAIL_SALES_FOLDER", "INBOX")),
            email_poll_seconds=_int_env("EMAIL_POLL_SECONDS", 300),
            email_search_criteria=os.getenv("EMAIL_SEARCH_CRITERIA", "UNSEEN"),
            email_lookback_hours=_int_env("EMAIL_LOOKBACK_HOURS", 120),
            email_mark_read=_bool_env("EMAIL_MARK_READ", True),
            email_from_filter=os.getenv("EMAIL_FROM_FILTER", ""),
            email_subject_filter=os.getenv("EMAIL_SUBJECT_FILTER", "New Website Contact"),
            contact_output_path=Path(os.getenv("CONTACT_OUTPUT_PATH", "data/pending_contacts.jsonl")),
            latest_contact_path=Path(os.getenv("LATEST_CONTACT_PATH", "data/latest_contact.json")),
            processed_emails_path=Path(os.getenv("PROCESSED_EMAILS_PATH", "data/processed_emails.json")),
            blocked_contacts_path=Path(
                os.getenv("BLOCKED_CONTACTS_PATH", "data/blocked_contacts.jsonl")
            ),
            blocked_contact_ips=_csv_env(
                "BLOCKED_CONTACT_IPS",
                "77.90.185.5,141.98.11.224,80.94.95.173,158.173.20.36",
            ),
            blocked_contact_names=_csv_env(
                "BLOCKED_CONTACT_NAMES", "RobertWew,HarryPhefs"
            ),
            mail_reader_log_path=Path(os.getenv("MAIL_READER_LOG_PATH", "logs/mail_reader.log")),
        )
