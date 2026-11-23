import base64
import hashlib
import http.client
import json
import socket
import sqlite3
import tempfile
import threading
import unittest
from email.message import Message
from pathlib import Path
from unittest import mock

from intel import api
from intel.private_workspace import VAULT_BODY_BYTES
from intel.workspace import SQLiteWorkspaceStore
from intel.workspace_api import MAX_BODY_BYTES, WorkspaceAPI


class NearBodyLimitTest(unittest.TestCase):
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

    def request(self, body, cookie=None, csrf=None):
        connection = http.client.HTTPConnection(*self.server.server_address, timeout=3)
        self.addCleanup(connection.close)
        headers = {"Origin": self.routes.origin, "Content-Type": "application/json"}
        if cookie is not None:
            headers["Cookie"] = cookie
        if csrf is not None:
            headers["X-CSRF-Token"] = csrf
        connection.request("POST", "/workspace-api/near/vault", body, headers)
        response = connection.getresponse()
        return response.status, json.loads(response.read())

    def declared_only(self, length, cookies=()):
        connection = socket.create_connection(self.server.server_address, timeout=3)
        self.addCleanup(connection.close)
        headers = ("POST /workspace-api/near/vault HTTP/1.1\r\nHost: localhost\r\n"
                   f"Content-Length: {length}\r\n")
        headers += "".join("Cookie: " + cookie + "\r\n" for cookie in cookies)
        with mock.patch.object(api, "WORKSPACE_BODY_TIMEOUT_S", 0.1):
            connection.sendall((headers + "\r\n").encode())
            response = http.client.HTTPResponse(connection)
            response.begin()
            result = response.status, json.loads(response.read())
        return result

    def test_large_anonymous_body_is_refused_without_reading(self):
        self.assertEqual(self.declared_only(MAX_BODY_BYTES + 1)[0], 413)

    def test_other_malformed_and_challenge_cookies_do_not_raise_limit(self):
        name = self.routes.private.cookie_name
        for cookies in (("other=" + "a" * 43,),
                        (name + "=short",),
                        (name + '=\"unterminated',),
                        ("siyab-local-near-challenge=" + "a" * 43,),
                        (name + "=" + "a" * 43 + "; " + name + "=" + "b" * 43,),
                        (name + "=" + "a" * 43, name + "=" + "b" * 43)):
            with self.subTest(cookies=cookies):
                self.assertEqual(self.declared_only(MAX_BODY_BYTES + 1, cookies)[0], 413)

    def test_exact_anonymous_limit_reaches_authentication(self):
        self.assertEqual(self.request(b"{}" + b" " * (MAX_BODY_BYTES - 2))[0], 401)

    def test_direct_api_also_limits_anonymous_vault_body(self):
        headers = Message()
        headers["Origin"] = self.routes.origin
        headers["Content-Type"] = "application/json"
        result = self.routes.handle("POST", "/workspace-api/near/vault", "203.0.113.1", headers,
                                    b"{}" + b" " * (MAX_BODY_BYTES - 1))
        self.assertEqual(result[0], 413)

    def test_shaped_but_invalid_session_cannot_write(self):
        cookie = self.routes.private.cookie_name + "=" + "a" * 43
        self.assertEqual(self.request(b"{}" + b" " * MAX_BODY_BYTES, cookie)[0], 413)
        self.assertEqual(self.request(b"{}", cookie)[0], 401)
        with self.store._connection() as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM near_vaults").fetchone()[0], 0)

    def test_fake_well_formed_session_is_refused_without_reading(self):
        cookie = self.routes.private.cookie_name + "=" + "a" * 43
        self.assertEqual(self.declared_only(MAX_BODY_BYTES + 1, (cookie,))[0], 413)

    def test_expired_and_other_origin_sessions_do_not_raise_limit(self):
        token = "a" * 43
        cookie = self.routes.private.cookie_name + "=" + token
        now = self.store._now()
        for origin, expires in ((self.routes.origin, now), ("https://other.example", now + 3600000)):
            with self.subTest(origin=origin, expires=expires):
                with self.store._connection(write=True) as connection:
                    connection.execute("INSERT OR REPLACE INTO near_sessions VALUES (?,?,?,?,?,?,?)",
                                       (hashlib.sha256(token.encode()).hexdigest(), origin,
                                        "alice.near", "ed25519:" + "1" * 32, "b" * 43, now, expires))
                self.assertEqual(self.declared_only(MAX_BODY_BYTES + 1, (cookie,))[0], 413)

    def test_unavailable_session_storage_does_not_raise_limit(self):
        cookie = self.routes.private.cookie_name + "=" + "a" * 43
        for error in (sqlite3.OperationalError("unavailable"), OSError("unavailable")):
            with self.subTest(error=type(error).__name__):
                with mock.patch.object(self.routes.private, "_session", side_effect=error) as session:
                    self.assertEqual(self.declared_only(MAX_BODY_BYTES + 1, (cookie,))[0], 413)
                session.assert_called_once_with("a" * 43)

    def test_valid_session_retains_full_ciphertext_limit(self):
        token, csrf = "a" * 43, "b" * 43
        key = "ed25519:" + "1" * 32
        now = self.store._now()
        with self.store._connection(write=True) as connection:
            connection.execute("INSERT INTO near_sessions VALUES (?,?,?,?,?,?,?)",
                               (hashlib.sha256(token.encode()).hexdigest(), self.routes.origin,
                                "alice.near", key, csrf, now, now + 3600000))
        vault = {"version": 1, "algorithm": "AES-256-GCM", "derivation": "NEP413-HKDF-SHA256-v1",
                 "origin": self.routes.origin, "account_id": "alice.near", "public_key": key,
                 "vault_id": "workspace:1", "iv": base64.b64encode(bytes(12)).decode(),
                 "ciphertext": base64.b64encode(bytes(524304)).decode()}
        body = json.dumps({"revision": 0, "vault": vault}).encode()
        body += b" " * (VAULT_BODY_BYTES - len(body))
        result = self.request(body, self.routes.private.cookie_name + "=" + token, csrf)
        self.assertEqual(result[0], 200, result[1])
        self.assertEqual(result[1]["revision"], 1)

    def test_session_cookie_cannot_exceed_absolute_limit(self):
        cookie = self.routes.private.cookie_name + "=" + "a" * 43
        self.assertEqual(self.declared_only(VAULT_BODY_BYTES + 1, (cookie,))[0], 413)
