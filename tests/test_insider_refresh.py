import csv
import gzip
import io
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch
from datetime import date

import insider_refresh as insider


DOCUMENT = """<ownershipDocument>
<issuer><issuerCik>123</issuerCik><issuerName>Test Issuer</issuerName>
<issuerTradingSymbol>TEST</issuerTradingSymbol></issuer>
<reportingOwner><reportingOwnerId><rptOwnerName>Test Owner</rptOwnerName>
<rptOwnerCik>456</rptOwnerCik></reportingOwnerId>
<reportingOwnerRelationship><isDirector>1</isDirector></reportingOwnerRelationship>
</reportingOwner><nonDerivativeTable><nonDerivativeTransaction>
<securityTitle><value>Common Stock</value></securityTitle>
<transactionDate><value>2026-09-28</value></transactionDate>
<transactionCoding><transactionCode>P</transactionCode></transactionCoding>
<transactionAmounts><transactionShares><value>2000</value></transactionShares>
<transactionPricePerShare><value>100</value></transactionPricePerShare></transactionAmounts>
<postTransactionAmounts><sharesOwnedFollowingTransaction><value>10000</value>
</sharesOwnedFollowingTransaction></postTransactionAmounts>
<ownershipNature><directOrIndirectOwnership><value>D</value></directOrIndirectOwnership>
</ownershipNature></nonDerivativeTransaction></nonDerivativeTable></ownershipDocument>"""
INDEX = "123|Test Issuer|4|2026-09-28|edgar/data/123/test.txt"


class InsiderRefreshTests(unittest.TestCase):
    def test_refresh_persists_and_exports_without_duplicate_purchases(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "data" / "insiders.sqlite3"
            export = database.with_name("insider_purchases.csv")
            with patch.object(insider, "DB", database), patch.object(insider, "CSV", export), \
                 patch.object(insider, "load_dotenv"), \
                 patch.dict(os.environ, {"JEV_SEC_USER_AGENT": "Test test@example.com"}), \
                 patch("sys.argv", ["insider_refresh.py", "--days", "1"]), \
                 patch.object(insider, "sec_get", side_effect=[INDEX, DOCUMENT, INDEX]) as get, \
                 patch.object(insider.time, "sleep"), patch("sys.stdout", new_callable=io.StringIO):
                result = insider.main()
                original_csv = export.read_bytes()
                repeated = insider.main()
                self.assertEqual(export.read_bytes(), original_csv)
            self.assertEqual(get.call_count, 3)
            self.assertEqual(repeated["filings_added"], 0)
            self.assertEqual(repeated["purchases_added"], 0)
            self.assertEqual(repeated["filings_skipped"], 1)
            self.assertEqual(result["database_path"], str(database))
            self.assertEqual(result["csv_path"], str(export))
            with sqlite3.connect(database) as db:
                self.assertEqual(db.execute("SELECT COUNT(*) FROM purchases").fetchone()[0], 1)
                self.assertEqual(db.execute("SELECT COUNT(*) FROM refresh_log WHERE error IS NULL").fetchone()[0], 2)
            with export.open() as file:
                rows = list(csv.DictReader(file))
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["ticker"], "TEST")
            self.assertEqual(rows[0]["value_usd"], "200000.0")
            self.assertEqual(rows[0]["qualifies"], "1")
            self.assertTrue(rows[0]["source_url"].endswith("test.txt"))

    def test_new_filings_extend_csv_and_duplicate_index_accessions_are_skipped(self):
        with sqlite3.connect(":memory:") as db, tempfile.TemporaryDirectory() as directory:
            insider.init_db(db)
            alias = INDEX.replace("edgar/data/123/", "edgar/data/456/")
            new_index = INDEX.replace("test.txt", "new.txt")
            with patch.object(insider, "sec_get", side_effect=[INDEX + "\n" + alias, DOCUMENT]) as get:
                first = insider.refresh(db, "Test test@example.com", 1, today=date(2026, 9, 29), pause=0)
            self.assertEqual(get.call_count, 2)
            self.assertEqual(first["filings_added"], 1)
            self.assertEqual(first["filings_skipped"], 1)
            with patch.object(insider, "sec_get", side_effect=[INDEX + "\n" + new_index, DOCUMENT]) as get:
                second = insider.refresh(db, "Test test@example.com", 1, today=date(2026, 9, 29), pause=0)
            self.assertEqual(second["purchases_added"], 1)
            self.assertEqual(second["total_purchases"], 2)
            self.assertEqual(get.call_count, 2)
            path = Path(directory) / "purchases.csv"
            insider.export_purchases(db, path)
            with path.open() as file:
                self.assertEqual({row["accession"] for row in csv.DictReader(file)}, {"test", "new"})

    def test_filings_without_purchases_are_also_skipped_on_rerun(self):
        with sqlite3.connect(":memory:") as db:
            insider.init_db(db)
            with patch.object(insider, "sec_get", side_effect=[INDEX, DOCUMENT.replace("<transactionCode>P", "<transactionCode>S"), INDEX]) as get:
                insider.refresh(db, "Test test@example.com", 1, today=date(2026, 9, 29), pause=0)
                repeated = insider.refresh(db, "Test test@example.com", 1, today=date(2026, 9, 29), pause=0)
            self.assertEqual(get.call_count, 3)
            self.assertEqual(repeated["filings_skipped"], 1)
            self.assertEqual(repeated["total_purchases"], 0)

    def test_failure_log_survives_rollback_without_partial_data(self):
        with sqlite3.connect(":memory:") as db:
            insider.init_db(db)
            with patch.object(insider, "sec_get", side_effect=[INDEX, DOCUMENT, ValueError("SEC returned HTTP 403")]):
                with self.assertRaisesRegex(ValueError, "HTTP 403"):
                    insider.refresh(db, "Test test@example.com", 2, today=date(2026, 9, 30), pause=0)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM filings").fetchone()[0], 0)
            self.assertEqual(db.execute("SELECT error FROM refresh_log").fetchone()[0], "SEC returned HTTP 403")

    def test_gzip_response_is_decoded(self):
        response = io.BytesIO(gzip.compress(INDEX.encode()))
        response.headers = {"Content-Encoding": "gzip"}
        with patch.object(insider.urllib.request, "urlopen", return_value=response):
            self.assertEqual(insider.sec_get("https://www.sec.gov/test", "Test test@example.com"), INDEX)

    def test_uses_completed_days_and_skips_weekends(self):
        with sqlite3.connect(":memory:") as db:
            insider.init_db(db)
            with patch.object(insider, "sec_get", return_value="") as get:
                insider.refresh(db, "Test test@example.com", 3, today=date(2026, 9, 29), pause=0)
            get.assert_called_once_with(insider.daily_index_url(date(2026, 9, 28)), "Test test@example.com")

    def test_temporary_connection_failure_is_retried(self):
        response = io.BytesIO(INDEX.encode())
        response.headers = {}
        with patch.object(insider.urllib.request, "urlopen", side_effect=[TimeoutError("timeout"), response]) as get, \
             patch.object(insider.time, "sleep") as sleep:
            self.assertEqual(insider.sec_get("https://www.sec.gov/test", "Test test@example.com"), INDEX)
        self.assertEqual(get.call_count, 2)
        sleep.assert_called_once_with(2)

    def test_access_denied_is_not_retried(self):
        error = insider.urllib.error.HTTPError("https://www.sec.gov/test", 403, "Forbidden", {}, None)
        with patch.object(insider.urllib.request, "urlopen", side_effect=error) as get:
            with self.assertRaisesRegex(ValueError, "HTTP 403"):
                insider.sec_get("https://www.sec.gov/test", "Test test@example.com")
        get.assert_called_once()


if __name__ == "__main__":
    unittest.main()
