"""Auditable public-policy catalyst collector for JEV research.

This is a research database, not a broker, signal executor, or political-trade
recommendation engine. Every record is kept with its primary-source URL.
"""
import argparse
import csv
import hashlib
import json
import sqlite3
import time
import tempfile
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from html.parser import HTMLParser
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DB = ROOT / "data" / "policy_catalysts.sqlite3"
CSV = ROOT / "data" / "policy_catalysts.csv"
RULES = ROOT / "policy_tracker_rules.json"
USER_AGENT = "JevPolicyTracker/1.0 research@localhost"


def load_rules():
    with RULES.open(encoding="utf-8") as source:
        data = json.load(source)
    required = {"sources", "entity_aliases", "tier_1_terms", "tier_2_terms", "tier_3_terms"}
    if set(data) != required:
        raise ValueError("policy_tracker_rules.json is incomplete")
    return data


def connect(path=DB):
    path.parent.mkdir(exist_ok=True)
    db = sqlite3.connect(path)
    db.row_factory = sqlite3.Row
    return db


def init_db(db):
    db.executescript("""
        CREATE TABLE IF NOT EXISTS policy_events (
          event_key TEXT PRIMARY KEY, source_id TEXT NOT NULL, title TEXT NOT NULL,
          url TEXT NOT NULL, published_at TEXT, summary TEXT NOT NULL,
          tier INTEGER NOT NULL, category TEXT NOT NULL, fetched_at REAL NOT NULL
        );
        CREATE TABLE IF NOT EXISTS policy_entities (
          event_key TEXT NOT NULL REFERENCES policy_events(event_key),
          ticker TEXT NOT NULL, matched_alias TEXT NOT NULL,
          PRIMARY KEY(event_key, ticker)
        );
        CREATE INDEX IF NOT EXISTS policy_events_fetched ON policy_events(fetched_at DESC);
        CREATE INDEX IF NOT EXISTS policy_entities_ticker ON policy_entities(ticker);
    """)


def fetch(url):
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/rss+xml,text/html"})
    try:
        with urllib.request.urlopen(request, timeout=25) as response:
            return response.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as error:
        raise ValueError(f"{url} returned HTTP {error.code}") from None
    except (urllib.error.URLError, TimeoutError):
        raise ValueError(f"Could not reach {url}") from None


def rss_items(raw):
    try:
        root = ET.fromstring(raw)
    except ET.ParseError:
        return []
    entries = root.findall(".//item") or root.findall(".//{http://www.w3.org/2005/Atom}entry")
    items = []
    for entry in entries:
        def text(name):
            value = entry.findtext(name) or entry.findtext(f"{{http://www.w3.org/2005/Atom}}{name}")
            return value.strip() if value else ""
        link = text("link")
        if not link:
            link_node = entry.find("{http://www.w3.org/2005/Atom}link")
            link = (link_node.get("href") if link_node is not None else "") or ""
        title = text("title")
        if title and link:
            items.append({"title": title, "url": link, "published_at": text("pubDate") or text("updated"), "summary": text("description") or text("summary")})
    return items


class ReleaseLinks(HTMLParser):
    def __init__(self):
        super().__init__(); self.links = []; self.href = None; self.parts = []
    def handle_starttag(self, tag, attrs):
        if tag == "a":
            self.href = dict(attrs).get("href"); self.parts = []
    def handle_data(self, data):
        if self.href: self.parts.append(data)
    def handle_endtag(self, tag):
        if tag == "a" and self.href:
            title = " ".join("".join(self.parts).split())
            if title and self.href:
                self.links.append({"title": title, "url": self.href, "published_at": "", "summary": title})
            self.href = None; self.parts = []


def html_release_items(raw, base="https://www.whitehouse.gov", link_pattern="/releases/"):
    parser = ReleaseLinks(); parser.feed(raw)
    seen, result = set(), []
    for item in parser.links:
        url = item["url"] if item["url"].startswith("http") else base.rstrip("/") + "/" + item["url"].lstrip("/")
        if link_pattern in url and url not in seen:
            seen.add(url); item["url"] = url; result.append(item)
    return result


def classify(text, rules):
    lower = text.lower()
    if any(term in lower for term in rules["tier_1_terms"]):
        return 1, "executed_capital_or_award"
    if any(term in lower for term in rules["tier_2_terms"]):
        return 2, "binding_policy_or_permit"
    if any(term in lower for term in rules["tier_3_terms"]):
        return 3, "rhetoric_or_theme"
    return 3, "policy_context"


def linked_entities(text, aliases):
    lower = text.lower()
    linked = []
    for ticker, names in aliases.items():
        matches = [alias for alias in names if alias.lower() in lower]
        if matches:
            linked.append((ticker, max(matches, key=len)))
    return linked


def ingest(db, source_id, items, rules):
    inserted = 0
    for item in items:
        text = f"{item['title']} {item.get('summary', '')}"
        tier, category = classify(text, rules)
        key = hashlib.sha256(f"{source_id}|{item['url']}".encode()).hexdigest()
        cursor = db.execute("""INSERT OR IGNORE INTO policy_events
          (event_key,source_id,title,url,published_at,summary,tier,category,fetched_at)
          VALUES (?,?,?,?,?,?,?,?,?)""", (key, source_id, item["title"], item["url"], item.get("published_at", ""), item.get("summary", ""), tier, category, time.time()))
        inserted += cursor.rowcount
        for ticker, alias in linked_entities(text, rules["entity_aliases"]):
            db.execute("INSERT OR IGNORE INTO policy_entities(event_key,ticker,matched_alias) VALUES (?,?,?)", (key, ticker, alias))
    return inserted


def refresh(rules, db):
    added, results = 0, []
    for source in rules["sources"]:
        try:
            raw = fetch(source["url"])
        except ValueError as error:
            results.append({"source": source["id"], "items": 0, "added": 0, "error": str(error)})
            continue
        items = rss_items(raw) if source["kind"] == "rss" else html_release_items(raw, source["url"], source.get("link_pattern", "/releases/"))
        count = ingest(db, source["id"], items, rules)
        added += count; results.append({"source": source["id"], "items": len(items), "added": count})
    db.commit()
    return {"ok": True, "added": added, "sources": results, "database_path": str(DB)}


def export_events(db, path=CSV):
    """Publish a stable flat snapshot for the dashboard without copying a live SQLite file."""
    rows = db.execute('''SELECT e.event_key,e.source_id,e.title,e.url,e.published_at,e.summary,
                                e.tier,e.category,e.fetched_at,GROUP_CONCAT(p.ticker, ', ') AS tickers
                         FROM policy_events e LEFT JOIN policy_entities p ON p.event_key=e.event_key
                         GROUP BY e.event_key ORDER BY e.fetched_at DESC,e.title''')
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", newline="", dir=path.parent, delete=False) as output:
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
    parser = argparse.ArgumentParser(description="Refresh JEV's public-policy catalyst research database.")
    parser.add_argument("--refresh", action="store_true", help="accepted for compatibility; refresh is always performed")
    args = parser.parse_args(); rules = load_rules()
    with connect() as db:
        init_db(db)
        result = refresh(rules, db)
        export_events(db)
        result["csv_path"] = str(CSV)
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
