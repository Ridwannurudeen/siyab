import hashlib
import http.client
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from intel import api


class ObservationHTTPTest(unittest.TestCase):
    def test_exact_public_observation_bytes_head_and_cache(self):
        body = '{"observed":"NEAR · source","records":[]}\n'.encode()
        identifier = hashlib.sha256(body).hexdigest()
        routes = mock.Mock(request_timeout_s=25, max_in_flight=8)
        routes.handle.return_value = (200, body, {"Content-Type": api.JSON_TYPE,
                                                "Cache-Control": "public, max-age=31536000, immutable"})
        server = api.make_server("127.0.0.1", 0, live_routes=routes)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            for kind in ("data", "events"):
                for method in ("GET", "HEAD"):
                    connection = http.client.HTTPConnection(*server.server_address, timeout=5)
                    try:
                        connection.request(method, f"/live/account-{kind}/{identifier}.json")
                        response = connection.getresponse()
                        self.assertEqual(response.status, 200)
                        self.assertEqual(response.read(), body if method == "GET" else b"")
                        self.assertEqual(int(response.getheader("Content-Length")), len(body))
                        self.assertEqual(response.getheader("Cache-Control"), "public, max-age=31536000, immutable" if kind == "data" else "no-store")
                        self.assertEqual(len(response.headers.get_all("Cache-Control")), 1)
                        self.assertEqual(response.headers.get_all("Content-Type"), [api.JSON_TYPE])
                    finally:
                        connection.close()
            routes.handle.return_value = (404, {"error": "not_found"}, {})
            connection = http.client.HTTPConnection(*server.server_address, timeout=5)
            try:
                connection.request("GET", f"/live/account-data/{identifier}.json")
                response = connection.getresponse()
                response.read()
                self.assertEqual(response.getheader("Cache-Control"), "no-store")
            finally:
                connection.close()
        finally:
            server.shutdown()
            server.server_close()
            thread.join()

    def test_serve_mounts_history_and_aggregate_status_outside_source(self):
        with tempfile.TemporaryDirectory() as directory:
            server = mock.Mock()
            server.server_address = ("127.0.0.1", 8801)
            with mock.patch.object(api, "make_server", return_value=server) as make:
                self.assertEqual(api.serve("127.0.0.1", 8801, live=True, state_dir=directory,
                                          workspace_origin="http://127.0.0.1:8801"), 0)
            arguments = make.call_args.kwargs
            self.assertEqual(arguments["live_routes"].history_store.path, Path(directory) / "history.sqlite3")
            self.assertIs(arguments["workspace_routes"].history_store, arguments["live_routes"].history_store)
            status = arguments["workspace_routes"].monitor_status()
            self.assertTrue(status["configured"])
            self.assertFalse(status["account_polling"]["running"])
            self.assertNotIn("watched_accounts", status["account_polling"])
