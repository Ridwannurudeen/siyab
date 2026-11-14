import hashlib
import html
import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from intel import api, feed, records, render


class FeedVerificationRuntimeTest(unittest.TestCase):
    @unittest.skipUnless(shutil.which("node"), "Node is unavailable")
    def test_feed_and_verification_state_boundaries(self):
        for scenario in ("feed-future", "feed-not-run", "feed-fresh-status", "feed-freshness", "feed-filter", "feed-acceptance", "feed-invalid", "verify-explicit", "verify-stale",
                         "verify-timeout", "verify-body-timeout", "verify-invalid"):
            with self.subTest(scenario=scenario):
                result = subprocess.run([shutil.which("node"), "tests/feed_verify_runtime.cjs", scenario],
                                        input=render.VERIFY_SCRIPT, text=True, capture_output=True,
                                        cwd=render.ROOT, timeout=15)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


class FeedWorkspaceTest(unittest.TestCase):
    def test_events_are_readable_records_without_invented_counterparties(self):
        event = {"signal": "full_access_key_then_outflow", "subject": "agent.near", "detail": "Recorded outflow",
                 "destination": "implicit", "amount_yocto": "1000000000000000000000000",
                 "observed": {"block_height": 12, "timestamp_ms": 0}, "source": {"provider": "Mainnet blocks"}}
        page = render._feed_panel([(event, "a" * 64)])
        self.assertIn('class="feed-table"', page)
        self.assertIn('id="feed-signals"', page)
        self.assertIn('<button type="button" data-export-table="feed-signals">Export shown events CSV</button>', page)
        self.assertNotIn('<table class="feed-table" data-export-table=', page)
        self.assertIn('scope="col">Counterparty', page)
        self.assertIn('1970-01-01', page)
        self.assertIn('1000000000000000000000000 yoctoNEAR', page)
        self.assertIn('/evidence/' + "a" * 64, page)
        self.assertNotIn('href="/accounts/implicit', page)
        self.assertIn('Not recorded', page)

    def test_feed_page_retains_recorded_detail_and_coverage_when_live_is_unavailable(self):
        state = json.loads(feed.STATE_PATH.read_text(encoding="utf-8"))
        event = {"signal": "recorded signal", "detail": "<untrusted>", "observed": {"block_height": 3}}
        page = render.feed_page(state=state, event_rows=[(event, "legacy-name")])
        self.assertIn('Recorded window details', page)
        self.assertIn(html.escape(event["detail"]), page)
        self.assertIn('Unlinked record', page)
        self.assertNotIn('/evidence/legacy-name', page)
        self.assertIn('id="feed-retry"', page)

    def test_feed_ids_are_source_filenames_and_do_not_overwrite_event_fields(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            (directory / "events").mkdir()
            original = {"id": "source-reserved", "signal": "recorded", "detail": "unchanged",
                        "observed": {"block_height": 3}}
            encoded = json.dumps(original).encode()
            digest = hashlib.sha256(encoded).hexdigest()
            (directory / "events" / f"{digest}.json").write_bytes(encoded)
            (directory / "events" / "legacy.json").write_bytes(encoded)
            with mock.patch.dict("os.environ", {"FEED_STATE_DIR": tmp}):
                status, body = api._feed()
            self.assertEqual(status, 200)
            self.assertEqual(body["event_ids"], [digest, None])
            self.assertEqual(body["events"], [original, original])

    def test_external_feed_evidence_is_hash_bound_and_cannot_escape_its_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp) / "events"
            directory.mkdir()
            encoded = b'{"signal":"recorded","id":"source-reserved"}'
            digest = hashlib.sha256(encoded).hexdigest()
            (directory / f"{digest}.json").write_bytes(encoded)
            (directory / ("a" * 64 + ".json")).write_bytes(encoded)
            with mock.patch.dict("os.environ", {"FEED_STATE_DIR": tmp}), mock.patch.object(api, "EVIDENCE_DIRS", ()):
                self.assertEqual(api._evidence(digest), (200, json.loads(encoded)))
                self.assertEqual(api._evidence("a" * 64)[0], 404)
                self.assertEqual(api._evidence("../private")[0], 404)


class VerificationWorkspaceTest(unittest.TestCase):
    def test_saved_example_is_explicitly_historical_and_live_read_is_separate(self):
        page = render.verify_page()
        record = json.loads(render.VERIFY_RECORD.read_text(encoding="utf-8"))
        self.assertIn('Recorded example', page)
        self.assertIn('Live testnet read', page)
        self.assertIn('does not verify a signature', page)
        self.assertIn('id="verify-example"', page)
        self.assertIn(str(record["observed_block"]), page)
        self.assertIn(record["evidence_hash"], page)
        self.assertIn('testnet.nearblocks.io/address/', page)

    def test_record_index_content_has_semantic_rows_without_scripts_or_fabricated_state(self):
        documents = [records.read(path) for path in sorted(render.RECORDS_DIR.glob("*.json"))
                     if path.name != "body-vector.json"]
        page = render.record_index_page(documents)
        self.assertIn('class="record-index-table"', page)
        self.assertIn('scope="col">Recorded (UTC)', page)
        self.assertIn('Synthetic examples', page)
        self.assertIn('Superseded', page)
        main = page.split('<main class="app-content record-content" id="main-content">', 1)[1].split("</main>", 1)[0]
        self.assertNotIn('<script', main)
        self.assertNotIn('<button', main)
        for record in documents:
            self.assertIn(f'href="{record["record_id"]}.html">{record["record_id"]}</a>', page)
            self.assertIn(record["timeline"][-1]["at"], page)
            self.assertIn(record["subjects"][0]["account"], page)


if __name__ == "__main__":
    unittest.main()
