import hashlib
import html
import http.client
import json
import os
import re
import tempfile
import threading
import time
import unittest
import urllib.parse
from datetime import UTC, datetime
from pathlib import Path
from unittest import mock

from gate import rpc
from intel import api, feed, issuers, labels, render

ROOT = Path(__file__).resolve().parent.parent
EVIDENCE = min((ROOT / "fixtures" / "evidence").glob("*.json"))
TREASURY = "0x2CfF890f0378a11913B6129B2E97417a2c302680"
JSON_TYPE = "application/json; charset=utf-8"
LINK = re.compile(r'<a\b[^>]*\bhref="([^"]*)"')


def get(server, path):
    """(status, content type, cache control, body) of one GET to a server on loopback."""
    connection = http.client.HTTPConnection("127.0.0.1", server.server_address[1], timeout=10)
    try:
        connection.request("GET", path)
        reply = connection.getresponse()
        return reply.status, reply.getheader("Content-Type"), reply.getheader("Cache-Control"), reply.read()
    finally:
        connection.close()


def write_follow_state(state_dir, texts, **fields):
    """A follow state under state_dir, fields over its defaults (a last_time of now, so it reads running), and
    one event file per text, named by the sha256 of its bytes as the follower names them."""
    state = json.loads(feed.STATE_PATH.read_text(encoding="utf-8"))
    state.update(mode="follow", started_at="2026-11-03T12:00:00Z", last_height=1060,
                 last_time=datetime.fromtimestamp(time.time(), UTC).strftime(feed._TIME), lag_blocks=0)
    state.update(fields)
    (state_dir / "state.json").write_text(json.dumps(state), encoding="utf-8")
    (state_dir / "events").mkdir()
    for text in texts:
        name = hashlib.sha256(text.encode("utf-8")).hexdigest() + ".json"
        (state_dir / "events" / name).write_text(text, encoding="utf-8")


class ApiTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.store = labels.load()
        cls.site = tempfile.TemporaryDirectory()
        site = Path(cls.site.name)
        (site / "index.html").write_text("<!doctype html><title>demo</title>", encoding="utf-8")
        (site / "pages").mkdir()
        (site / "pages" / "a.css").write_text("body{}", encoding="utf-8")
        cls.server = api.make_server("127.0.0.1", 0, site=site, store=cls.store)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join()
        cls.site.cleanup()

    def request(self, path, method="GET"):
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_address[1], timeout=10)
        self.addCleanup(connection.close)
        connection.request(method, path)
        reply = connection.getresponse()
        body = reply.read()
        return reply.status, reply.getheader("Content-Type"), reply.getheader("Cache-Control"), body

    def get_json(self, path):
        status, content_type, cache, body = self.request(path)
        self.assertEqual(content_type, "application/json; charset=utf-8")
        self.assertEqual(cache, "no-store")
        return status, json.loads(body)

    def incident_subject(self):
        return next(a.subject for a in self.store.assertions if a.category == "incident")

    def test_health(self):
        self.assertEqual(self.get_json("/health"), (200, {"status": "ok", "assertions": len(self.store.assertions),
                                                         "label_store_sha256": self.store.sha256}))

    def test_the_api_search_matches_by_subject_label_and_entity(self):
        for term, expected_type in (("genesis.sputnik-dao.near", "account"),
                                    ("DAO created by the Sputnik DAO factory", "account"),
                                    ("Sputnik DAO: \"genesis\"", "entity")):
            with self.subTest(term=term):
                # The second argument leaves no character unencoded.
                status, body = self.get_json("/search/" + urllib.parse.quote(term, ""))
                self.assertEqual(status, 200)
                self.assertTrue(any(result["type"] == expected_type for result in body["results"]))
                for result in body["results"]:
                    folder = "accounts/" if result["type"] == "account" else "entities/"
                    self.assertTrue(result["url"].startswith(folder) and result["url"].endswith(".html"))

    def test_labels_on_every_chain(self):
        status, body = self.get_json(f"/labels/{TREASURY}")
        self.assertEqual(status, 200)
        self.assertEqual(body["subject"], TREASURY)
        expected = [a for a in self.store.assertions if a.subject == TREASURY]
        self.assertEqual([row["id"] for row in body["assertions"]], [a.id for a in expected])
        self.assertGreater(len({row["chain"] for row in body["assertions"]}), 1)
        self.assertEqual(sorted(body["assertions"][0]), sorted(labels.FIELDS))
        self.assertEqual(self.get_json("/labels/nobody-here.near"), (200, {"subject": "nobody-here.near",
                                                                          "assertions": []}))

    def test_incidents(self):
        status, body = self.get_json("/incidents/rhea")
        self.assertEqual(status, 200)
        self.assertEqual(body, json.loads((ROOT / "intel" / "incidents" / "rhea.json").read_text(encoding="utf-8")))
        self.assertEqual(self.get_json("/incidents/none-such"), (404, {"error": "no incident 'none-such'"}))
        self.assertEqual(self.get_json("/incidents/..%2Frhea")[0], 404)

    def test_evidence(self):
        status, body = self.get_json(f"/evidence/{EVIDENCE.stem}")
        self.assertEqual((status, body), (200, json.loads(EVIDENCE.read_bytes())))
        self.assertEqual(self.get_json("/evidence/" + "0" * 64)[0], 404)
        self.assertEqual(self.get_json("/evidence/..%2F..%2FREADME")[0], 404)

    def test_evidence_from_the_feed_events(self):
        with tempfile.TemporaryDirectory() as tmp:
            event = Path(tmp) / ("ab" * 32 + ".json")
            event.write_text('{"verdict": "DENY"}', encoding="utf-8")
            with mock.patch.object(api, "EVIDENCE_DIRS", (ROOT / "fixtures" / "evidence", Path(tmp))):
                self.assertEqual(self.get_json(f"/evidence/{event.stem}"), (200, {"verdict": "DENY"}))

    def test_feed_is_empty_without_the_feed_files(self):
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.object(api, "FEED_STATE", Path(tmp) / "state.json"):
            self.assertEqual(self.get_json("/feed"), (200, {"follower": None, "stats": None, "events": [], "event_ids": []}))

    def test_feed_returns_the_state_and_the_last_50_events_in_block_order(self):
        with tempfile.TemporaryDirectory() as tmp:
            events = Path(tmp) / "events"
            events.mkdir()
            (Path(tmp) / "state.json").write_text('{"blocks": 300}', encoding="utf-8")
            for number in range(60):
                document = {"observed": {"block_height": 1000 - number}, "n": number}
                (events / f"{number:064x}.json").write_text(json.dumps(document), encoding="utf-8")
            with mock.patch.object(api, "FEED_STATE", Path(tmp) / "state.json"):
                status, body = self.get_json("/feed")
        self.assertEqual(status, 200)
        self.assertEqual(body["stats"], {"blocks": 300})
        self.assertEqual([event["n"] for event in body["events"]], list(range(49, -1, -1)))
        self.assertEqual(body["event_ids"], [f"{number:064x}" for number in range(49, -1, -1)])

    def test_the_follower_reads_stopped_a_second_past_the_threshold_by_updated_at_else_last_time_else_started_at(self):
        written = datetime(2026, 11, 5, 5, 3, 12, tzinfo=UTC).timestamp()
        limit = written + api.FOLLOWER_STOPPED_AFTER_S
        stamp, earlier = "2026-11-05T05:03:12Z", "2026-11-05T01:00:00Z"
        cases = (("updated_at", {"updated_at": stamp, "last_time": earlier, "started_at": earlier}),
                 ("last_time", {"last_time": stamp, "started_at": earlier}),
                 ("last_time past a null updated_at", {"updated_at": None, "last_time": stamp, "started_at": earlier}),
                 ("last_time past a malformed updated_at",
                  {"updated_at": "2026-11-05 05:03:12", "last_time": stamp, "started_at": earlier}),
                 ("started_at", {"last_time": None, "started_at": stamp}))
        for judged_by, state in cases:
            with self.subTest(judged_by=judged_by):
                self.assertIsNone(api._stopped_since(state, limit))
                self.assertEqual(api._stopped_since(state, limit + 1), stamp)
        self.assertEqual(api._stopped_since({"last_time": None, "started_at": None}, written), "an unknown time")

    def test_the_stopped_threshold_clears_the_longest_gap_between_two_stamps_of_a_running_follower(self):
        requests = len(feed.Context(self.store).deny_receivers) - 1 + feed.MAX_TRANSFER_PAGES
        poll = requests * rpc.TIMEOUT_S + (requests - 1) * feed.FOLLOW_INTERVAL_S
        final_read = 2 * rpc.TIMEOUT_S + feed.BACKOFF_S + 2 * rpc.TIMEOUT_S
        fetch = rpc.TIMEOUT_S + feed.BACKOFF_S + rpc.TIMEOUT_S
        self.assertGreater(api.FOLLOWER_STOPPED_AFTER_S,
                           max(1 + poll, 1 + final_read + feed.FOLLOW_INTERVAL_S + poll,
                               final_read + feed.FOLLOW_INTERVAL_S + fetch))

    def test_flow_refuses_a_subject_without_an_incident_label(self):
        subject = "intents.near"
        self.assertTrue(self.store.by_subject(subject, "near"))
        self.assertNotIn("incident", {a.category for a in self.store.by_subject(subject, "near")})
        self.assertEqual(self.get_json(f"/flow/{subject}"), (403, {"error": "subject carries no incident label"}))
        self.assertEqual(self.get_json("/flow/nobody-here.near"), (403, {"error": "subject carries no incident label"}))

    def test_flow_for_an_incident_subject(self):
        subject = self.incident_subject()
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(api, "FLOWS_DIR", Path(tmp)):
            self.assertEqual(self.get_json(f"/flow/{subject}"), (404, {"error": f"no exit trace for {subject}"}))
            (Path(tmp) / f"{subject}.json").write_text('{"hops": []}', encoding="utf-8")
            self.assertEqual(self.get_json(f"/flow/{subject}"), (200, {"hops": []}))

    def test_the_api_refuses_a_cluster_for_a_subject_without_an_incident_label(self):
        subject = "intents.near"
        self.assertTrue(self.store.by_subject(subject, "near"))
        self.assertNotIn("incident", {a.category for a in self.store.by_subject(subject, "near")})
        self.assertEqual(self.get_json(f"/cluster/{subject}"), (403, {"error": "subject carries no incident label"}))
        self.assertEqual(self.get_json("/cluster/nobody-here.near"), (403, {"error": "subject carries no incident label"}))

    def test_the_api_serves_the_cluster_of_a_seed_and_404_without_a_file(self):
        document = json.loads((ROOT / "intel" / "clusters" / "rhea.json").read_bytes())
        for seed in (document["seeds"][0], document["seeds"][-1]):
            with self.subTest(seed=seed):
                self.assertEqual(self.get_json(f"/cluster/{seed}"), (200, document))
        subject = self.incident_subject()
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(api, "CLUSTERS_DIR", Path(tmp)):
            self.assertEqual(self.get_json(f"/cluster/{subject}"), (404, {"error": f"no cluster for {subject}"}))

    def test_the_api_serves_the_issuer_rows(self):
        status, body = self.get_json("/issuers")
        self.assertEqual(status, 200)
        for key, path in (("status", issuers.STATUS_PATH), ("history", issuers.HISTORY_PATH)):
            with self.subTest(rows=key):
                rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
                self.assertTrue(rows)
                self.assertEqual(body[key], rows)
        reads = issuers.reads()
        self.assertEqual((body["status_reads"], body["status_read_subjects"]),
                         (len(reads), len({read["subject"] for read in reads})))
        self.assertEqual(body["history_coverage"], issuers.history_coverage())

    def test_static_files_stay_under_the_site(self):
        status, content_type, cache, body = self.request("/")
        self.assertEqual((status, content_type, cache), (200, "text/html", "no-store"))
        self.assertIn(b"<title>demo</title>", body)
        status, content_type, _, body = self.request("/pages/a.css")
        self.assertEqual((status, content_type, body), (200, "text/css", b"body{}"))
        for path in ("/../tests/test_api.py", "/%2e%2e/%2e%2e/README.md", "/missing.html", "/a%00b.html"):
            with self.subTest(path=path):
                self.assertEqual(self.get_json(path), (404, {"error": "not found"}))

    def test_no_write_route(self):
        for method in ("POST", "PUT", "DELETE"):
            with self.subTest(method=method):
                self.assertEqual(self.request("/labels/intents.near", method)[0], 501)


class HostedSiteTest(unittest.TestCase):
    """The API with --site set to a fresh render, the way the hosted copy runs it."""

    @classmethod
    def setUpClass(cls):
        cls.store = labels.load()
        cls.site = tempfile.TemporaryDirectory()
        render.render(cls.site.name, store=cls.store)
        cls.server = api.make_server("127.0.0.1", 0, site=cls.site.name, store=cls.store)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join()
        cls.site.cleanup()

    def test_landing_and_explore_links_answer_200_with_html_through_the_server(self):
        for name in ("index.html", "explore.html"):
            page = (Path(self.site.name) / name).read_text(encoding="utf-8")
            links = [html.unescape(link) for link in LINK.findall(page) if not link.startswith("https://")]
            self.assertIn("incidents/rhea.html", [link.lstrip("/") for link in links])
            for link in links:
                with self.subTest(page=name, link=link):
                    path = urllib.parse.urljoin("/" + name, link)
                    status, content_type, cache, body = get(self.server, path)
                    self.assertEqual((status, cache), (200, "no-store"))
                    self.assertEqual(content_type, "text/html")
                    self.assertTrue(body.startswith(b"<!doctype html>"))

    def test_every_search_link_answers_200_with_html_through_the_server(self):
        search = json.loads((Path(self.site.name) / "search.json").read_text(encoding="utf-8"))
        self.assertGreaterEqual(len([entry for entry in search["accounts"] if ":" in entry["subject"]]), 12)
        for entry in search["accounts"] + search["entities"]:
            with self.subTest(url=entry["url"]):
                status, content_type, _, body = get(self.server, "/" + entry["url"])
                self.assertEqual((status, content_type), (200, "text/html"))
                self.assertTrue(body.startswith(b"<!doctype html>"))

    def test_feed_page_uses_the_follow_state_and_events_from_feed_state_dir(self):
        state = json.loads(feed.STATE_PATH.read_text(encoding="utf-8"))
        state.update(mode="follow", started_at="2026-11-03T12:00:00Z", last_height=987,
                     last_time="2026-11-03T12:05:00Z", lag_blocks=4,
                     updated_at=datetime.fromtimestamp(time.time(), UTC).strftime(feed._TIME))
        event = {"signal": "new receiver", "observed": {"block_height": 987}, "detail": "recorded event",
                 "receipt_id": "receipt", "tx_hash": "transaction"}
        with tempfile.TemporaryDirectory() as tmp:
            state_dir = Path(tmp)
            (state_dir / "state.json").write_text(json.dumps(state), encoding="utf-8")
            events_dir = state_dir / "events"
            events_dir.mkdir()
            (events_dir / ("ab" * 32 + ".json")).write_text(json.dumps(event), encoding="utf-8")
            with mock.patch.dict(os.environ, {"FEED_STATE_DIR": str(state_dir)}):
                status, content_type, cache, body = get(self.server, "/feed.html")
        page = body.decode("utf-8")
        self.assertEqual((status, content_type, cache), (200, "text/html", "no-store"))
        self.assertIn("following since 2026-11-03T12:00:00Z", page)
        self.assertIn("last block 987 at 2026-11-03T12:05:00Z", page)
        self.assertIn("lag 4 blocks", page)
        self.assertIn("recorded event", page)
        self.assertIn('<meta name="robots" content="noindex">', page)
        self.assertIn('rel="stylesheet" href="/style.css"', page)

    def test_feed_route_and_page_say_running_with_the_lag_or_stopped_with_the_last_update(self):
        now = int(time.time())
        for age, follower in ((10, "running"), (api.FOLLOWER_STOPPED_AFTER_S + 60, "stopped")):
            written = datetime.fromtimestamp(now - age, UTC).strftime(feed._TIME)
            with self.subTest(follower=follower), tempfile.TemporaryDirectory() as tmp:
                write_follow_state(Path(tmp), [], lag_blocks=4, last_time="2026-11-03T12:05:00Z", updated_at=written)
                with mock.patch.dict(os.environ, {"FEED_STATE_DIR": tmp}):
                    page = get(self.server, "/feed.html")[3].decode("utf-8")
                    body = json.loads(get(self.server, "/feed")[3])
                self.assertEqual((body["follower"], body["stats"]["updated_at"]), (follower, written))
                self.assertIn("<li>last block 1060 at 2026-11-03T12:05:00Z</li>", page)
                if follower == "running":
                    self.assertEqual(body["stats"]["lag_blocks"], 4)
                    self.assertIn("<li>lag 4 blocks</li>", page)
                    self.assertNotIn("follower stopped", page)
                else:
                    self.assertIsNone(body["stats"]["lag_blocks"])
                    self.assertIn(f"<li>follower stopped: its last update was at {written}, lag unknown</li>", page)
                    self.assertNotIn("lag 4 blocks", page)

    def test_a_follow_state_without_updated_at_is_judged_by_last_time_else_by_started_at(self):
        now = int(time.time())
        fresh = datetime.fromtimestamp(now - 10, UTC).strftime(feed._TIME)
        stale = datetime.fromtimestamp(now - api.FOLLOWER_STOPPED_AFTER_S - 60, UTC).strftime(feed._TIME)
        for last_time, started_at, follower in ((fresh, stale, "running"), (stale, fresh, "stopped"),
                                                (None, fresh, "running"), (None, stale, "stopped")):
            shown = last_time or started_at
            with self.subTest(last_time=last_time, started_at=started_at), tempfile.TemporaryDirectory() as tmp:
                write_follow_state(Path(tmp), [], lag_blocks=4, last_time=last_time, started_at=started_at)
                with mock.patch.dict(os.environ, {"FEED_STATE_DIR": tmp}):
                    page = get(self.server, "/feed.html")[3].decode("utf-8")
                    body = json.loads(get(self.server, "/feed")[3])
                self.assertNotIn("updated_at", body["stats"])
                self.assertEqual((body["follower"], body["stats"]["lag_blocks"]),
                                 ("running", 4) if follower == "running" else ("stopped", None))
                self.assertEqual(f"<li>follower stopped: its last update was at {shown}, lag unknown</li>" in page,
                                 follower == "stopped")
                self.assertEqual("<li>lag 4 blocks</li>" in page, follower == "running")

    def test_a_null_or_malformed_updated_at_is_read_as_missing_by_the_feed_route_and_page(self):
        for updated_at in (None, "2026-11-05 05:03:12"):
            with self.subTest(updated_at=updated_at), tempfile.TemporaryDirectory() as tmp:
                write_follow_state(Path(tmp), [], lag_blocks=4, updated_at=updated_at)
                with mock.patch.dict(os.environ, {"FEED_STATE_DIR": tmp}):
                    status, _, _, page = get(self.server, "/feed.html")
                    route_status, _, _, body = get(self.server, "/feed")
                self.assertEqual((status, route_status), (200, 200))
                self.assertEqual(json.loads(body)["follower"], "running")
                self.assertIn("<li>lag 4 blocks</li>", page.decode("utf-8"))

    def test_feed_page_falls_back_to_static_without_a_follow_state(self):
        static_page = (Path(self.site.name) / "feed.html").read_bytes()
        recorded_state = json.dumps(json.loads(feed.STATE_PATH.read_text(encoding="utf-8")))
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertEqual(get(self.server, "/feed.html")[3], static_page)
        for state_text in (None, "not json", recorded_state):
            with self.subTest(state=state_text), tempfile.TemporaryDirectory() as tmp:
                if state_text is not None:
                    (Path(tmp) / "state.json").write_text(state_text, encoding="utf-8")
                with mock.patch.dict(os.environ, {"FEED_STATE_DIR": tmp}):
                    status, content_type, cache, body = get(self.server, "/feed.html")
                self.assertEqual((status, content_type, cache, body),
                                 (200, "text/html", "no-store", static_page))

    def test_feed_page_shows_the_50_events_the_feed_route_answers_and_the_total(self):
        events = [{"signal": "new receiver", "observed": {"block_height": height}, "detail": f"event {height}",
                   "receipt_id": f"receipt {height}", "tx_hash": f"transaction {height}"}
                  for height in range(1001, 1061)]
        with tempfile.TemporaryDirectory() as tmp:
            write_follow_state(Path(tmp), [json.dumps(event) for event in events])
            with mock.patch.dict(os.environ, {"FEED_STATE_DIR": tmp}):
                status, content_type, _, body = get(self.server, "/feed.html")
                route_events = json.loads(get(self.server, "/feed")[3])["events"]
        page = body.decode("utf-8")
        self.assertEqual((status, content_type), (200, "text/html"))
        self.assertEqual(page.count('<div class="card">'), 50)
        self.assertIn("<p>60 events in total; the 50 most recent by block height are shown.</p>", page)
        heights = [event["observed"]["block_height"] for event in route_events]
        self.assertEqual(heights, list(range(1011, 1061)))
        self.assertEqual([int(height) for height in re.findall(r"at block (\d+)</p>", page)], heights)
        self.assertNotIn("<p>event 1010</p>", page)
        recorded_details = page.split('<details class="feed-recorded-details">', 1)[1].split('</details>', 1)[0]
        self.assertNotIn("could not be read", recorded_details)

    def test_feed_page_skips_and_counts_a_torn_a_non_json_and_a_keyless_event_file(self):
        good = json.dumps({"signal": "new receiver", "observed": {"block_height": 1001}, "detail": "readable event",
                           "receipt_id": "receipt", "tx_hash": "transaction"})
        cases = {"torn": good[:30], "not json": "not json", "missing keys": json.dumps({"signal": "new receiver"})}
        for case, text in cases.items():
            with self.subTest(case=case), tempfile.TemporaryDirectory() as tmp:
                write_follow_state(Path(tmp), [good, text])
                with mock.patch.dict(os.environ, {"FEED_STATE_DIR": tmp}):
                    status, content_type, _, body = get(self.server, "/feed.html")
                page = body.decode("utf-8")
                self.assertEqual((status, content_type), (200, "text/html"))
                self.assertIn("following since 2026-11-03T12:00:00Z", page)
                self.assertIn("<p>1 event in total.</p>\n<p>1 event file could not be read.</p>", page)
                self.assertEqual(page.count('<div class="card">'), 1)
                self.assertIn("readable event", page)
                self.assertNotIn(hashlib.sha256(text.encode("utf-8")).hexdigest(), page)
                self.assertNotIn(html.escape(text), page)

    def test_feed_page_and_feed_route_answer_over_every_kind_of_bad_event_file(self):
        good = json.dumps({"signal": "labelled_account_large_transfer", "observed": {"block_height": 1002},
                           "detail": "transfer event", "receipt_id": "receipt", "transaction_id": "transaction",
                           "usd_amount": "1500", "direction": "outbound", "other_account_id": "other.near",
                           "asset_id": "nep141:wrap.near", "block_height": 1002})
        bad = ["", '{"signal": "new rec', "not json", "[1, 2]", json.dumps({"signal": "new receiver"}),
               json.dumps({"observed": "1001"}), json.dumps({"observed": {"block_height": "1001"}})]
        with tempfile.TemporaryDirectory() as tmp:
            write_follow_state(Path(tmp), [good] + bad)
            with mock.patch.dict(os.environ, {"FEED_STATE_DIR": tmp}):
                status, content_type, _, body = get(self.server, "/feed.html")
                route_status, _, _, route_body = get(self.server, "/feed")
        page = body.decode("utf-8")
        self.assertEqual((status, content_type), (200, "text/html"))
        self.assertIn("<p>1 event in total.</p>\n<p>7 event files could not be read.</p>", page)
        self.assertIn("transfer event", page)
        self.assertIn("transaction unknown", page)
        self.assertEqual(route_status, 200)
        self.assertEqual([event["detail"] for event in json.loads(route_body)["events"]], ["transfer event"])

    def test_feed_page_with_only_unreadable_event_files_does_not_say_no_signal_fired(self):
        with tempfile.TemporaryDirectory() as tmp:
            write_follow_state(Path(tmp), ["not json"])
            with mock.patch.dict(os.environ, {"FEED_STATE_DIR": tmp}):
                status, _, _, body = get(self.server, "/feed.html")
        page = body.decode("utf-8")
        self.assertEqual(status, 200)
        self.assertIn("<p>0 events in total.</p>\n<p>1 event file could not be read.</p>", page)
        self.assertNotIn("No signal fired", page)

    def test_the_incident_json_route_still_answers(self):
        status, content_type, _, body = get(self.server, "/incidents/rhea")
        self.assertEqual((status, content_type), (200, JSON_TYPE))
        self.assertEqual(json.loads(body), json.loads((ROOT / "intel" / "incidents" / "rhea.json").read_bytes()))

    def test_an_unknown_path_answers_the_html_404_and_an_api_miss_stays_json(self):
        page = (Path(self.site.name) / "404.html").read_bytes()

        self.assertIn(b'href="/index.html"', page)
        for path in ("/nonexistent", "/accounts/x", "/incidents", "/incidents/none-such.html", "/labels/x.html",
                     "/flows/missing.html", "/../README.md"):
            with self.subTest(path=path):
                self.assertEqual(get(self.server, path), (404, "text/html", "no-store", page))
        missing = "0" * 64
        for path, expected in (("/incidents/none-such", (404, {"error": "no incident 'none-such'"})),
                               ("/flow/nobody-here.near", (403, {"error": "subject carries no incident label"})),
                               (f"/evidence/{missing}", (404, {"error": f"no evidence document {missing}"}))):
            with self.subTest(path=path):
                status, content_type, cache, body = get(self.server, path)
                self.assertEqual((content_type, cache), (JSON_TYPE, "no-store"))
                self.assertEqual((status, json.loads(body)), expected)

    def test_a_miss_one_level_down_links_back_to_pages_that_answer_200(self):
        status, content_type, _, body = get(self.server, "/incidents/nope.html")
        self.assertEqual((status, content_type), (404, "text/html"))
        links = LINK.findall(body.decode("utf-8"))
        self.assertIn("/index.html", links)
        for link in links:
            with self.subTest(link=link):
                if link.startswith("#"):
                    self.assertEqual(link, "#main-content")
                    self.assertIn(b'id="main-content"', body)
                    continue
                self.assertTrue(link.startswith("/") and not link.startswith("//"))
                status, content_type, _, page = get(self.server, link)
                if link == "/style.css":
                    self.assertEqual((status, content_type), (200, "text/css"))
                else:
                    self.assertEqual((status, content_type), (200, "text/html"))
                    self.assertTrue(page.startswith(b"<!doctype html>"))


if __name__ == "__main__":
    unittest.main()
