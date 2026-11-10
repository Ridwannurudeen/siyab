import hashlib
import http.client
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from intel import api


class GraphCacheTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.site = tempfile.TemporaryDirectory()
        root = Path(cls.site.name)
        cls.payload = b'{"kind":"flow","records":[]}'
        cls.digest = hashlib.sha256(cls.payload).hexdigest()
        cls.path = f"/graph-data/{cls.digest}.json"
        (root / "graph-data").mkdir()
        (root / cls.path.lstrip("/")).write_bytes(cls.payload)
        (root / "graph-data" / ("0" * 64 + ".json")).write_bytes(cls.payload)
        (root / "assets").mkdir()
        (root / "assets" / (cls.digest + ".js")).write_bytes(cls.payload)
        (root / "assets" / ("0" * 64 + ".js")).write_bytes(cls.payload)
        (root / "search.json").write_bytes(cls.payload)
        (root / "entities").mkdir()
        (root / "entities" / f"{cls.digest}.table.json").write_bytes(cls.payload)
        (root / "index.html").write_text("<h1>Recorded investigation</h1>", encoding="utf-8")
        (root / "404.html").write_text("<h1>Not found</h1>", encoding="utf-8")
        cls.live = mock.Mock(max_in_flight=2, request_timeout_s=5)
        cls.live.handle.return_value = (200, {"status": "ok", "signing": "never"}, {})
        cls.server = api.make_server("127.0.0.1", 0, site=root, live_routes=cls.live)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join()
        cls.site.cleanup()

    def request(self, path):
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_address[1], timeout=10)
        self.addCleanup(connection.close)
        connection.request("GET", path)
        reply = connection.getresponse()
        return reply.status, reply.headers.get_all("Cache-Control"), reply.read()

    def test_matching_content_addressed_graph_is_immutable(self):
        for path in (self.path, self.path + "?v=1"):
            with self.subTest(path=path):
                self.assertEqual(self.request(path),
                                 (200, ["public, max-age=31536000, immutable"], self.payload))

    def test_hash_mismatch_is_never_immutable(self):
        self.assertEqual(self.request("/graph-data/" + "0" * 64 + ".json"),
                         (200, ["no-store"], self.payload))

    def test_script_cache_requires_canonical_path_and_matching_bytes(self):
        path = f"/assets/{self.digest}.js"
        self.assertEqual(self.request(path),
                         (200, ["public, max-age=31536000, immutable"], self.payload))
        for uncached in ("/assets/" + "0" * 64 + ".js", "/assets/../" + path.lstrip("/")):
            with self.subTest(path=uncached):
                self.assertEqual(self.request(uncached), (200, ["no-store"], self.payload))

    def test_missing_graph_and_other_static_documents_remain_uncached(self):
        for path, status in (("/graph-data/" + "1" * 64 + ".json", 404),
                             ("/search.json", 200), ("/index.html", 200),
                             (f"/entities/{self.digest}.table.json", 200)):
            with self.subTest(path=path):
                actual_status, cache, _ = self.request(path)
                self.assertEqual((actual_status, cache), (status, ["no-store"]))

    def test_live_and_read_only_api_responses_remain_uncached(self):
        for path in ("/live/health", "/health", "/labels/wrap.near"):
            with self.subTest(path=path):
                status, cache, _ = self.request(path)
                self.assertEqual((status, cache), (200, ["no-store"]))

    def test_noncanonical_graph_path_does_not_gain_immutable_caching(self):
        status, cache, _ = self.request("/graph-data/../" + self.path.lstrip("/"))
        self.assertEqual((status, cache), (200, ["no-store"]))


if __name__ == "__main__":
    unittest.main()
