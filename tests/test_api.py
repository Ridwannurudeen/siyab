import html
import http.client
import json
import re
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from intel import api, issuers, labels, render

ROOT = Path(__file__).resolve().parent.parent
EVIDENCE = min((ROOT / "fixtures" / "evidence").glob("*.json"))
TREASURY = "0x2CfF890f0378a11913B6129B2E97417a2c302680"
JSON_TYPE = "application/json; charset=utf-8"
LINK = re.compile(r'href="([^"]*)"')


def get(server, path):
    """(status, content type, cache control, body) of one GET to a server on loopback."""
    connection = http.client.HTTPConnection("127.0.0.1", server.server_address[1], timeout=10)
    try:
        connection.request("GET", path)
        reply = connection.getresponse()
        return reply.status, reply.getheader("Content-Type"), reply.getheader("Cache-Control"), reply.read()
    finally:
        connection.close()


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
                mock.patch.object(api, "FEED_STATE", Path(tmp) / "state.json"), \
                mock.patch.object(api, "FEED_EVENTS", Path(tmp) / "events"):
            self.assertEqual(self.get_json("/feed"), (200, {"stats": None, "events": []}))

    def test_feed_returns_the_state_and_the_last_50_events_in_block_order(self):
        with tempfile.TemporaryDirectory() as tmp:
            events = Path(tmp) / "events"
            events.mkdir()
            (Path(tmp) / "state.json").write_text('{"blocks": 300}', encoding="utf-8")
            for number in range(60):
                document = {"observed": {"block_height": 1000 - number}, "n": number}
                (events / f"{number:064x}.json").write_text(json.dumps(document), encoding="utf-8")
            with mock.patch.object(api, "FEED_STATE", Path(tmp) / "state.json"), \
                    mock.patch.object(api, "FEED_EVENTS", events):
                status, body = self.get_json("/feed")
        self.assertEqual(status, 200)
        self.assertEqual(body["stats"], {"blocks": 300})
        self.assertEqual([event["n"] for event in body["events"]], list(range(49, -1, -1)))

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

    def test_every_index_link_answers_200_with_html_through_the_server(self):
        index = (Path(self.site.name) / "index.html").read_text(encoding="utf-8")
        links = [html.unescape(link) for link in LINK.findall(index) if not link.startswith("https://")]
        self.assertIn("incidents/rhea.html", links)
        for link in links:
            with self.subTest(link=link):
                status, content_type, cache, body = get(self.server, "/" + link)
                self.assertEqual((status, content_type, cache), (200, "text/html", "no-store"))
                self.assertTrue(body.startswith(b"<!doctype html>"))

    def test_the_incident_json_route_still_answers(self):
        status, content_type, _, body = get(self.server, "/incidents/rhea")
        self.assertEqual((status, content_type), (200, JSON_TYPE))
        self.assertEqual(json.loads(body), json.loads((ROOT / "intel" / "incidents" / "rhea.json").read_bytes()))

    def test_an_unknown_path_answers_the_html_404_and_an_api_miss_stays_json(self):
        page = (Path(self.site.name) / "404.html").read_bytes()

        self.assertIn(b'href="index.html"', page)
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


if __name__ == "__main__":
    unittest.main()
