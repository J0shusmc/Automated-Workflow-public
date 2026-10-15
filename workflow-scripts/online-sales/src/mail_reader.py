from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from email import policy
from email.header import decode_header, make_header
from email.message import EmailMessage, Message
from email.parser import BytesParser
from email.utils import parsedate_to_datetime
import imaplib
import json
import logging
from pathlib import Path
import re
import sys
import time
from typing import Iterable

from config import MailReaderConfig
from storage import append_jsonl, ensure_parent, load_processed_ids, save_processed_ids, write_json


EMAIL_PATTERN = re.compile(r"[\w.!#$%&'*+/=?^_`{|}~-]+@[\w-]+(?:\.[\w-]+)+")


@dataclass(frozen=True)
class ParsedEmail:
    uid: str
    message_id: str | None
    sender: str
    subject: str
    received_at: str
    body: str


def setup_logging(log_path: Path) -> None:
    ensure_parent(log_path)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=[
            logging.FileHandler(log_path, encoding="utf-8"),
            logging.StreamHandler(sys.stdout),
        ],
    )


def decode_mime(value: str | None) -> str:
    if not value:
        return ""
    return str(make_header(decode_header(value)))


def message_body(message: Message) -> str:
    if message.is_multipart():
        plain_parts: list[str] = []
        fallback_parts: list[str] = []

        for part in message.walk():
            content_disposition = part.get_content_disposition()
            content_type = part.get_content_type()
            if content_disposition == "attachment":
                continue

            if content_type == "text/plain":
                plain_parts.append(_part_text(part))
            elif content_type == "text/html":
                fallback_parts.append(strip_html(_part_text(part)))

        return "\n".join(part for part in plain_parts or fallback_parts if part).strip()

    return _part_text(message).strip()


def _part_text(part: Message) -> str:
    payload = part.get_payload(decode=True)
    if payload is None:
        raw = part.get_payload()
        return raw if isinstance(raw, str) else ""
    charset = part.get_content_charset() or "utf-8"
    return payload.decode(charset, errors="replace")


def strip_html(value: str) -> str:
    value = re.sub(r"(?is)<(script|style).*?>.*?</\1>", " ", value)
    value = re.sub(r"(?s)<br\s*/?>", "\n", value)
    value = re.sub(r"(?s)</p\s*>", "\n", value)
    value = re.sub(r"(?s)<.*?>", " ", value)
    return re.sub(r"[ \t]+", " ", value)


def parse_message(uid: str, raw_message: bytes) -> ParsedEmail:
    message = BytesParser(policy=policy.default).parsebytes(raw_message)
    received_at = _received_at(message)

    return ParsedEmail(
        uid=uid,
        message_id=decode_mime(message.get("Message-ID")) or None,
        sender=decode_mime(message.get("From")),
        subject=decode_mime(message.get("Subject")),
        received_at=received_at,
        body=message_body(message),
    )


def _received_at(message: EmailMessage | Message) -> str:
    raw_date = message.get("Date")
    if raw_date:
        try:
            parsed = parsedate_to_datetime(raw_date)
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return parsed.isoformat()
        except (TypeError, ValueError):
            pass
    return datetime.now(timezone.utc).isoformat()


def build_search_query(config: MailReaderConfig, now: datetime | None = None) -> str:
    parts = [config.email_search_criteria or "UNSEEN"]
    if config.email_lookback_hours > 0:
        cutoff = _lookback_cutoff(config, now)
        parts.extend(["SINCE", cutoff.astimezone().strftime("%d-%b-%Y")])
    if config.email_from_filter:
        parts.extend(["FROM", f'"{config.email_from_filter}"'])
    if config.email_subject_filter:
        parts.extend(["SUBJECT", f'"{config.email_subject_filter}"'])
    return " ".join(parts)


def _lookback_cutoff(config: MailReaderConfig, now: datetime | None = None) -> datetime:
    current_time = now or datetime.now(timezone.utc)
    if current_time.tzinfo is None:
        current_time = current_time.replace(tzinfo=timezone.utc)
    return current_time.astimezone(timezone.utc) - timedelta(hours=config.email_lookback_hours)


def is_within_lookback(
    parsed: ParsedEmail,
    config: MailReaderConfig,
    now: datetime | None = None,
) -> bool:
    if config.email_lookback_hours <= 0:
        return True

    try:
        received_at = datetime.fromisoformat(parsed.received_at)
    except ValueError:
        logging.warning(
            "Could not parse received_at for UID %s, keeping message in candidate set: %s",
            parsed.uid,
            parsed.received_at,
        )
        return True

    if received_at.tzinfo is None:
        received_at = received_at.replace(tzinfo=timezone.utc)

    return received_at.astimezone(timezone.utc) >= _lookback_cutoff(config, now)


def connect(config: MailReaderConfig) -> imaplib.IMAP4_SSL:
    mailbox = imaplib.IMAP4_SSL(config.email_host, config.email_port)
    mailbox.login(config.email_username, config.email_password)
    status, _ = mailbox.select(config.email_sales_folder)
    if status != "OK":
        raise RuntimeError(f"Unable to select mailbox folder: {config.email_sales_folder}")
    return mailbox


def fetch_uids(mailbox: imaplib.IMAP4_SSL, search_query: str) -> list[str]:
    status, data = mailbox.uid("search", None, search_query)
    if status != "OK":
        raise RuntimeError(f"IMAP search failed for query: {search_query}")
    return data[0].decode("ascii").split() if data and data[0] else []


def fetch_message(mailbox: imaplib.IMAP4_SSL, uid: str) -> bytes:
    status, data = mailbox.uid("fetch", uid, "(BODY.PEEK[])")
    if status != "OK":
        raise RuntimeError(f"Unable to fetch message UID {uid}")

    for item in data:
        if isinstance(item, tuple) and item[1]:
            return item[1]
    raise RuntimeError(f"No message payload returned for UID {uid}")


def mark_read(mailbox: imaplib.IMAP4_SSL, uid: str) -> None:
    mailbox.uid("store", uid, "+FLAGS", "(\\Seen)")


def extract_sale(parsed: ParsedEmail, config: MailReaderConfig) -> dict[str, str] | None:
    searchable = "\n".join([parsed.subject, parsed.sender, parsed.body])
    fields = _parse_labeled_fields(searchable)
    buyer_email = _extract_with_regex(config.buyer_email_regex, searchable, "buyer_email")
    product = _extract_with_regex(config.product_regex, searchable, "product")
    order_id = _extract_with_regex(config.order_id_regex, searchable, "order_id")
    payment_reference = _extract_with_regex(
        config.payment_reference_regex,
        searchable,
        "payment_reference",
    )

    if not buyer_email:
        buyer_email = fields.get("customer email") or fields.get("buyer email")
    if not buyer_email:
        buyer_email = _first_buyer_email(searchable, parsed.sender)
    if not product:
        product = fields.get("product") or _guess_product(searchable)
    if not order_id:
        order_id = fields.get("order id") or _guess_labeled_value(
            searchable,
            ["order id", "order", "receipt", "invoice"],
        )
    if not payment_reference:
        payment_reference = fields.get("payment reference")
    if not payment_reference:
        payment_reference = _guess_labeled_value(
            searchable,
            ["transaction id", "transaction", "payment id", "payment reference"],
        )
    if not payment_reference:
        payment_reference = order_id

    amount = fields.get("amount", "")
    payment_method = fields.get("payment method", "")
    payment_provider = fields.get("payment provider", "")
    customer_name = fields.get("customer", "")

    if not buyer_email or not product:
        return None

    return {
        "source_email_id": parsed.uid,
        "message_id": parsed.message_id or "",
        "buyer_email": buyer_email,
        "buyer_name": customer_name,
        "product": product,
        "amount": amount,
        "payment_method": payment_method,
        "payment_provider": payment_provider,
        "order_id": order_id or "",
        "payment_reference": payment_reference or "",
        "received_at": parsed.received_at,
        "status": "pending_payment_verification",
    }


def _parse_labeled_fields(text: str) -> dict[str, str]:
    fields: dict[str, str] = {}
    for line in text.splitlines():
        match = re.match(r"^\s*([A-Za-z][A-Za-z ]{1,40})\s*:\s*(.+?)\s*$", line)
        if not match:
            continue

        key = re.sub(r"\s+", " ", match.group(1).strip().lower())
        fields[key] = _clean_value(match.group(2))
    return fields


def _extract_with_regex(pattern: str | None, text: str, group_name: str) -> str | None:
    if not pattern:
        return None

    match = re.search(pattern, text, flags=re.IGNORECASE | re.MULTILINE)
    if not match:
        return None
    if group_name in match.groupdict():
        return match.group(group_name).strip()
    if match.groups():
        return match.group(1).strip()
    return match.group(0).strip()


def _first_buyer_email(text: str, sender: str) -> str | None:
    candidates = EMAIL_PATTERN.findall(text)
    sender_emails = set(EMAIL_PATTERN.findall(sender))
    for candidate in candidates:
        if candidate not in sender_emails:
            return candidate
    return candidates[0] if candidates else None


def _guess_product(text: str) -> str | None:
    labels = ["product", "item", "purchased", "purchase", "title"]
    return _guess_labeled_value(text, labels)


def _guess_labeled_value(text: str, labels: Iterable[str]) -> str | None:
    for label in labels:
        pattern = rf"(?im)^\s*{re.escape(label)}\s*[:#-]\s*(.+?)\s*$"
        match = re.search(pattern, text)
        if match:
            return _clean_value(match.group(1))
    return None


def _clean_value(value: str) -> str:
    value = re.sub(r"\s+", " ", value).strip()
    return value[:200]


def workflow_sale_payload(sale: dict[str, str]) -> dict[str, str]:
    return {
        "source_email_id": sale.get("source_email_id", ""),
        "message_id": sale.get("message_id", ""),
        "order_id": sale.get("order_id", ""),
        "buyer_email": sale.get("buyer_email", ""),
        "buyer_name": sale.get("buyer_name", ""),
        "product": sale.get("product", ""),
        "amount": sale.get("amount", ""),
        "payment_method": sale.get("payment_method", ""),
        "payment_provider": sale.get("payment_provider", ""),
        "payment_reference": sale.get("payment_reference", ""),
        "received_at": sale.get("received_at", ""),
        "status": sale.get("status", ""),
    }


def print_order_summary(sale: dict[str, str]) -> None:
    print("ORDER " + json.dumps(workflow_sale_payload(sale), sort_keys=True), flush=True)


def print_workflow_result(payload: object) -> None:
    print("@@WORKFLOW_RESULT@@" + json.dumps(payload, sort_keys=True), flush=True)


def no_new_sales_result() -> dict[str, str]:
    return {"status": "no_new_sales", "message": "No New Sales"}


def run_once(config: MailReaderConfig, dry_run: bool, ignore_processed: bool = False) -> list[dict[str, str]]:
    processed_ids = load_processed_ids(config.processed_emails_path)
    new_sales: list[dict[str, str]] = []
    now = datetime.now(timezone.utc)
    search_query = build_search_query(config, now)

    logging.info("Connecting to %s folder %s", config.email_host, config.email_sales_folder)
    mailbox = connect(config)
    try:
        uids = fetch_uids(mailbox, search_query)
        logging.info("Found %s candidate messages for query: %s", len(uids), search_query)

        for uid in uids:
            if uid in processed_ids and not ignore_processed:
                logging.info("Skipped UID %s because it is already in %s.", uid, config.processed_emails_path)
                if config.email_mark_read:
                    mark_read(mailbox, uid)
                continue

            parsed = parse_message(uid, fetch_message(mailbox, uid))
            if not is_within_lookback(parsed, config, now):
                logging.info(
                    "Skipped UID %s because it is outside the %s-hour lookback.",
                    uid,
                    config.email_lookback_hours,
                )
                continue

            sale = extract_sale(parsed, config)
            if not sale:
                logging.warning(
                    "Skipped UID %s because buyer email or product could not be parsed. Subject=%r",
                    uid,
                    parsed.subject,
                )
                processed_ids.add(uid)
                continue

            if dry_run:
                logging.info("DRY RUN parsed sale: %s", sale)
            else:
                append_jsonl(config.sales_output_path, sale)

            if config.email_mark_read:
                mark_read(mailbox, uid)
                logging.info("Marked UID %s as read.", uid)

            print_order_summary(sale)

            if not ignore_processed:
                processed_ids.add(uid)
            new_sales.append(workflow_sale_payload(sale))

        if not dry_run and not ignore_processed:
            save_processed_ids(config.processed_emails_path, processed_ids)

        return new_sales
    finally:
        mailbox.logout()


def run_loop(config: MailReaderConfig, dry_run: bool, ignore_processed: bool = False) -> None:
    while True:
        try:
            sales = run_once(config, dry_run, ignore_processed)
            logging.info("Mail reader cycle complete. Parsed %s new sale records.", len(sales))
        except Exception:
            logging.exception("Mail reader cycle failed")
        time.sleep(config.email_poll_seconds)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Read sale notification emails and write pending sale records.")
    parser.add_argument("--once", action="store_true", help="Run one mailbox scan and exit. This is the default.")
    parser.add_argument("--loop", action="store_true", help="Keep scanning every EMAIL_POLL_SECONDS.")
    parser.add_argument("--dry-run", action="store_true", help="Parse and log matches without writing output files.")
    parser.add_argument(
        "--ignore-processed",
        action="store_true",
        help="Parse matching emails even if their UID is already in processed_emails.json.",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    config = MailReaderConfig.from_env()
    setup_logging(config.mail_reader_log_path)
    dry_run = args.dry_run or config.email_test_mode
    ignore_processed = args.ignore_processed or config.email_ignore_processed or config.email_test_mode

    if config.email_test_mode:
        logging.info("EMAIL_TEST_MODE is enabled: reprocessing matching emails without writing sale records.")

    if args.loop:
        run_loop(config, dry_run, ignore_processed)
        return

    sales = run_once(config, dry_run, ignore_processed)
    latest_sale = sales[0] if sales else no_new_sales_result()
    write_json(config.latest_sale_path, latest_sale)
    print_workflow_result(latest_sale)
    logging.info("Parsed %s new sale records.", len(sales))


if __name__ == "__main__":
    main()
