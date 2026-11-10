import json
import tempfile
import unittest
from email.message import Message
from pathlib import Path

from intel.workspace import SQLiteWorkspaceStore
from intel.workspace_api import PREFIX, WorkspaceAPI


class WorkspaceAPITest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.store = SQLiteWorkspaceStore(Path(self.directory.name) / "workspace.sqlite3")
        self.api = WorkspaceAPI(self.store)

    def request(self, path, *, method="GET", body=None, cookie=None, csrf=None, origin="https://siyab.xyz", extra=(), peer="203.0.113.1"):
        headers = Message()
        if origin is not None:
            headers["Origin"] = origin
        headers["Content-Type"] = "application/json"
        if cookie:
            headers["Cookie"] = cookie
        if csrf:
            headers["X-CSRF-Token"] = csrf
        for name, value in extra:
            headers[name] = value
        encoded = body if isinstance(body, bytes) else json.dumps(body or {}).encode()
        return self.api.handle(method, PREFIX + path, peer, headers, encoded)

    def register(self, username="alice"):
        status, body, headers = self.request("/auth/register", method="POST", body={"username": username, "password": "test-password-123"})
        self.assertEqual(status, 201, body)
        self.assertNotIn("session_token", body)
        return body, headers["Set-Cookie"].split(";", 1)[0]

    def test_unrelated_cookie_does_not_break_anonymous_session(self):
        status, body, _ = self.request("/session", cookie="other_application=value")
        self.assertEqual(status, 200, body)
        self.assertFalse(body["authenticated"])

    def test_cookie_security_csrf_origin_and_actor_isolation(self):
        body, cookie = self.register()
        response = self.request("/session", cookie=cookie)
        team = response[1]["teams"][0]["id"]
        route = f"/teams/{team}/cases"
        values = {"title": "Case", "notes": "literal <script>", "url": "/investigate.html?account=wrap.near"}
        self.assertEqual(self.request(route, method="POST", body=values, cookie=cookie)[0], 403)
        self.assertEqual(self.request(route, method="POST", body=values, cookie=cookie, csrf=body["csrf_token"], origin="https://other.example")[0], 403)
        created = self.request(route, method="POST", body=values, cookie=cookie, csrf=body["csrf_token"])
        self.assertEqual(created[0], 201, created[1])
        self.assertEqual(self.request(route, cookie=cookie)[1]["cases"][0]["notes"], "literal <script>")
        _, other_cookie = self.register("bobby")
        self.assertEqual(self.request(route, cookie=other_cookie)[0], 403)
        self.assertEqual(self.request(route, method="POST", body={**values, "user_id": body["user"]["id"]}, cookie=cookie, csrf=body["csrf_token"])[0], 400)
        self.assertEqual(self.request("/session", cookie=cookie, extra=(("Cookie", cookie),))[0], 400)
        _, _, headers = self.request("/auth/login", method="POST", body={"username": "alice", "password": "test-password-123"})
        for flag in ("__Host-siyab-session=", "Secure", "HttpOnly", "SameSite=Strict", "Path=/"):
            self.assertIn(flag, headers["Set-Cookie"])

    def test_login_rotation_logout_and_query_credentials_denied(self):
        body, cookie = self.register()
        logged = self.request("/auth/login", method="POST", body={"username": "alice", "password": "test-password-123"}, cookie=cookie)
        new_cookie = logged[2]["Set-Cookie"].split(";", 1)[0]
        self.assertFalse(self.request("/session", cookie=cookie)[1]["authenticated"])
        self.assertEqual(self.request("/auth/logout", method="POST", cookie=new_cookie, csrf=logged[1]["csrf_token"])[0], 200)
        self.assertFalse(self.request("/session", cookie=new_cookie)[1]["authenticated"])
        self.assertEqual(self.request("/auth/login?username=alice", method="POST", body={})[0], 400)
        self.assertEqual(self.request("/auth/register", method="POST", body={}, origin=None)[0], 403)
        self.assertEqual(self.request("/auth/register", method="POST", body={}, extra=(("Origin", "https://siyab.xyz"),))[0], 403)

    def test_malformed_and_duplicate_json_never_reaches_registration(self):
        for body in (b"[]", b'{"username":"a","username":"b"}', b"\xff", b"{" * 32000, b"[" * 2000 + b"]" * 2000):
            with self.subTest(body=body[:20]):
                self.assertEqual(self.request("/auth/register", method="POST", body=body)[0], 400)
        self.assertEqual(self.request("/auth/register", method="POST", body=b"x" * 32769)[0], 413)

    def test_auth_throttle_does_not_consume_public_intelligence_routes(self):
        for _ in range(10):
            self.request("/auth/login", method="POST", body={"username": "missing", "password": "test-password-123"})
        self.assertEqual(self.request("/auth/login", method="POST", body={"username": "missing", "password": "test-password-123"})[0], 429)
        self.assertIsNone(self.api.handle("GET", "/live/health", "203.0.113.1", Message()))

    def test_invalid_unicode_identity_is_a_client_error(self):
        for username in ("\ud800", "x" * 1000, 7):
            with self.subTest(username=repr(username)[:40]):
                self.assertEqual(self.request("/auth/login", method="POST", body={"username": username, "password": "test-password-123"})[0], 400)

    def test_one_client_cannot_lock_another_out_of_login_or_recovery(self):
        registered, _ = self.register()
        for _ in range(10):
            self.request("/auth/login", method="POST", body={"username": "alice", "password": "wrong-password-123"}, peer="203.0.113.66")
        result = self.request("/auth/login", method="POST", body={"username": "alice", "password": "test-password-123"}, peer="203.0.113.2")
        self.assertEqual(result[0], 200, result[1])
        recovered = self.request("/auth/recover", method="POST", body={"username": "alice", "recovery_code": registered["recovery_code"], "new_password": "new-password-123"}, peer="203.0.113.3")
        self.assertEqual(recovered[0], 200, recovered[1])

    def test_explicit_import_is_atomic_and_repeated_requests_do_not_duplicate_notes(self):
        session, cookie = self.register()
        team = self.request("/session", cookie=cookie)[1]["teams"][0]["id"]
        route = f"/teams/{team}/import"
        values = {"cases": [{"title": "Imported", "notes": "Private note", "url": "/investigate.html?account=wrap.near"}], "watchlist": ["wrap.near"]}
        first = self.request(route, method="POST", body=values, cookie=cookie, csrf=session["csrf_token"])
        self.assertEqual(first[0], 200, first[1])
        self.assertEqual(first[1], {"created_cases": 1, "added_watches": 1, "skipped_cases": 0})
        second = self.request(route, method="POST", body=values, cookie=cookie, csrf=session["csrf_token"])
        self.assertEqual(second[1], {"created_cases": 0, "added_watches": 0, "skipped_cases": 1})
