import hashlib
import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from unittest import mock

from intel import enrichment


class EnrichmentTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.now = 1794344400.0
        self.directory = enrichment.EntityDirectory(self.temp.name, clock=lambda: self.now)

    def body(self, url):
        source = next(row for row in self.directory.sources.values() if row["url"] == url)
        return (enrichment.ROOT / source["fixture"]).read_bytes()

    def test_real_source_coverage_hashes_and_exact_mainnet_addresses(self):
        result = self.directory.search(limit=100)
        self.assertGreaterEqual(result["pagination"]["total"], 30)
        self.assertGreaterEqual(len({row["entity_id"] for row in result["items"]}), 8)
        for source in self.directory.sources.values():
            self.assertEqual(hashlib.sha256((enrichment.ROOT / source["fixture"]).read_bytes()).hexdigest(), source["content_sha256"])
        value = self.directory.lookup("mpdao-vote.near")
        row = value["entities"][0]
        self.assertEqual(row["name"], "Meta Pool")
        self.assertEqual(row["provenance"], "official_source_documented")
        self.assertIn("mpdao-vote.near", row["evidence"]["excerpt"])
        self.assertNotIn("owner", row)

    def test_lookup_never_guesses_ownership_from_suffix_or_exchange_names(self):
        for account in ("fake.meta-pool.near", "binance.near", "okx.near", "example.near"):
            self.assertEqual(self.directory.lookup(account)["entities"], [])
        for account in ("alice.testnet", "BAD.near", "a!"):
            with self.assertRaises(enrichment.EnrichmentInputError):
                self.directory.lookup(account)

    def test_alias_category_pagination_and_input_bounds(self):
        result = self.directory.search("REF Finance", category="dex", limit=2)
        self.assertEqual(len(result["items"]), 2)
        self.assertEqual(result["pagination"]["next_offset"], 2)
        second = self.directory.search("ref finance", category="dex", limit=2, offset=2)
        self.assertNotEqual(result["items"][0]["account_id"], second["items"][0]["account_id"])
        self.assertEqual(self.directory.search("LiNEAR")["items"][0]["account_id"], "linear-protocol.near")
        for kwargs in ({"limit": 101}, {"limit": True}, {"offset": -1}, {"query": "x" * 129}, {"category": "unknown"}):
            with self.assertRaises(enrichment.EnrichmentInputError):
                self.directory.search(**kwargs)

    def test_identical_source_refresh_persists_success_and_retrieval(self):
        before = self.directory.lookup("wrap.near")["entities"][0]
        result = self.directory.refresh(limit=8, fetcher=self.body)
        self.assertEqual(result["confirmed"], 8)
        restarted = enrichment.EntityDirectory(self.temp.name, clock=lambda: self.now)
        after = restarted.lookup("wrap.near")["entities"][0]
        self.assertEqual(after["check_status"], "confirmed")
        self.assertEqual(after["status"], "documented")
        self.assertEqual(after["evidence"]["first_retrieved_at"], before["evidence"]["retrieved_at"])
        self.assertEqual(after["evidence"]["content_sha256"], before["evidence"]["content_sha256"])

    def test_failures_preserve_dated_evidence_and_mark_stale(self):
        self.directory.refresh(limit=8, fetcher=self.body)
        before = self.directory.lookup("wrap.near")["entities"][0]
        self.now += 10

        def fail(url):
            raise OSError("provider message must never enter response")

        result = self.directory.refresh(limit=8, fetcher=fail)
        self.assertEqual(result["unavailable"], 8)
        after = self.directory.lookup("wrap.near")["entities"][0]
        self.assertEqual(after["status"], "stale")
        self.assertEqual(after["evidence"], before["evidence"])
        self.assertNotIn("provider message", json.dumps(after))

    def test_changed_source_needs_review_and_missing_exact_account_withdraws(self):
        before = self.directory.lookup("wrap.near")["entities"][0]

        def changed(url):
            return self.body(url) + b"\nDocumentation update\n"

        self.directory.refresh(limit=8, fetcher=changed)
        changed_row = self.directory.lookup("wrap.near")["entities"][0]
        self.assertEqual(changed_row["status"], "source_changed")
        self.assertEqual(changed_row["evidence"], before["evidence"])

        def withdrawn(url):
            return self.body(url).replace(b"wrap.near", b"replacement.near")

        self.now += 1
        self.directory.refresh(limit=8, fetcher=withdrawn)
        row = self.directory.lookup("wrap.near")["entities"][0]
        self.assertEqual(row["status"], "source_withdrawn")
        self.assertEqual(row["evidence"], before["evidence"])
        self.assertNotEqual(row["latest_check"]["content_sha256"], row["evidence"]["content_sha256"])

    def test_source_format_failure_is_not_a_revocation(self):
        self.directory.refresh(limit=8, fetcher=lambda url: b"<html>Access denied</html>")
        row = self.directory.lookup("wrap.near")["entities"][0]
        self.assertEqual(row["check_status"], "unavailable")
        self.assertEqual(row["status"], "stale")

    def test_age_and_deprecated_status_are_explicit(self):
        self.now += enrichment.STALE_AFTER_S + 86400
        row = self.directory.lookup("wrap.near")["entities"][0]
        self.assertEqual(row["status"], "stale")
        old = self.directory.lookup("meta-token.near")["entities"][0]
        self.assertEqual(old["lifecycle"], "deprecated")

    def test_refresh_budget_rotates_sources_and_rejects_oversized_bodies(self):
        calls = []

        def fetch(url):
            calls.append(url)
            return self.body(url)

        self.directory.refresh(limit=2, fetcher=fetch)
        self.now += 1
        self.directory.refresh(limit=2, fetcher=fetch)
        self.assertEqual(len(set(calls)), 4)
        result = self.directory.refresh(limit=8, fetcher=lambda url: b"x" * (enrichment.MAX_SOURCE_BYTES + 1))
        self.assertEqual(result["unavailable"], 8)
        with self.assertRaises(enrichment.EnrichmentInputError):
            self.directory.refresh(limit=9, fetcher=fetch)

    def test_source_state_cannot_be_written_inside_repository(self):
        with self.assertRaises(enrichment.EnrichmentInputError):
            enrichment.EntityDirectory(enrichment.ROOT / "fixtures")
        with self.assertRaises(enrichment.EnrichmentInputError):
            enrichment.EntityDirectory().refresh(fetcher=self.body)

    def test_public_results_do_not_alias_internal_catalog(self):
        row = self.directory.lookup("wrap.near")["entities"][0]
        row["aliases"].append("Binance")
        self.assertNotIn("Binance", self.directory.lookup("wrap.near")["entities"][0]["aliases"])

    def test_a_later_outage_cannot_erase_a_source_withdrawal(self):
        self.directory.refresh(limit=8, fetcher=lambda url: self.body(url).replace(b"wrap.near", b"replacement.near"))
        before = self.directory.lookup("wrap.near")["entities"][0]

        def fail(url):
            raise OSError("unavailable")

        self.now += 1
        self.directory.refresh(limit=8, fetcher=fail)
        after = self.directory.lookup("wrap.near")["entities"][0]
        self.assertEqual(after["status"], "source_withdrawn")
        self.assertEqual(after["check_status"], "unavailable")
        self.assertEqual(after["evidence"], before["evidence"])
        self.assertEqual(after["last_document_sha256"], before["latest_check"]["content_sha256"])

    def test_changed_source_bytes_are_retained_for_evidence_review(self):
        self.directory.refresh(limit=8, fetcher=lambda url: self.body(url) + b"\nSource update\n")
        with closing(sqlite3.connect(self.directory.path)) as connection:
            rows = connection.execute("SELECT source_id, content_sha256, body FROM source_documents").fetchall()
        self.assertEqual(len(rows), 8)
        for sid, digest, body in rows:
            self.assertEqual(hashlib.sha256(body).hexdigest(), digest)
            self.assertNotEqual(digest, self.directory.sources[sid]["content_sha256"])

    def test_transport_refuses_redirect_and_content_length_before_reading(self):
        response = mock.MagicMock()
        response.__enter__.return_value = response
        response.geturl.return_value = "https://untrusted.invalid/"
        opener = mock.Mock()
        opener.open.return_value = response
        with mock.patch.object(enrichment.urllib.request, "build_opener", return_value=opener):
            with self.assertRaises(enrichment.EnrichmentReadError):
                enrichment._fetch("https://docs.pyth.network/pyth-token/pyth-token-addresses.md", enrichment.time.monotonic() + 5)
            response.geturl.return_value = "https://docs.pyth.network/pyth-token/pyth-token-addresses.md"
            response.headers.get.return_value = str(enrichment.MAX_SOURCE_BYTES + 1)
            with self.assertRaises(enrichment.EnrichmentReadError):
                enrichment._fetch(response.geturl(), enrichment.time.monotonic() + 5)
        response.read.assert_not_called()

    def test_expired_refresh_transport_never_opens_network(self):
        with mock.patch.object(enrichment.urllib.request, "build_opener") as build:
            with self.assertRaises(TimeoutError):
                enrichment._fetch("https://docs.pyth.network/pyth-token/pyth-token-addresses.md", enrichment.time.monotonic() - 1)
            build.return_value.open.assert_not_called()

    def test_a_new_catalog_snapshot_cannot_inherit_an_old_confirmation(self):
        self.directory.refresh(limit=8, fetcher=self.body)
        self.directory.sources["wrap"]["content_sha256"] = "a" * 64
        value = self.directory.lookup("wrap.near")["entities"][0]
        self.assertEqual(value["check_status"], "bundled_snapshot")
        self.assertIsNone(value["last_checked_at"])

    def test_other_mentions_do_not_preserve_a_removed_exact_association(self):
        anchor = next(row["anchor"] for row in self.directory.entries if row["account_id"] == "v2.ref-finance.near")

        def fetch(url):
            return self.body(url).replace(anchor.encode(), b"Deployment association removed")

        self.directory.refresh(limit=8, fetcher=fetch)
        row = self.directory.lookup("v2.ref-finance.near")["entities"][0]
        self.assertEqual(row["status"], "source_withdrawn")
        self.assertEqual(row["evidence"]["excerpt"], anchor)


if __name__ == "__main__":
    unittest.main()
