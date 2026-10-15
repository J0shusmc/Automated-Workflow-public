from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import datetime, timezone
from email.message import EmailMessage
import json
import os
import logging
from pathlib import Path
import re
import smtplib
import sys
from typing import Any


def _load_env_file(path: Path = Path(".env")) -> None:
    if not path.exists():
        return

    import os

    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue

        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


    unfilled = sorted(k for k, v in os.environ.items() if v.startswith("ENTER_") and v.endswith("_HERE"))
    if unfilled:
        raise SystemExit(f"Fill in {', '.join(unfilled)} in {path} before running this workflow.")


def _int_env(name: str, default: int) -> int:
    import os

    value = os.getenv(name)
    if value is None or not value.strip():
        return default
    return int(value)


def _bool_env(name: str, default: bool = False) -> bool:
    import os

    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "y", "on"}


@dataclass(frozen=True)
class ContactResponderConfig:
    smtp_host: str
    smtp_port: int
    smtp_username: str
    smtp_password: str
    from_email: str
    from_name: str
    latest_contact_path: Path
    sent_contacts_path: Path
    blocked_contacts_path: Path
    blocked_contact_names: frozenset[str]
    require_screening: bool
    log_path: Path

    @classmethod
    def from_env(cls) -> "ContactResponderConfig":
        import os

        _load_env_file()
        username = os.getenv("SMTP_USERNAME") or os.getenv("EMAIL_USERNAME", "")
        password = os.getenv("SMTP_PASSWORD") or "".join(os.getenv("EMAIL_PASSWORD", "").split())

        return cls(
            smtp_host=os.getenv("SMTP_HOST", "smtp.gmail.com"),
            smtp_port=_int_env("SMTP_PORT", 587),
            smtp_username=username,
            smtp_password=password,
            from_email=os.getenv("CONTACT_RESPONSE_FROM_EMAIL", username),
            from_name=os.getenv("CONTACT_RESPONSE_FROM_NAME", ""),
            latest_contact_path=Path(os.getenv("LATEST_CONTACT_PATH", "data/latest_contact.json")),
            sent_contacts_path=Path(os.getenv("SENT_CONTACTS_PATH", "data/sent_contact_responses.jsonl")),
            blocked_contacts_path=Path(
                os.getenv("BLOCKED_CONTACTS_PATH", "data/blocked_contacts.jsonl")
            ),
            blocked_contact_names=frozenset(
                item.strip()
                for item in os.getenv(
                    "BLOCKED_CONTACT_NAMES", "RobertWew,HarryPhefs"
                ).split(",")
                if item.strip()
            ),
            require_screening=_bool_env("CONTACT_REQUIRE_SCREENING", True),
            log_path=Path(os.getenv("CONTACT_RESPONDER_LOG_PATH", "logs/contact_responder.log")),
        )


def setup_logging(log_path: Path) -> None:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=[
            logging.FileHandler(log_path, encoding="utf-8"),
        ],
    )


def parse_contact_text(value: str) -> dict[str, str]:
    contact: dict[str, str] = {}
    key_map = {
        "name": "Name",
        "email": "Email",
        "company": "Company",
        "interested in": "Interested In",
        "message": "Message",
    }

    for line in value.splitlines():
        match = re.match(r"^\s*-?\s*([A-Za-z][A-Za-z ]{1,40})\s*:\s*(.*?)\s*$", line)
        if not match:
            continue

        normalized_key = re.sub(r"\s+", " ", match.group(1).strip().lower())
        if normalized_key in key_map:
            contact[key_map[normalized_key]] = match.group(2).strip()

    return contact


def is_no_new_contact_payload(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, str):
        normalized = re.sub(r"[^a-z0-9]+", "_", value.strip().lower()).strip("_")
        return normalized in {"no_new_contact", "no_new_contacts"}
    if isinstance(value, dict):
        status = value.get("status") or value.get("Status")
        message = value.get("message") or value.get("result")
        return is_no_new_contact_payload(status) or is_no_new_contact_payload(message)
    return False


def parse_contact_input(value: str) -> dict[str, str]:
    text = value.strip()
    if not text:
        return {}
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        if is_no_new_contact_payload(text):
            return {"Status": "no_new_contacts"}
        return parse_contact_text(text)
    if is_no_new_contact_payload(payload):
        return {"Status": "no_new_contacts"}
    if not isinstance(payload, dict):
        return {}
    return normalize_contact(payload)


def load_contact(args: argparse.Namespace, config: ContactResponderConfig) -> dict[str, str]:
    if args.contact_json:
        payload = json.loads(Path(args.contact_json).read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise ValueError(f"Contact JSON must contain an object: {args.contact_json}")
        return normalize_contact(payload)

    text = args.input or ""
    if not text and not sys.stdin.isatty():
        text = sys.stdin.read()
    if text.strip():
        return parse_contact_input(text)

    if config.latest_contact_path.exists():
        payload = json.loads(config.latest_contact_path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise ValueError(f"Latest contact JSON must contain an object: {config.latest_contact_path}")
        return normalize_contact(payload)

    return {"Status": "no_new_contacts"}


def normalize_contact(payload: dict[str, Any]) -> dict[str, str]:
    return {
        "Name": str(payload.get("Name") or payload.get("contact_name") or "").strip(),
        "Email": str(payload.get("Email") or payload.get("contact_email") or "").strip(),
        "Company": str(payload.get("Company") or payload.get("company") or "").strip(),
        "Interested In": str(payload.get("Interested In") or payload.get("interested_in") or "").strip(),
        "Message": str(payload.get("Message") or payload.get("message") or "").strip(),
        "IP": str(payload.get("IP") or payload.get("ip") or "").strip(),
        "Country": str(payload.get("Country") or payload.get("country") or "").strip().upper(),
        "Screening Status": str(
            payload.get("Screening Status") or payload.get("screening_status") or ""
        ).strip().lower(),
    }


def validate_contact(contact: dict[str, str]) -> None:
    if is_no_new_contact_payload(contact):
        return
    if not contact.get("Email"):
        raise ValueError("Contact is missing Email.")
    if "@" not in contact["Email"]:
        raise ValueError(f"Contact Email does not look valid: {contact['Email']!r}")


def normalize_contact_name(value: object) -> str:
    return "".join(character for character in str(value or "").casefold() if character.isalnum())


def is_blocked_contact(contact: dict[str, str], config: ContactResponderConfig) -> bool:
    blocked_names = {
        normalize_contact_name(name)
        for name in config.blocked_contact_names
        if normalize_contact_name(name)
    }
    return normalize_contact_name(contact.get("Name")) in blocked_names


def append_blocked_record(
    path: Path,
    contact: dict[str, str],
    reasons: list[str] | None = None,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    record = {
        "contact_email": contact.get("Email", ""),
        "contact_name": contact.get("Name", ""),
        "blocked_at": datetime.now(timezone.utc).isoformat(),
        "spam_reasons": reasons or ["blocked_name"],
        "source": "contact_responder",
        "status": "blocked_spam",
    }
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, sort_keys=True) + "\n")


def build_response(contact: dict[str, str], config: ContactResponderConfig) -> EmailMessage:
    name = contact.get("Name") or "there"
    message = EmailMessage()
    message["To"] = contact["Email"]
    message["From"] = f"{config.from_name} <{config.from_email}>" if config.from_name else config.from_email
    business = os.getenv("CONTACT_BUSINESS_NAME", "our team")
    signature = os.getenv("CONTACT_SIGNATURE", config.from_name or business)
    message["Subject"] = f"Thanks for reaching out to {business}"
    message.set_content(
        "\n".join(
            [
                f"Hello {name},",
                "",
                f"Thank you for reaching out to {business}. I received your message and will get back to you at my earliest convenience.",
                "",
                "Best regards,",
                *signature.split("|"),
            ]
        )
    )
    return message


def send_message(message: EmailMessage, config: ContactResponderConfig) -> None:
    if not config.smtp_username or not config.smtp_password:
        raise ValueError("SMTP credentials are missing. Set SMTP_USERNAME/SMTP_PASSWORD or EMAIL_USERNAME/EMAIL_PASSWORD.")

    with smtplib.SMTP(config.smtp_host, config.smtp_port) as smtp:
        smtp.starttls()
        smtp.login(config.smtp_username, config.smtp_password)
        smtp.send_message(message)


def sent_contact_key(contact: dict[str, str]) -> str:
    return str(contact.get("Email") or "").strip().lower()


def already_sent_response(path: Path, contact: dict[str, str]) -> bool:
    key = sent_contact_key(contact)
    if not key or not path.exists():
        return False
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            if str(record.get("contact_email") or "").strip().lower() == key and record.get("status") == "sent":
                return True
    return False


def append_sent_record(path: Path, contact: dict[str, str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    record = {
        "contact_email": contact["Email"],
        "contact_name": contact.get("Name", ""),
        "sent_at": datetime.now(timezone.utc).isoformat(),
        "status": "sent",
    }
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, sort_keys=True) + "\n")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Send an initial acknowledgment email to a new contact.")
    parser.add_argument("--input", help="Contact text from the mail reader output.")
    parser.add_argument("--contact-json", help="Path to a contact JSON file. Defaults to LATEST_CONTACT_PATH.")
    return parser.parse_args()


def main() -> None:
    try:
        args = parse_args()
        config = ContactResponderConfig.from_env()
        setup_logging(config.log_path)

        contact = load_contact(args, config)
        if is_no_new_contact_payload(contact):
            logging.info("No new contacts from mail reader; skipping contact response.")
            print("No New Contacts", flush=True)
            return
        validate_contact(contact)
        if is_blocked_contact(contact, config):
            append_blocked_record(config.blocked_contacts_path, contact)
            logging.warning(
                "Blocked contact response before SMTP. Name=%r Email=%r",
                contact.get("Name", ""),
                contact.get("Email", ""),
            )
            print("Blocked Spam", flush=True)
            return
        if config.require_screening and contact.get("Screening Status") != "allowed":
            append_blocked_record(
                config.blocked_contacts_path,
                contact,
                ["screening_required"],
            )
            logging.warning(
                "Refused unscreened contact before SMTP. Name=%r Email=%r",
                contact.get("Name", ""),
                contact.get("Email", ""),
            )
            print("Screening Required", flush=True)
            return
        if already_sent_response(config.sent_contacts_path, contact):
            logging.info("Contact response already sent to %s; skipping duplicate.", contact["Email"])
            print("Already Sent", flush=True)
            return
        message = build_response(contact, config)

        logging.info("Prepared contact response for %s", contact["Email"])
        send_message(message, config)
        logging.info("Sent contact response to %s", contact["Email"])

        append_sent_record(config.sent_contacts_path, contact)
        print("message sent", flush=True)
    except Exception:
        logging.exception("Contact response failed")
        print("message failed", flush=True)
        raise SystemExit(1)


if __name__ == "__main__":
    main()
