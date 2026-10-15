from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import json
import os
from typing import Any


ROOT_DIR = Path(__file__).resolve().parents[1]


def load_dotenv(path: Path | None = None) -> None:
    env_path = path or ROOT_DIR / ".env"
    if not env_path.exists():
        return

    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue

        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)


    unfilled = sorted(k for k, v in os.environ.items() if v.startswith("ENTER_") and v.endswith("_HERE"))
    if unfilled:
        raise SystemExit(f"Fill in {', '.join(unfilled)} in {env_path} before running this workflow.")


def env_bool(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class MailReaderConfig:
    email_host: str
    email_port: int
    email_username: str
    email_password: str
    email_sales_folder: str
    email_poll_seconds: int
    email_search_criteria: str
    email_lookback_hours: int
    email_mark_read: bool
    email_ignore_processed: bool
    email_test_mode: bool
    email_from_filter: str | None
    email_subject_filter: str | None
    buyer_email_regex: str | None
    product_regex: str | None
    order_id_regex: str | None
    payment_reference_regex: str | None
    sales_output_path: Path
    latest_sale_path: Path
    processed_emails_path: Path
    mail_reader_log_path: Path

    @classmethod
    def from_env(cls) -> "MailReaderConfig":
        load_dotenv()

        username = os.getenv("EMAIL_USERNAME", "").strip()
        password = os.getenv("EMAIL_PASSWORD", "").strip()
        if not username or not password:
            raise RuntimeError("EMAIL_USERNAME and EMAIL_PASSWORD must be set in .env")

        return cls(
            email_host=os.getenv("EMAIL_HOST", "imap.gmail.com").strip(),
            email_port=int(os.getenv("EMAIL_PORT", "993")),
            email_username=username,
            email_password=password,
            email_sales_folder=os.getenv("EMAIL_SALES_FOLDER", "INBOX").strip(),
            email_poll_seconds=int(os.getenv("EMAIL_POLL_SECONDS", "300")),
            email_search_criteria=os.getenv("EMAIL_SEARCH_CRITERIA", "UNSEEN").strip(),
            email_lookback_hours=int(os.getenv("EMAIL_LOOKBACK_HOURS", "120")),
            email_mark_read=env_bool("EMAIL_MARK_READ", False),
            email_ignore_processed=env_bool("EMAIL_IGNORE_PROCESSED", False),
            email_test_mode=env_bool("EMAIL_TEST_MODE", False),
            email_from_filter=_optional("EMAIL_FROM_FILTER"),
            email_subject_filter=_optional("EMAIL_SUBJECT_FILTER"),
            buyer_email_regex=_optional("BUYER_EMAIL_REGEX"),
            product_regex=_optional("PRODUCT_REGEX"),
            order_id_regex=_optional("ORDER_ID_REGEX"),
            payment_reference_regex=_optional("PAYMENT_REFERENCE_REGEX"),
            sales_output_path=_path_from_env("SALES_OUTPUT_PATH", "data/sales.jsonl"),
            latest_sale_path=_path_from_env("LATEST_SALE_PATH", "data/latest_sale.json"),
            processed_emails_path=_path_from_env("PROCESSED_EMAILS_PATH", "data/processed_emails.json"),
            mail_reader_log_path=_path_from_env("MAIL_READER_LOG_PATH", "logs/mail_reader.log"),
        )


@dataclass(frozen=True)
class PayPalVerifyConfig:
    paypal_client_id: str
    paypal_client_secret: str
    paypal_env: str
    paypal_api_base_url: str
    payment_verify_test_mode: bool
    payment_verify_lookback_days: int
    payment_verify_window_days: int
    payment_verify_fallback_window_hours: int
    payment_verify_currency: str
    product_price_map: dict[str, str]
    sales_output_path: Path
    latest_sale_path: Path
    verified_sales_output_path: Path
    payment_review_output_path: Path
    payment_verifier_log_path: Path

    @classmethod
    def from_env(cls) -> "PayPalVerifyConfig":
        load_dotenv()

        client_id = os.getenv("PAYPAL_CLIENT_ID", "").strip()
        client_secret = os.getenv("PAYPAL_CLIENT_SECRET", "").strip()
        if not client_id or not client_secret:
            raise RuntimeError("PAYPAL_CLIENT_ID and PAYPAL_CLIENT_SECRET must be set in .env")

        paypal_env = os.getenv("PAYPAL_ENV", "sandbox").strip().lower()
        if paypal_env not in {"sandbox", "live"}:
            raise RuntimeError("PAYPAL_ENV must be either sandbox or live")

        base_url = (
            "https://api-m.paypal.com"
            if paypal_env == "live"
            else "https://api-m.sandbox.paypal.com"
        )

        return cls(
            paypal_client_id=client_id,
            paypal_client_secret=client_secret,
            paypal_env=paypal_env,
            paypal_api_base_url=os.getenv("PAYPAL_API_BASE_URL", base_url).strip(),
            payment_verify_test_mode=env_bool("PAYMENT_VERIFY_TEST_MODE", False),
            payment_verify_lookback_days=int(os.getenv("PAYMENT_VERIFY_LOOKBACK_DAYS", "5")),
            payment_verify_window_days=int(os.getenv("PAYMENT_VERIFY_WINDOW_DAYS", "5")),
            payment_verify_fallback_window_hours=int(
                os.getenv("PAYMENT_VERIFY_FALLBACK_WINDOW_HOURS", "6")
            ),
            payment_verify_currency=os.getenv("PAYMENT_VERIFY_CURRENCY", "USD").strip().upper(),
            product_price_map=_json_object_from_env("PRODUCT_PRICE_MAP"),
            sales_output_path=_path_from_env("SALES_OUTPUT_PATH", "data/sales.jsonl"),
            latest_sale_path=_path_from_env("LATEST_SALE_PATH", "data/latest_sale.json"),
            verified_sales_output_path=_path_from_env(
                "VERIFIED_SALES_OUTPUT_PATH",
                "data/verified_sales.jsonl",
            ),
            payment_review_output_path=_path_from_env(
                "PAYMENT_REVIEW_OUTPUT_PATH",
                "data/payment_review.jsonl",
            ),
            payment_verifier_log_path=_path_from_env(
                "PAYMENT_VERIFIER_LOG_PATH",
                "logs/paypal_verify.log",
            ),
        )


@dataclass(frozen=True)
class FileMailerConfig:
    smtp_host: str
    smtp_port: int
    smtp_username: str
    smtp_password: str
    smtp_from: str
    smtp_use_tls: bool
    fulfillment_test_mode: bool
    product_root_dir: Path
    product_file_map: dict[str, str]
    verified_sales_output_path: Path
    fulfilled_sales_output_path: Path
    fulfillment_review_output_path: Path
    fulfillment_log_path: Path
    file_mailer_log_path: Path

    @classmethod
    def from_env(cls) -> "FileMailerConfig":
        load_dotenv()

        fulfillment_test_mode = env_bool("FULFILLMENT_TEST_MODE", True)
        username = os.getenv("SMTP_USERNAME", "").strip()
        password = os.getenv("SMTP_PASSWORD", "").strip()
        smtp_from = os.getenv("SMTP_FROM", username).strip()
        if not fulfillment_test_mode and (not username or not password or not smtp_from):
            raise RuntimeError("SMTP_USERNAME, SMTP_PASSWORD, and SMTP_FROM must be set in .env")

        return cls(
            smtp_host=os.getenv("SMTP_HOST", "smtp.gmail.com").strip(),
            smtp_port=int(os.getenv("SMTP_PORT", "587")),
            smtp_username=username,
            smtp_password=password,
            smtp_from=smtp_from,
            smtp_use_tls=env_bool("SMTP_USE_TLS", True),
            fulfillment_test_mode=fulfillment_test_mode,
            product_root_dir=_path_from_env(
                "PRODUCT_ROOT_DIR",
                "products",
            ),
            product_file_map=_json_object_from_env("PRODUCT_FILE_MAP"),
            verified_sales_output_path=_path_from_env(
                "VERIFIED_SALES_OUTPUT_PATH",
                "data/verified_sales.jsonl",
            ),
            fulfilled_sales_output_path=_path_from_env(
                "FULFILLED_SALES_OUTPUT_PATH",
                "data/fulfilled_sales.jsonl",
            ),
            fulfillment_review_output_path=_path_from_env(
                "FULFILLMENT_REVIEW_OUTPUT_PATH",
                "data/fulfillment_review.jsonl",
            ),
            fulfillment_log_path=_path_from_env(
                "FULFILLMENT_LOG_PATH",
                "data/fulfillment_log.jsonl",
            ),
            file_mailer_log_path=_path_from_env(
                "FILE_MAILER_LOG_PATH",
                "logs/file_mailer.log",
            ),
        )


def _optional(name: str) -> str | None:
    value = os.getenv(name, "").strip()
    return value or None


def _path_from_env(name: str, default: str) -> Path:
    value = os.getenv(name, default).strip()
    path = Path(value)
    return path if path.is_absolute() else ROOT_DIR / path


def _json_object_from_env(name: str) -> dict[str, Any]:
    value = os.getenv(name, "").strip()
    if not value:
        return {}

    parsed = json.loads(value)
    if not isinstance(parsed, dict):
        raise RuntimeError(f"{name} must be a JSON object")
    return parsed
