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

from config import MailReaderConfig
from storage import append_jsonl, ensure_parent, load_processed_ids, save_processed_ids, write_json


EMAIL_PATTERN = re.compile(r"[\w.!#$%&'*+/=?^_`{|}~-]+@[\w-]+(?:\.[\w-]+)+")
REPLY_SUBJECT_PATTERN = re.compile(r"^\s*(?:re|fwd?|fw)\s*:", re.IGNORECASE)
result: object = None


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
    status, _ = mailbox.select(config.email_folder)
    if status != "OK":
        raise RuntimeError(f"Unable to select mailbox folder: {config.email_folder}")
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


def extract_contact(parsed: ParsedEmail, config: MailReaderConfig) -> dict[str, str] | None:
    if is_reply_or_forward(parsed.subject):
        logging.info(
            "Skipped UID %s because it appears to be a reply/forward. Subject=%r",
            parsed.uid,
            parsed.subject,
        )
        return None

    searchable = "\n".join([parsed.subject, parsed.sender, parsed.body])
    fields = _parse_labeled_fields(searchable)

    message = fields.get("message") or _extract_message(parsed.body)

    if not has_required_contact_form_fields(fields, message):
        logging.info(
            "Skipped UID %s because it does not contain the required contact form fields. Subject=%r",
            parsed.uid,
            parsed.subject,
        )
        return None

    contact_email = fields.get("email") or _first_contact_email(searchable, parsed.sender)
    contact_name = fields.get("name", "")
    interested_in = fields.get("interested in", "")

    if not contact_email:
        return None

    return {
        "source_email_id": parsed.uid,
        "message_id": parsed.message_id or "",
        "contact_name": contact_name,
        "contact_email": contact_email,
        "company": fields.get("company", ""),
        "interested_in": interested_in,
        "form_timestamp": fields.get("timestamp", ""),
        "ip": fields.get("ip", ""),
        "country": fields.get("country", "").upper(),
        "message": message,
        "subject": parsed.subject,
        "received_at": parsed.received_at,
        "status": "new_contact",
    }


def normalize_contact_name(value: object) -> str:
    return "".join(character for character in str(value or "").casefold() if character.isalnum())


def contact_spam_reasons(contact: dict[str, str], config: MailReaderConfig) -> list[str]:
    reasons: list[str] = []
    contact_ip = str(contact.get("ip") or "").strip()
    blocked_names = {
        normalize_contact_name(name)
        for name in config.blocked_contact_names
        if normalize_contact_name(name)
    }
    if contact_ip and contact_ip in config.blocked_contact_ips:
        reasons.append("blocked_ip")
    if normalize_contact_name(contact.get("contact_name")) in blocked_names:
        reasons.append("blocked_name")
    return reasons


def quarantine_contact(
    contact: dict[str, str],
    reasons: list[str],
    config: MailReaderConfig,
) -> None:
    quarantined = dict(contact)
    quarantined.update(
        {
            "status": "blocked_spam",
            "spam_reasons": sorted(set(reasons)),
            "blocked_at": datetime.now(timezone.utc).isoformat(),
        }
    )
    append_jsonl(config.blocked_contacts_path, quarantined)


def is_reply_or_forward(subject: str) -> bool:
    return bool(REPLY_SUBJECT_PATTERN.match(subject or ""))


def has_required_contact_form_fields(fields: dict[str, str], message: str) -> bool:
    return bool(fields.get("name") and fields.get("email") and message)


def _parse_labeled_fields(text: str) -> dict[str, str]:
    fields: dict[str, str] = {}
    lines = text.splitlines()
    for index, line in enumerate(lines):
        match = re.match(r"^\s*([A-Za-z][A-Za-z ]{1,40})\s*:\s*(.*?)\s*$", line)
        if not match:
            continue

        key = re.sub(r"\s+", " ", match.group(1).strip().lower())
        value = match.group(2)
        if not value and key == "message":
            value = _value_after_label(lines, index + 1)
        if value:
            fields[key] = _clean_value(value)
    return fields


def _value_after_label(lines: list[str], start_index: int) -> str:
    values: list[str] = []
    for line in lines[start_index:]:
        if re.match(r"^\s*[A-Za-z][A-Za-z ]{1,40}\s*:\s*", line):
            break
        values.append(line)
    return "\n".join(values).strip()


def _first_contact_email(text: str, sender: str) -> str | None:
    candidates = EMAIL_PATTERN.findall(text)
    sender_emails = set(EMAIL_PATTERN.findall(sender))
    for candidate in candidates:
        if candidate not in sender_emails:
            return candidate
    return candidates[0] if candidates else None


def _extract_message(text: str) -> str:
    match = re.search(r"(?ims)^\s*Message\s*:\s*(.*?)\s*$", text)
    if not match:
        return ""
    return match.group(1).strip()[:5000]


def _clean_value(value: str) -> str:
    value = re.sub(r"\s+", " ", value).strip()
    return value[:200]


def workflow_contact_payload(contact: dict[str, str]) -> dict[str, str]:
    return {
        "Name": contact.get("contact_name", contact.get("Name", "")),
        "Email": contact.get("contact_email", contact.get("Email", "")),
        "Company": contact.get("company", contact.get("Company", "")),
        "Interested In": contact.get("interested_in", contact.get("Interested In", "")),
        "Message": contact.get("message", contact.get("Message", "")),
        "IP": contact.get("ip", contact.get("IP", "")),
        "Country": contact.get("country", contact.get("Country", "")),
    }


def format_contact_for_workflow(contact: dict[str, str]) -> str:
    payload = workflow_contact_payload(contact)
    return "\n".join(
        [
            f"- Name: {payload['Name']}",
            f"- Email: {payload['Email']}",
            f"- Company: {payload['Company']}",
            f"- Interested In: {payload['Interested In']}",
            f"- Message: {payload['Message']}",
            f"- IP: {payload['IP']}",
            f"- Country: {payload['Country']}",
        ]
    )


def print_contact_summary(contact: dict[str, str]) -> None:
    print(format_contact_for_workflow(contact), flush=True)


def print_workflow_result(payload: object) -> None:
    if isinstance(payload, dict) and payload.get("status") == "no_new_contacts":
        print("No New Contacts", flush=True)
        return
    if isinstance(payload, dict):
        print(format_contact_for_workflow(payload), flush=True)


def no_new_contacts_result() -> dict[str, str]:
    return {"status": "no_new_contacts"}


def run_once(config: MailReaderConfig) -> list[dict[str, str]]:
    processed_ids = load_processed_ids(config.processed_emails_path)
    new_contacts: list[dict[str, str]] = []
    now = datetime.now(timezone.utc)
    search_query = build_search_query(config, now)

    logging.info("Connecting to %s folder %s", config.email_host, config.email_folder)
    mailbox = connect(config)
    try:
        uids = fetch_uids(mailbox, search_query)
        logging.info("Found %s candidate messages for query: %s", len(uids), search_query)

        for uid in uids:
            if uid in processed_ids:
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

            contact = extract_contact(parsed, config)
            if not contact:
                logging.warning(
                    "Skipped UID %s because contact email could not be parsed. Subject=%r",
                    uid,
                    parsed.subject,
                )
                continue

            spam_reasons = contact_spam_reasons(contact, config)
            if spam_reasons:
                quarantine_contact(contact, spam_reasons, config)
                if config.email_mark_read:
                    mark_read(mailbox, uid)
                    logging.info("Marked quarantined UID %s as read.", uid)
                processed_ids.add(uid)
                logging.warning(
                    "Quarantined spam contact UID %s. Reasons=%s Name=%r IP=%r",
                    uid,
                    ",".join(spam_reasons),
                    contact.get("contact_name", ""),
                    contact.get("ip", ""),
                )
                continue

            append_jsonl(config.contact_output_path, contact)

            if config.email_mark_read:
                mark_read(mailbox, uid)
                logging.info("Marked UID %s as read.", uid)

            processed_ids.add(uid)
            new_contacts.append(workflow_contact_payload(contact))

        save_processed_ids(config.processed_emails_path, processed_ids)

        return new_contacts
    finally:
        mailbox.logout()


def run_loop(config: MailReaderConfig) -> None:
    while True:
        try:
            contacts = run_once(config)
            logging.info("Mail reader cycle complete. Parsed %s new contact records.", len(contacts))
        except Exception:
            logging.exception("Mail reader cycle failed")
        time.sleep(config.email_poll_seconds)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Read website contact emails and write contact records.")
    parser.add_argument("--once", action="store_true", help="Run one mailbox scan and exit. This is the default.")
    parser.add_argument("--loop", action="store_true", help="Keep scanning every EMAIL_POLL_SECONDS.")
    return parser.parse_args()


def main() -> None:
    global result
    args = parse_args()
    config = MailReaderConfig.from_env()
    setup_logging(config.mail_reader_log_path)

    if args.loop:
        run_loop(config)
        return

    contacts = run_once(config)
    if contacts:
        latest_contact = contacts[0]
        write_json(config.latest_contact_path, latest_contact)
        print_workflow_result(latest_contact)
        result = latest_contact
    else:
        result = no_new_contacts_result()
        print_workflow_result(result)
    logging.info("Parsed %s new contact records.", len(contacts))


if __name__ == "__main__":
    main()
