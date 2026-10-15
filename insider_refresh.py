"""Refresh Jev's U.S. corporate-insider database from SEC Form 4 filings.

This collects reported Form 4 open-market purchases (P) and sales (S).
It never places orders and keeps each SEC source URL for auditability.
"""
import argparse
import csv
import gzip
import json
import os
import sqlite3
import sys
import time
import tempfile
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent
DB = ROOT / "data" / "insiders.sqlite3"
CSV = ROOT / "data" / "insider_purchases.csv"
RULES = ROOT / "insider_rules.json"
SEC_ARCHIVES = "https://www.sec.gov/Archives/edgar"


def load_dotenv():
    """Load local workflow configuration without replacing real environment values."""
    path = ROOT / ".env"
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        if "=" in line and not line.lstrip().startswith("#"):
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip().strip("\"'"))


def load_rules():
    with RULES.open() as f:
        rules = json.load(f)
    required = {"refresh_lookback_days", "minimum_purchase_usd", "minimum_ownership_change_pct",
                "cluster_window_days", "cluster_minimum_insiders", "included_titles", "transaction_code"}
    if set(rules) != required or rules["transaction_code"] != "P":
        raise ValueError("insider_rules.json is incomplete or does not define P-purchase thesis rules.")
    return rules


def init_db(db):
    db.executescript("""
        CREATE TABLE IF NOT EXISTS filings (
          accession TEXT PRIMARY KEY, issuer_cik TEXT NOT NULL, issuer_name TEXT NOT NULL,
          ticker TEXT, reporting_owner TEXT NOT NULL, owner_cik TEXT, owner_title TEXT,
          filed_at TEXT NOT NULL, source_url TEXT NOT NULL, fetched_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS purchases (
          id INTEGER PRIMARY KEY, accession TEXT NOT NULL REFERENCES filings(accession),
          transaction_date TEXT NOT NULL, security_title TEXT NOT NULL, shares REAL NOT NULL,
          price REAL NOT NULL, value_usd REAL NOT NULL, owned_after REAL,
          ownership_form TEXT, ownership_change_pct REAL, qualifies INTEGER NOT NULL,
          transaction_code TEXT NOT NULL DEFAULT 'P',
          UNIQUE(accession, transaction_date, security_title, shares, price, ownership_form)
        );
        CREATE INDEX IF NOT EXISTS purchases_date ON purchases(transaction_date DESC);
        CREATE INDEX IF NOT EXISTS purchases_qualifies ON purchases(qualifies, transaction_date DESC);
        CREATE TABLE IF NOT EXISTS refresh_log (
          id INTEGER PRIMARY KEY, started_at TEXT NOT NULL, completed_at TEXT,
          filings_seen INTEGER NOT NULL DEFAULT 0, purchases_seen INTEGER NOT NULL DEFAULT 0,
          error TEXT
        );
        CREATE TABLE IF NOT EXISTS filing_transaction_coverage (
          accession TEXT PRIMARY KEY REFERENCES filings(accession),
          covered_at TEXT NOT NULL
        );
    """)
    try: db.execute("ALTER TABLE purchases ADD COLUMN transaction_code TEXT NOT NULL DEFAULT 'P'")
    except sqlite3.OperationalError: pass


def sec_get(url, user_agent):
    request = urllib.request.Request(url, headers={"User-Agent": user_agent, "Accept-Encoding": "gzip"})
    for attempt in range(3):
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                body = response.read()
                if response.headers.get("Content-Encoding", "").lower() == "gzip":
                    body = gzip.decompress(body)
                return body.decode("utf-8", errors="replace")
        except urllib.error.HTTPError as e:
            error = f"SEC returned HTTP {e.code} for {url}"
            if e.code not in {429, 500, 502, 503, 504}:
                raise ValueError(error) from None
        except (urllib.error.URLError, TimeoutError) as e:
            error = f"Could not reach SEC EDGAR for {url}: {e}"
        if attempt < 2:
            print(f"Retrying SEC request in {2 ** (attempt + 1)} seconds: {url}", file=sys.stderr, flush=True)
            time.sleep(2 ** (attempt + 1))
    raise ValueError(error)


def daily_index_url(day):
    quarter = (day.month - 1) // 3 + 1
    return f"{SEC_ARCHIVES}/daily-index/{day.year}/QTR{quarter}/master.{day:%Y%m%d}.idx"


def form4_submission_urls(index_text):
    """Return deduplicated archive .txt URLs from an EDGAR daily master index."""
    results = []
    for line in index_text.splitlines():
        columns = line.split("|")
        if len(columns) == 5 and columns[2] in {"4", "4/A"}:
            results.append(f"https://www.sec.gov/Archives/{columns[4]}")
    return list(dict.fromkeys(results))


def accession_from_url(url):
    return url.rsplit("/", 1)[-1].removesuffix(".txt")


def _text(parent, path):
    found = parent.find(path)
    return found.text.strip() if found is not None and found.text else None


def ownership_xml(document):
    """Extract the ownershipDocument XML from an EDGAR submission text file."""
    start = document.find("<ownershipDocument")
    end = document.find("</ownershipDocument>", start)
    if start < 0 or end < 0:
        return None
    return document[start:end + len("</ownershipDocument>")]


def parse_form4(document, source_url, filed_at):
    raw = ownership_xml(document)
    if not raw:
        return None, []
    try:
        root = ET.fromstring(raw)
    except ET.ParseError:
        return None, []
    issuer = root.find("issuer")
    owner = root.find("reportingOwner")
    if issuer is None or owner is None:
        return None, []
    relationship = owner.find("reportingOwnerRelationship")
    title_bits = []
    if relationship is not None:
        for tag, label in (("isDirector", "Director"), ("isOfficer", "Officer"), ("isTenPercentOwner", "10% Owner")):
            if _text(relationship, tag) == "1": title_bits.append(label)
        officer_title = _text(relationship, "officerTitle")
        if officer_title: title_bits.append(officer_title)
    accession = accession_from_url(source_url)
    filing = {
        "accession": accession, "issuer_cik": _text(issuer, "issuerCik") or "",
        "issuer_name": _text(issuer, "issuerName") or "Unknown issuer",
        "ticker": _text(issuer, "issuerTradingSymbol"),
        "reporting_owner": _text(owner, "reportingOwnerId/rptOwnerName") or "Unknown owner",
        "owner_cik": _text(owner, "reportingOwnerId/rptOwnerCik"),
        "owner_title": "; ".join(title_bits) or None,
        "filed_at": filed_at, "source_url": source_url,
    }
    transactions = []
    for node in root.findall(".//nonDerivativeTransaction"):
        code = _text(node, "transactionCoding/transactionCode")
        if code not in {"P", "S"}:
            continue
        shares = _text(node, "transactionAmounts/transactionShares/value")
        price = _text(node, "transactionAmounts/transactionPricePerShare/value")
        traded = _text(node, "transactionDate/value")
        owned = _text(node, "postTransactionAmounts/sharesOwnedFollowingTransaction/value")
        form = _text(node, "ownershipNature/directOrIndirectOwnership/value")
        try:
            shares, price = float(shares), float(price)
            owned = float(owned) if owned else None
        except (TypeError, ValueError):
            continue
        if shares <= 0 or price <= 0 or not traded:
            continue
        before = owned - shares if code == "P" and owned is not None else owned + shares if owned is not None else None
        change = (shares / before * 100) if before and before > 0 else None
        if code == "S" and change is not None: change = -change
        transactions.append({"transaction_date": traded, "security_title": _text(node, "securityTitle/value") or "Common stock",
                             "shares": shares, "price": price, "value_usd": shares * price, "owned_after": owned,
                             "transaction_code": code,
                             "ownership_form": form, "ownership_change_pct": change})
    return filing, transactions


def is_qualified(filing, transaction, rules):
    if transaction.get("transaction_code") != "P":
        return 0
    title = (filing["owner_title"] or "").lower()
    role_ok = any(role in title for role in rules["included_titles"])
    value_ok = transaction["value_usd"] >= rules["minimum_purchase_usd"]
    increase_ok = (transaction["ownership_change_pct"] or 0) >= rules["minimum_ownership_change_pct"]
    return int(role_ok and value_ok and (increase_ok or transaction["ownership_change_pct"] is None))


def refresh(db, user_agent, lookback_days=None, today=None, pause=0.12):
    rules = load_rules()
    days = lookback_days if lookback_days is not None else rules["refresh_lookback_days"]
    if not isinstance(days, int) or not 1 <= days <= 14:
        raise ValueError("lookback days must be between 1 and 14.")
    today = today or datetime.now(ZoneInfo("America/New_York")).date()
    started = datetime.now(timezone.utc).isoformat()
    cursor = db.execute("INSERT INTO refresh_log(started_at) VALUES (?)", (started,))
    log_id = cursor.lastrowid
    db.commit()
    filings_seen = purchases_seen = 0
    filings_skipped = purchases_added = 0
    known_accessions = {row[0] for row in db.execute("SELECT accession FROM filings")}
    transaction_coverage = {row[0] for row in db.execute("SELECT accession FROM filing_transaction_coverage")}
    try:
        # SEC publishes daily indexes overnight, not during the filing day.
        for offset in range(1, days + 1):
            day = today - timedelta(days=offset)
            if day.weekday() >= 5:
                continue
            try:
                urls = form4_submission_urls(sec_get(daily_index_url(day), user_agent))
            except ValueError as e:
                # Weekends and market holidays have no daily index. Connectivity, rate-limit,
                # and other SEC errors must remain visible to the calling workflow.
                if "HTTP 404" in str(e):
                    continue
                raise
            print(f"Checking {len(urls)} Form 4 index entries for {day}; downloading only unseen accessions...", file=sys.stderr, flush=True)
            for url in urls:
                accession = accession_from_url(url)
                if accession in known_accessions and accession in transaction_coverage:
                    filings_skipped += 1
                    continue
                time.sleep(pause)
                filing, transactions = parse_form4(sec_get(url, user_agent), url, day.isoformat())
                if not filing: continue
                filings_seen += 1
                db.execute("""INSERT INTO filings VALUES (:accession,:issuer_cik,:issuer_name,:ticker,:reporting_owner,:owner_cik,:owner_title,:filed_at,:source_url,:fetched_at)
                              ON CONFLICT(accession) DO UPDATE SET fetched_at=excluded.fetched_at""", {**filing, "fetched_at": datetime.now(timezone.utc).isoformat()})
                for transaction in transactions:
                    transaction["qualifies"] = is_qualified(filing, transaction, rules)
                    inserted = db.execute("""INSERT OR IGNORE INTO purchases(accession,transaction_date,security_title,shares,price,value_usd,owned_after,ownership_form,ownership_change_pct,qualifies,transaction_code)
                                  VALUES (:accession,:transaction_date,:security_title,:shares,:price,:value_usd,:owned_after,:ownership_form,:ownership_change_pct,:qualifies,:transaction_code)""", {**transaction, "accession": filing["accession"]})
                    purchases_seen += 1
                    purchases_added += inserted.rowcount
                db.execute("INSERT OR REPLACE INTO filing_transaction_coverage(accession,covered_at) VALUES (?,?)", (accession, datetime.now(timezone.utc).isoformat()))
                known_accessions.add(accession)
                transaction_coverage.add(accession)
                if filings_seen % 100 == 0:
                    print(f"Added {filings_seen} new filings and {purchases_added} purchases.", file=sys.stderr, flush=True)
        db.execute("UPDATE refresh_log SET completed_at=?,filings_seen=?,purchases_seen=? WHERE id=?", (datetime.now(timezone.utc).isoformat(), filings_seen, purchases_seen, log_id))
        db.commit()
        total_purchases = db.execute("SELECT COUNT(*) FROM purchases").fetchone()[0]
        print(f"Added {filings_seen} new filings and {purchases_added} purchases; skipped {filings_skipped} already stored entries. Total purchases: {total_purchases}.", file=sys.stderr, flush=True)
        return {"ok": True, "filings_seen": filings_seen, "purchases_seen": purchases_seen,
                "filings_added": filings_seen, "filings_skipped": filings_skipped,
                "purchases_added": purchases_added, "total_purchases": total_purchases}
    except Exception as e:
        db.rollback()
        db.execute("UPDATE refresh_log SET completed_at=?,filings_seen=?,purchases_seen=?,error=? WHERE id=?", (datetime.now(timezone.utc).isoformat(), filings_seen, purchases_seen, str(e), log_id))
        db.commit()
        raise


def export_purchases(db, path):
    """Publish a complete joined snapshot without exposing a partially written CSV."""
    rows = db.execute("""SELECT f.*, p.transaction_date, p.security_title, p.shares,
                         p.price, p.value_usd, p.owned_after, p.ownership_form,
                         p.ownership_change_pct, p.qualifies, p.transaction_code
                         FROM purchases p JOIN filings f USING (accession)
                         ORDER BY p.transaction_date DESC, f.accession, p.id""")
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", newline="",
                                         dir=path.parent, delete=False) as output:
            temporary = Path(output.name)
            writer = csv.writer(output)
            writer.writerow(column[0] for column in rows.description)
            writer.writerows(rows)
        temporary.chmod(0o644)
        temporary.replace(path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def main():
    parser = argparse.ArgumentParser(description="Refresh Jev corporate-insider purchases from SEC Form 4.")
    parser.add_argument("--days", type=int, help="completed calendar days of SEC daily indexes to re-check (1-14)")
    args = parser.parse_args()
    load_dotenv()
    agent = os.getenv("JEV_SEC_USER_AGENT")
    if not agent or "@" not in agent:
        raise SystemExit(f"Set JEV_SEC_USER_AGENT='JevInsiderMonitor your-contact-email' in {ROOT / '.env'} before refreshing.")
    DB.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(DB) as db:
        init_db(db)
        result = refresh(db, agent, args.days)
        export_purchases(db, CSV)
    result.update(database_path=str(DB), csv_path=str(CSV))
    print(json.dumps(result))
    return result


if __name__ == "__main__":
    result = main()
