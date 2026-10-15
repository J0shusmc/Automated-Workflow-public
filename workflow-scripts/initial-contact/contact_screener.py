from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import datetime, timezone
import fcntl
import json
import os
from pathlib import Path
import re
import sys
from urllib.parse import urlparse


URL_PATTERN = re.compile(r"(?i)\b(?:https?://|www\.)[^\s<>]+")
PROMOTIONAL_PATTERNS = (
    re.compile(r"(?i)\bpromo(?:tional)?\s+code\b"),
    re.compile(r"(?i)\b(?:coupon|discount)\s+code\b"),
    re.compile(r"(?i)\b(?:prize|lottery|sweepstakes|winner|giveaway)\b"),
    re.compile(r"(?i)\b(?:bitcoin|crypto(?:currency)?|casino|gambling)\b"),
    re.compile(r"(?i)\b(?:backlinks?|guest\s+posts?|search\s+engine\s+optimization|SEO)\b"),
    re.compile(r"(?i)\b(?:guaranteed\s+traffic|click\s+here|limited\s+time)\b"),
)
UNKNOWN_COUNTRIES = {"", "XX"}
result: object = None


def _load_env_file(path: Path = Path(".env")) -> None:
    if not path.exists():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


    unfilled = sorted(k for k, v in os.environ.items() if v.startswith("ENTER_") and v.endswith("_HERE"))
    if unfilled:
        raise SystemExit(f"Fill in {', '.join(unfilled)} in {path} before running this workflow.")


def csv_values(value: str) -> frozenset[str]:
    return frozenset(item.strip() for item in str(value or "").split(",") if item.strip())


def normalize_name(value: object) -> str:
    return "".join(character for character in str(value or "").casefold() if character.isalnum())


@dataclass(frozen=True)
class ScreeningConfig:
    allowed_countries: frozenset[str]
    blocked_names: frozenset[str]
    blocked_ips: frozenset[str]
    blocked_domains: frozenset[str]
    screened_path: Path
    blocked_path: Path
    review_path: Path
    minimum_message_length: int = 20

    @classmethod
    def from_env(cls) -> "ScreeningConfig":
        _load_env_file()
        return cls(
            allowed_countries=frozenset(
                country.upper()
                for country in csv_values(
                    os.getenv("CONTACT_ALLOWED_COUNTRIES", "US,PR,VI,GU,AS,MP")
                )
            ),
            blocked_names=frozenset(
                normalize_name(name)
                for name in csv_values(
                    os.getenv("BLOCKED_CONTACT_NAMES", "RobertWew,HarryPhefs")
                )
            ),
            blocked_ips=csv_values(
                os.getenv(
                    "BLOCKED_CONTACT_IPS",
                    "77.90.185.5,141.98.11.224,80.94.95.173,158.173.20.36",
                )
            ),
            blocked_domains=frozenset(
                domain.casefold()
                for domain in csv_values(os.getenv("BLOCKED_CONTACT_DOMAINS", "jbiv.com"))
            ),
            screened_path=Path(
                os.getenv("SCREENED_CONTACTS_PATH", "data/screened_contacts.jsonl")
            ),
            blocked_path=Path(
                os.getenv("BLOCKED_CONTACTS_PATH", "data/blocked_contacts.jsonl")
            ),
            review_path=Path(
                os.getenv("REVIEW_CONTACTS_PATH", "data/review_contacts.jsonl")
            ),
            minimum_message_length=max(
                1, int(os.getenv("CONTACT_MINIMUM_MESSAGE_LENGTH", "20"))
            ),
        )


def parse_contact(value: object) -> dict[str, str]:
    if isinstance(value, dict):
        if is_no_new_contact(value):
            return {"status": "no_new_contacts"}
        payload = value
    else:
        text = str(value or "").strip()
        if is_no_new_contact(text):
            return {"status": "no_new_contacts"}
        try:
            decoded = json.loads(text)
        except json.JSONDecodeError:
            decoded = _parse_labeled_text(text)
        payload = decoded if isinstance(decoded, dict) else {}
    return {
        "Name": str(payload.get("Name") or payload.get("contact_name") or "").strip(),
        "Email": str(payload.get("Email") or payload.get("contact_email") or "").strip(),
        "Company": str(payload.get("Company") or payload.get("company") or "").strip(),
        "Interested In": str(
            payload.get("Interested In") or payload.get("interested_in") or ""
        ).strip(),
        "Message": str(payload.get("Message") or payload.get("message") or "").strip(),
        "IP": str(payload.get("IP") or payload.get("ip") or "").strip(),
        "Country": str(payload.get("Country") or payload.get("country") or "")
        .strip()
        .upper(),
    }


def _parse_labeled_text(text: str) -> dict[str, str]:
    fields: dict[str, str] = {}
    for line in text.splitlines():
        match = re.match(r"^\s*-?\s*([A-Za-z][A-Za-z ]{1,40})\s*:\s*(.*?)\s*$", line)
        if match:
            fields[match.group(1).strip()] = match.group(2).strip()
    return fields


def is_no_new_contact(value: object) -> bool:
    if isinstance(value, dict):
        status = value.get("status") or value.get("Status")
        return is_no_new_contact(status)
    normalized = re.sub(r"[^a-z0-9]+", "_", str(value or "").casefold()).strip("_")
    return normalized in {
        "no_new_contact",
        "no_new_contacts",
        "status_no_new_contact",
        "status_no_new_contacts",
    }


def message_domains(message: str) -> set[str]:
    domains: set[str] = set()
    for raw_url in URL_PATTERN.findall(message or ""):
        url = raw_url if "://" in raw_url else f"https://{raw_url}"
        hostname = (urlparse(url).hostname or "").casefold().rstrip(".")
        if hostname.startswith("www."):
            hostname = hostname[4:]
        if hostname:
            domains.add(hostname)
    return domains


def screening_decision(
    contact: dict[str, str], config: ScreeningConfig
) -> tuple[str, list[str]]:
    if is_no_new_contact(contact):
        return "empty", []

    name = normalize_name(contact.get("Name"))
    ip = str(contact.get("IP") or "").strip()
    country = str(contact.get("Country") or "").strip().upper()
    message = str(contact.get("Message") or "").strip()
    domains = message_domains(message)
    urls_present = bool(domains)
    promo_matches = [
        pattern.pattern for pattern in PROMOTIONAL_PATTERNS if pattern.search(message)
    ]

    block_reasons: list[str] = []
    if name and name in config.blocked_names:
        block_reasons.append("blocked_name")
    if ip and ip in config.blocked_ips:
        block_reasons.append("blocked_ip")
    if any(
        domain == blocked or domain.endswith(f".{blocked}")
        for domain in domains
        for blocked in config.blocked_domains
    ):
        block_reasons.append("blocked_link_domain")
    if urls_present and promo_matches:
        block_reasons.append("promotional_link")
    elif promo_matches and any("promo" in match.casefold() or "prize" in match.casefold() for match in promo_matches):
        block_reasons.append("promotional_spam")
    if country == "T1":
        block_reasons.append("tor_exit_country")
    elif country not in UNKNOWN_COUNTRIES and country not in config.allowed_countries:
        block_reasons.append("non_us_country")
    if block_reasons:
        return "blocked", sorted(set(block_reasons))

    review_reasons: list[str] = []
    if not contact.get("Name") or not contact.get("Email") or not message:
        review_reasons.append("missing_required_field")
    if country in UNKNOWN_COUNTRIES:
        review_reasons.append("unknown_country")
    if urls_present:
        review_reasons.append("unverified_link")
    if message and len(message) < config.minimum_message_length:
        review_reasons.append("message_too_short")
    if review_reasons:
        return "review", sorted(set(review_reasons))
    return "allowed", []


def append_audit(path: Path, contact: dict[str, str], decision: str, reasons: list[str]) -> None:
    record = {
        **contact,
        "screened_at": datetime.now(timezone.utc).isoformat(),
        "screening_status": decision,
        "screening_reasons": reasons,
        "source": "contact_screener",
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        handle.write(json.dumps(record, sort_keys=True, ensure_ascii=False) + "\n")
        handle.flush()
        os.fsync(handle.fileno())
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def screen_contact(value: object, config: ScreeningConfig) -> dict[str, object]:
    contact = parse_contact(value)
    decision, reasons = screening_decision(contact, config)
    if decision == "empty":
        return {"status": "no_new_contacts", "screening_status": "empty"}

    append_audit(config.screened_path, contact, decision, reasons)
    if decision == "blocked":
        append_audit(config.blocked_path, contact, decision, reasons)
    elif decision == "review":
        append_audit(config.review_path, contact, decision, reasons)

    if decision != "allowed":
        return {
            "status": "no_new_contacts",
            "screening_status": decision,
            "screening_reasons": reasons,
        }
    return {
        **contact,
        "screening_status": "allowed",
        "screening_reasons": [],
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Screen a contact before automatic response.")
    parser.add_argument("--input", help="Contact JSON or labeled text from the mail reader.")
    return parser.parse_args()


def main() -> dict[str, object]:
    args = parse_args()
    value = args.input
    if value is None and not sys.stdin.isatty():
        value = sys.stdin.read()
    outcome = screen_contact(value or "No New Contacts", ScreeningConfig.from_env())
    status = outcome.get("screening_status", "unknown")
    reasons = outcome.get("screening_reasons") or []
    reason_text = f" ({', '.join(str(reason) for reason in reasons)})" if reasons else ""
    print(f"Contact screening: {status}{reason_text}", flush=True)
    return outcome


if __name__ == "__main__":
    result = main()
