import hashlib
import html
import importlib.util
import json
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock
from urllib.parse import urlsplit

from intel import api, feed, labels, records, render


class FeedVerificationRuntimeTest(unittest.TestCase):
    @unittest.skipUnless(shutil.which("node"), "Node is unavailable")
    def test_feed_and_verification_state_boundaries(self):
        for scenario in ("feed-future", "feed-not-run", "feed-fresh-status", "feed-freshness", "feed-coverage", "feed-filter", "feed-acceptance", "feed-invalid", "verify-explicit", "verify-stale",
                         "verify-timeout", "verify-body-timeout", "verify-invalid"):
            with self.subTest(scenario=scenario):
                result = subprocess.run([shutil.which("node"), "tests/feed_verify_runtime.cjs", scenario],
                                        input=render.VERIFY_SCRIPT, text=True, capture_output=True,
                                        cwd=render.ROOT, timeout=15)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    @unittest.skipUnless(shutil.which("node"), "Node is unavailable")
    def test_feed_and_explore_render_compact_coverage_disclosures(self):
        for page in (render.feed_page(), render.explore_page([], [], [], labels.load())):
            with self.subTest(title=page.split("<title>", 1)[1].split("</title>", 1)[0]):
                scripts = [script for script in re.findall(r"<script>(.*?)</script>", page, re.DOTALL)
                           if 'const panel = document.getElementById("feed-health")' in script]
                self.assertEqual(len(scripts), 1)
                result = subprocess.run([shutil.which("node"), "tests/feed_verify_runtime.cjs", "feed-coverage-render"],
                                        input=scripts[0], text=True, capture_output=True,
                                        cwd=render.ROOT, timeout=15)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertIn('<link rel="stylesheet" href="/style.css">', page)
                self.assertIn(".feed-panel #feed-health .feed-coverage{grid-column:1/-1;min-width:0}", render.STYLE)


class FeedWorkspaceTest(unittest.TestCase):
    def test_nine_digit_height_summaries_fit_on_phone_and_values_reflow(self):
        if not importlib.util.find_spec("playwright"):
            self.skipTest("Browser package is not importable")
        from playwright.sync_api import Error, sync_playwright

        runtime = sync_playwright().start()
        self.addCleanup(runtime.stop)
        try:
            browser = runtime.chromium.launch(headless=True)
        except (Error, OSError) as error:
            self.skipTest("Browser cannot launch: " + str(error))
        self.addCleanup(browser.close)
        heights = [*range(225875399, 225874999, -1), 225874999, 225875000]
        pages = {"/feed.html": render.feed_page(), "/explore.html": render.explore_page([], [], [], labels.load())}
        assets = {}
        for path in (render.ROOT / "intel/ui").glob("*.js"):
            source = path.read_text(encoding="utf-8")
            assets[render._script_asset(source)[0]] = source
        fonts = {"/fonts/" + path.name: path for path in (render.ROOT / "intel/ui/fonts").iterdir()}

        def local_route(route):
            path = urlsplit(route.request.url).path
            if path in pages:
                route.fulfill(body=pages[path], content_type="text/html")
            elif path == "/style.css":
                route.fulfill(body=render.STYLE, content_type="text/css")
            elif path in assets:
                route.fulfill(body=assets[path], content_type="application/javascript")
            elif path in fonts:
                route.fulfill(path=str(fonts[path]))
            elif path == "/feed":
                route.fulfill(json={"events": [], "event_ids": [], "follower": "running", "stats": {"skipped_heights": heights}})
            else:
                route.abort()

        with browser.new_context(reduced_motion="reduce") as context:
            context.route("**/*", local_route)
            page = context.new_page()
            for width in (375, 1440):
                page.set_viewport_size({"width": width, "height": 900})
                for path in pages:
                    with self.subTest(width=width, page=path):
                        page.goto("http://siyab.local" + path, wait_until="networkidle")
                        page.evaluate("document.fonts.ready")
                        detail = page.locator("#feed-health .feed-coverage")
                        summary = detail.locator("summary")
                        self.assertEqual(summary.inner_text(), "Skipped heights: 402 (225874999 to 225875399)")
                        self.assertEqual(summary.bounding_box()["height"], 44)
                        self.assertFalse(detail.locator("p").is_visible())
                        summary.click()
                        self.assertEqual(detail.locator("p").inner_text(), ", ".join(str(height) for height in heights))
                        self.assertLessEqual(page.evaluate("document.documentElement.scrollWidth"), width)

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
