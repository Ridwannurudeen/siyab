import http.client
import json
import socket
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from intel import api
from intel.workspace import SQLiteWorkspaceStore
from intel.workspace_api import WorkspaceAPI


class WorkspaceHTTPTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.store = SQLiteWorkspaceStore(Path(self.directory.name) / "workspace.sqlite3")
        self.routes = WorkspaceAPI(self.store, origin="http://127.0.0.1", secure_cookie=False)
        self.server = api.make_server("127.0.0.1", 0, workspace_routes=self.routes)
        self.addCleanup(self.server.server_close)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.stop)

    def stop(self):
        self.server.shutdown()
        self.thread.join()

    def request(self, method, path, data=None, headers=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_address[1], timeout=10)
        self.addCleanup(connection.close)
        body = None if data is None else json.dumps(data).encode()
        values = {"Origin": "http://127.0.0.1", "Content-Type": "application/json", **(headers or {})}
        connection.request(method, path, body, values)
        response = connection.getresponse()
        return response.status, response.headers, response.read()

    def raw(self, request):
        connection = socket.create_connection(self.server.server_address, timeout=3)
        self.addCleanup(connection.close)
        connection.sendall(request)
        response = http.client.HTTPResponse(connection)
        response.begin()
        return response.status, response.read()

    def test_private_cookie_roundtrip_no_cache_and_head(self):
        status, headers, body = self.request("POST", "/workspace-api/auth/register", {"username": "alice", "password": "test-password-123"})
        self.assertEqual(status, 201, body)
        cookie = headers["Set-Cookie"].split(";", 1)[0]
        self.assertEqual(headers.get_all("Cache-Control"), ["no-store"])
        self.assertEqual(headers["Vary"], "Cookie")
        private = self.request("GET", "/workspace-api/session", headers={"Cookie": cookie})
        self.assertTrue(json.loads(private[2])["authenticated"])
        anonymous = self.request("GET", "/workspace-api/session")
        self.assertFalse(json.loads(anonymous[2])["authenticated"])
        status, headers, body = self.request("HEAD", "/workspace-api/session", headers={"Cookie": cookie})
        self.assertEqual((status, body), (200, b""))
        self.assertGreater(int(headers["Content-Length"]), 0)
        self.assertEqual(self.request("POST", "/live/verdict", {})[0], 501)

    def test_ambiguous_oversized_or_unsupported_framing_is_rejected_before_body(self):
        for extra, expected in ((b"", 411), (b"Content-Length: 40000\r\n", 413),
                                (b"Content-Length: 2\r\nContent-Length: 2\r\n", 400),
                                (b"Transfer-Encoding: chunked\r\n", 400),
                                (b"Content-Length: -1\r\n", 400)):
            with self.subTest(extra=extra):
                status, _ = self.raw(b"POST /workspace-api/auth/login HTTP/1.1\r\nHost: localhost\r\n" + extra + b"\r\n")
                self.assertEqual(status, expected)
        status, _ = self.raw(b"GET /workspace-api/session HTTP/1.1\r\nHost: localhost\r\nContent-Length: 5\r\n\r\n")
        self.assertEqual(status, 400)

    def test_partial_body_has_an_overall_deadline(self):
        with mock.patch.object(api, "WORKSPACE_BODY_TIMEOUT_S", 0.1):
            status, _ = self.raw(b"POST /workspace-api/auth/login HTTP/1.1\r\nHost: localhost\r\nContent-Length: 100\r\n\r\nx")
        self.assertEqual(status, 408)

    def test_server_without_workspace_support_preserves_read_only_mode(self):
        server = api.make_server("127.0.0.1", 0)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            connection = http.client.HTTPConnection("127.0.0.1", server.server_address[1], timeout=5)
            try:
                connection.request("POST", "/workspace-api/auth/register", b"{}")
                response = connection.getresponse()
                self.assertEqual(response.status, 501)
                response.read()
            finally:
                connection.close()
        finally:
            server.shutdown()
            server.server_close()
            thread.join()
