"""Kalshi BTC request intake and local drafts; delivery is intentionally held."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from email import policy
from email.parser import BytesParser
import hashlib
import imaplib
import os
from pathlib import Path
import re
import sqlite3

SUBJECT = "Kalshi BTC Bot Request"
ROOT = Path(__file__).resolve().parent.parent
DATABASE = ROOT / "data" / "kalshi-btc" / "requests.sqlite3"
HOLD_REASON = "Delivery held: access policy, verification, and bot download are not configured."


def load_mail_settings():
    """Read existing mail credentials without importing/mutating the contact scanner."""
    settings = {}
    path = Path(os.environ.get("KALSHI_MAIL_ENV_FILE", str(ROOT / "workflow-scripts" / "initial-contact" / ".env")))
    if path.is_file():
        for line in path.read_text(encoding="utf-8").splitlines():
            key, separator, value = line.strip().partition("=")
            if separator and not key.startswith("#"):
                settings[key.strip()] = value.strip().strip('"').strip("'")
    settings.update(os.environ)
    return settings


def parse_request(raw: bytes):
    message = BytesParser(policy=policy.default).parsebytes(raw)
    # Exact subject avoids replies, forwards and the consulting/sales scanners.
    if str(message.get("Subject", "")).strip().casefold() != SUBJECT.casefold():
        return None
    if message.get("Auto-Submitted", "no").lower() != "no":
        return None
    body = message.get_body(preferencelist=("plain",))
    if body is None:
        return None
    fields = {}
    # Only website envelope fields BEFORE Message are trusted as contact fields.
    for line in body.get_content().splitlines():
        key, separator, value = line.partition(":")
        key = key.strip().casefold()
        if key == "message":
            break
        if separator:
            if key in fields:
                return None
            fields[key] = value.strip()
    address = fields.get("email", "")
    if not re.fullmatch(r"[^\s<>@]+@[^\s<>@]+\.[^\s<>@]+", address):
        return None
    if not fields.get("name"):
        return None
    return {
        "request_key": hashlib.sha256(raw).hexdigest(),
        "email": address,
        "name": fields["name"][:200],
        "youtube_channel": fields.get("youtube channel", "")[:200],
    }


def open_queue(path=DATABASE):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path, timeout=30)
    connection.execute("""CREATE TABLE IF NOT EXISTS requests (
        request_key TEXT PRIMARY KEY, email TEXT NOT NULL, name TEXT NOT NULL,
        youtube_channel TEXT NOT NULL, received_at TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'held_pending_setup')""")
    connection.commit()
    return connection


def queue_request(connection, request):
    with connection:
        cursor = connection.execute(
            "INSERT OR IGNORE INTO requests (request_key, email, name, youtube_channel, received_at) VALUES (?, ?, ?, ?, ?)",
            (request["request_key"], request["email"], request["name"],
             request["youtube_channel"], datetime.now(timezone.utc).isoformat()),
        )
    return cursor.rowcount == 1


def scan_requests():
    settings = load_mail_settings()
    if settings.get("KALSHI_SCAN_ENABLED", "").lower() != "true":
        return {"status": "setup_pending", "subject": SUBJECT,
                "message": "Scanner prepared. Set KALSHI_SCAN_ENABLED=true after website routing is ready.", "added": 0}
    if not settings.get("EMAIL_USERNAME") or not settings.get("EMAIL_PASSWORD"):
        return {"status": "setup_pending", "message": "Mail credentials missing.", "added": 0}
    connection = open_queue()
    added = skipped = 0
    try:
        with imaplib.IMAP4_SSL(settings.get("EMAIL_HOST", "imap.gmail.com"),
                              int(settings.get("EMAIL_PORT", "993")), timeout=30) as mailbox:
            mailbox.login(settings["EMAIL_USERNAME"], "".join(settings["EMAIL_PASSWORD"].split()))
            status, _ = mailbox.select(settings.get("EMAIL_FOLDER", "INBOX"), readonly=True)
            if status != "OK":
                raise RuntimeError("Unable to open request mailbox")
            # ALL includes read mail so another client cannot cause a missed request.
            cutoff = datetime.now(timezone.utc) - timedelta(days=30)
            since = f"{cutoff.day:02d}-{('Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec')[cutoff.month-1]}-{cutoff.year}"
            status, data = mailbox.uid("search", None, f'ALL SINCE {since} SUBJECT "{SUBJECT}"')
            if status != "OK":
                raise RuntimeError("Request mailbox search failed")
            for uid in (data[0] or b"").split():
                status, parts = mailbox.uid("fetch", uid, "(BODY.PEEK[])")
                if status != "OK":
                    raise RuntimeError("Unable to read request message; retry scan")
                raw = next((part[1] for part in parts if isinstance(part, tuple)), None)
                if raw is None:
                    raise RuntimeError("Request message has no payload; retry scan")
                request = parse_request(raw)
                if request:
                    added += int(queue_request(connection, request))
                else:
                    skipped += 1
    finally:
        connection.close()
    return {"status": "scanned", "added": added, "skipped": skipped, "subject": SUBJECT}


def access_gate(scan_result):
    return {"status": "held_pending_setup", "message": HOLD_REASON, "scan": scan_result}


def draft_download_email(gate_result):
    # No SMTP or send API: workflow execution cannot release an unverified download.
    return {
        "status": "draft_only", "delivery_enabled": False,
        "message": HOLD_REASON, "gate": gate_result,
        "draft": {
            "subject": "Your Kalshi BTC bot download",
            "body": "Hi [NAME],\n\nHere is your Kalshi BTC bot download:\n[DOWNLOAD_LINK]\n\nFollow the included setup instructions to get started.\n\nJosh",
        },
    }
