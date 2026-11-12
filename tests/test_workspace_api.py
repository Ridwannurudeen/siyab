import hashlib
import json
import tempfile
import unittest
from email.message import Message
from pathlib import Path
from unittest.mock import patch

from intel import history
from intel.workspace import SQLiteWorkspaceStore
from intel.workspace_api import PREFIX, WorkspaceAPI


class WorkspaceAPITest(unittest.TestCase):
    def setUp(self):
        registration = patch.dict("os.environ", {"SIYAB_REGISTRATIONS_PER_HOUR": "10", "SIYAB_REGISTRATIONS_PER_DAY": "40"})
        registration.start()
        self.addCleanup(registration.stop)
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

    def test_registration_defaults_open_and_closed_creates_nothing(self):
        registered, cookie = self.register()
        with patch.dict("os.environ", {"SIYAB_REGISTRATION": "closed"}):
            self.api = WorkspaceAPI(self.store)
        with self.store._connection() as connection:
            before = {table: connection.execute("SELECT COUNT(*) FROM " + table).fetchone()[0]
                      for table in ("users", "sessions", "teams", "memberships")}
        denied = self.request("/auth/register", method="POST", body={"username": "bobby", "password": "test-password-123"})
        self.assertEqual((denied[0], denied[1]["error"]), (403, "registration_closed"))
        with self.store._connection() as connection:
            after = {table: connection.execute("SELECT COUNT(*) FROM " + table).fetchone()[0]
                     for table in before}
        self.assertEqual(after, before)
        self.assertFalse(self.request("/session")[1]["registration_open"])
        self.assertFalse(self.request("/session", cookie=cookie)[1]["registration_open"])
        self.assertTrue(registered["registration_open"])
        login = self.request("/auth/login", method="POST", body={"username": "alice", "password": "test-password-123"})
        self.assertEqual(login[0], 200, login[1])
        self.assertFalse(login[1]["registration_open"])
        recovery = self.request("/auth/recover", method="POST", body={"username": "alice", "recovery_code": registered["recovery_code"], "new_password": "new-password-123"})
        self.assertEqual(recovery[0], 200, recovery[1])
        self.assertFalse(recovery[1]["registration_open"])

    def test_global_hour_registration_limit_survives_store_restart(self):
        now = [2 * 86400 + 61]
        self.store = SQLiteWorkspaceStore(self.store.path, clock=lambda: now[0])
        self.api = WorkspaceAPI(self.store)
        for index in range(10):
            result = self.request("/auth/register", method="POST", peer=f"203.0.113.{index + 1}",
                                  body={"username": f"person{index}", "password": "test-password-123"})
            self.assertEqual(result[0], 201, result[1])
        for restarted in (False, True):
            if restarted:
                self.store = SQLiteWorkspaceStore(self.store.path, clock=lambda: now[0])
                self.api = WorkspaceAPI(self.store)
            result = self.request("/auth/register", method="POST", peer="203.0.113.100",
                                  body={"username": "hourblocked", "password": "test-password-123"})
            self.assertEqual((result[0], result[1].get("error")), (429, "registration_limit"))
            self.assertEqual(result[2]["Retry-After"], "3539")
        with self.store._connection() as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM users").fetchone()[0], 10)
        now[0] += 3600
        self.assertEqual(self.request("/auth/register", method="POST", body={"username": "next_hour", "password": "test-password-123"})[0], 201)

    def test_global_day_registration_limit_survives_store_restart(self):
        now = [2 * 86400 + 61]
        self.store = SQLiteWorkspaceStore(self.store.path, clock=lambda: now[0])
        self.api = WorkspaceAPI(self.store)
        for index in range(40):
            now[0] = 2 * 86400 + (index // 10) * 3600 + 61
            result = self.request("/auth/register", method="POST", peer=f"203.0.113.{index + 1}",
                                  body={"username": f"person{index}", "password": "test-password-123"})
            self.assertEqual(result[0], 201, result[1])
        now[0] = 2 * 86400 + 4 * 3600 + 61
        for restarted in (False, True):
            if restarted:
                self.store = SQLiteWorkspaceStore(self.store.path, clock=lambda: now[0])
                self.api = WorkspaceAPI(self.store)
            result = self.request("/auth/register", method="POST", peer="203.0.113.100",
                                  body={"username": "dayblocked", "password": "test-password-123"})
            self.assertEqual((result[0], result[1].get("error")), (429, "registration_limit"))
            self.assertEqual(result[2]["Retry-After"], "71939")
        with self.store._connection() as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM users").fetchone()[0], 40)
        now[0] = 3 * 86400
        self.assertEqual(self.request("/auth/register", method="POST", body={"username": "next_day", "password": "test-password-123"})[0], 201)

    def test_access_code_registration_skips_closed_and_address_windows(self):
        with patch.dict("os.environ", {"SIYAB_REGISTRATION": "closed"}):
            self.api = WorkspaceAPI(self.store)
        self.api._rates[("register", "203.0.113.1")] = (self.api.clock(), 5)
        self.api._rates[("register_day", "203.0.113.1")] = (self.api.clock(), 10)
        values = {"username": "coded", "password": "test-password-123", "access_code": "a" * 43}
        with patch.object(self.store, "register", return_value={"session_token": "a" * 43, "expires_at": 100000}) as register:
            result = self.request("/auth/register", method="POST", body=values)
            self.assertEqual(result[0], 201, result[1])
            register.assert_called_once_with("coded", "test-password-123", access_code="a" * 43)
            for _ in range(9):
                self.assertEqual(self.request("/auth/register", method="POST", body=values)[0], 201)
            self.assertEqual(self.request("/auth/register", method="POST", body=values)[0], 429)
            self.assertEqual(register.call_count, 10)

    def test_access_code_registration_uses_reserved_tier_with_closed_and_full_windows(self):
        with patch.dict("os.environ", {"SIYAB_REGISTRATIONS_PER_HOUR": "1", "SIYAB_REGISTRATIONS_PER_DAY": "1"}):
            self.store = SQLiteWorkspaceStore(self.store.path)
        self.store.register("public", "test-password-123")
        self.api = WorkspaceAPI(self.store)
        denied = self.request("/auth/register", method="POST", body={"username": "denied", "password": "test-password-123"})
        self.assertEqual((denied[0], denied[1]["error"]), (429, "registration_limit"))
        code = self.store.create_access_code(2, 1)["code"]
        with patch.dict("os.environ", {"SIYAB_REGISTRATION": "closed"}):
            self.api = WorkspaceAPI(self.store)
        result = self.request("/auth/register", method="POST", body={"username": "coded", "password": "test-password-123", "access_code": code})
        self.assertEqual(result[0], 201, result[1])
        with self.store._connection() as connection:
            self.assertIsNotNone(connection.execute("SELECT 1 FROM reserved_users WHERE user_id=?", (result[1]["user"]["id"],)).fetchone())
        self.assertEqual(self.store.list_access_codes()[0]["uses_left"], 1)
        invalid = self.request("/auth/register", method="POST", body={"username": "invalid", "password": "test-password-123", "access_code": "a" * 43})
        self.assertEqual((invalid[0], invalid[1]["error"]), (403, "invalid_access_code"))

    def test_access_code_errors_do_not_disclose_usernames_when_registration_is_closed(self):
        self.register()
        code = self.store.create_access_code(2, 1)["code"]
        with patch.dict("os.environ", {"SIYAB_REGISTRATION": "closed"}):
            self.api = WorkspaceAPI(self.store)
        for username in ("alice", "unknown"):
            with self.subTest(username=username):
                result = self.request("/auth/register", method="POST", body={
                    "username": username, "password": "test-password-123", "access_code": "a" * 43})
                self.assertEqual((result[0], result[1]["error"]), (403, "invalid_access_code"))
        result = self.request("/auth/register", method="POST", body={
            "username": "alice", "password": "test-password-123", "access_code": code})
        self.assertEqual((result[0], result[1]["error"]), (409, "username_taken"))
        self.assertEqual(self.store.list_access_codes()[0]["uses_left"], 2)

    def test_invalid_access_code_type_attempts_are_throttled(self):
        values = {"username": "coded", "password": "test-password-123", "access_code": 7}
        for _ in range(10):
            self.assertEqual(self.request("/auth/register", method="POST", body=values)[0], 400)
        self.assertEqual(self.request("/auth/register", method="POST", body=values)[0], 429)

    def test_access_code_registration_rejects_non_string_fields(self):
        for value in (None, 7, [], {}):
            with self.subTest(value=value):
                result = self.request("/auth/register", method="POST", body={"username": "coded", "password": "test-password-123", "access_code": value})
                self.assertEqual(result[0], 400, result[1])

    def test_registration_address_retry_after_is_remaining_window(self):
        now = [0]
        self.api = WorkspaceAPI(self.store, clock=lambda: now[0])
        values = {"username": "missing", "password": "test-password-123"}
        with patch.object(self.store, "register", return_value={"session_token": "a" * 43, "expires_at": 100000}):
            for _ in range(5):
                self.assertEqual(self.request("/auth/register", method="POST", body=values)[0], 201)
            now[0] = 61
            self.assertEqual(self.request("/auth/register", method="POST", body=values)[2]["Retry-After"], "3539")

    def test_auth_address_retry_after_is_remaining_window(self):
        now = [0]
        self.api = WorkspaceAPI(self.store, clock=lambda: now[0])
        values = {"username": "missing", "password": "test-password-123"}
        for _ in range(10):
            self.assertEqual(self.request("/auth/login", method="POST", body=values)[0], 401)
        now[0] = 11
        self.assertEqual(self.request("/auth/login", method="POST", body=values)[2]["Retry-After"], "49")

    def test_forwarded_client_uses_last_header_line(self):
        forwarded = (("X-Forwarded-For", "203.0.113.17"), ("X-Forwarded-For", "untrusted, 203.0.113.18"))
        self.assertEqual(self.request("/session", peer="127.0.0.1", extra=forwarded)[0], 200)
        self.assertIn(("requests", "203.0.113.18"), self.api._rates)
        self.assertNotIn(("requests", "203.0.113.17"), self.api._rates)
        empty = (("X-Forwarded-For", "203.0.113.17"), ("X-Forwarded-For", ""))
        self.assertEqual(self.request("/session", peer="127.0.0.1", extra=empty)[0], 400)

    def test_mapped_ipv6_clients_use_distinct_ipv4_buckets(self):
        for address in ("203.0.113.17", "203.0.113.18"):
            self.assertEqual(self.request("/session", peer="::ffff:" + address)[0], 200)
            self.assertIn(("requests", address), self.api._rates)
        self.assertEqual(len(self.api._rates), 2)

    def test_empty_registration_setting_defaults_open(self):
        with patch.dict("os.environ", {"SIYAB_REGISTRATION": ""}):
            self.api = WorkspaceAPI(self.store)
        self.assertTrue(self.request("/session")[1]["registration_open"])
        self.register()

    def test_rate_limit_replies_survive_bucket_eviction(self):
        session, cookie = self.register()
        client = "203.0.113.1"
        identity = hashlib.sha256(b"alice").hexdigest()
        cases = (
            (("requests", client), 120, 60, "/session", "GET", {}),
            (("auth", client), 10, 60, "/auth/login", "POST", {"username": "alice", "password": "test-password-123"}),
            (("identity", "login", client, identity), 10, 60, "/auth/login", "POST", {"username": "alice", "password": "test-password-123"}),
            (("register", client), 5, 3600, "/auth/register", "POST", {"username": "alice", "password": "test-password-123"}),
            (("register_day", client), 10, 86400, "/auth/register", "POST", {"username": "alice", "password": "test-password-123"}),
            (("recover", client), 5, 3600, "/auth/recover", "POST", {"username": "alice", "recovery_code": session["recovery_code"], "new_password": "new-password-123"}),
            (("password", session["user"]["id"]), 5, 60, "/auth/password", "POST", {"current_password": "test-password-123", "new_password": "new-password-123"}),
        )
        for bucket, limit, seconds, path, method, body in cases:
            with self.subTest(bucket=bucket):
                self.api = WorkspaceAPI(self.store, clock=lambda: 11)
                self.api._rates[bucket] = (0, limit)
                original = self.api._allowed
                def evict(key, limit, seconds):
                    result = original(key, limit, seconds)
                    if key == bucket:
                        for index in range(4096):
                            original(("requests", f"eviction-{index}"), 120, 60)
                    return result
                with patch.object(self.api, "_allowed", side_effect=evict):
                    result = self.request(path, method=method, body=body, cookie=cookie, csrf=session["csrf_token"])
                self.assertNotIn(bucket, self.api._rates)
                self.assertEqual((result[0], result[1]["error"], result[2]["Retry-After"]),
                                 (429, "rate_limited", str(seconds - 11)))

    def test_registration_setting_rejects_unknown_value(self):
        with patch.dict("os.environ", {"SIYAB_REGISTRATION": "unknown"}):
            with self.assertRaises(ValueError):
                WorkspaceAPI(self.store)

    def test_registration_address_hour_and_day_limits(self):
        now = [0]
        self.api = WorkspaceAPI(self.store, clock=lambda: now[0])
        values = {"username": "missing", "password": "test-password-123"}
        with patch.object(self.store, "register", return_value={"session_token": "a" * 43, "expires_at": 100000}) as register:
            for _ in range(5):
                self.assertEqual(self.request("/auth/register", method="POST", body=values)[0], 201)
            limited = self.request("/auth/register", method="POST", body=values)
            self.assertEqual((limited[0], limited[2]["Retry-After"]), (429, "3600"))
            now[0] = 3600
            for _ in range(5):
                self.assertEqual(self.request("/auth/register", method="POST", body=values)[0], 201)
            now[0] = 7200
            limited = self.request("/auth/register", method="POST", body=values)
            self.assertEqual((limited[0], limited[2]["Retry-After"]), (429, "79200"))
            self.assertEqual(register.call_count, 10)
            now[0] = 86400
            self.assertEqual(self.request("/auth/register", method="POST", body=values)[0], 201)

    def test_forwarded_client_uses_rightmost_address_only_from_loopback(self):
        for peer in ("127.0.0.1", "::1"):
            with self.subTest(peer=peer):
                self.api = WorkspaceAPI(self.store)
                forwarded = (("X-Forwarded-For", "untrusted, 203.0.113.17"),)
                self.assertEqual(self.request("/session", peer=peer, extra=forwarded)[0], 200)
                self.assertIn(("requests", "203.0.113.17"), self.api._rates)
                self.assertEqual(self.request("/session", peer=peer, extra=(("X-Forwarded-For", ""),))[0], 400)
                self.assertEqual(self.request("/session", peer=peer, extra=(("X-Forwarded-For", "203.0.113.17, invalid"),))[0], 400)
        self.assertEqual(self.request("/session", peer="203.0.113.2", extra=(("X-Forwarded-For", "invalid"),))[0], 200)
        self.assertIn(("requests", "203.0.113.2"), self.api._rates)

    def test_ipv6_clients_share_a_rate_bucket_by_64_prefix(self):
        values = {"username": "missing", "password": "test-password-123"}
        for suffix in range(1, 11):
            reply = self.request("/auth/login", method="POST", body=values, peer=f"2001:db8:1:2::{suffix}")
            self.assertEqual(reply[0], 401, reply[1])
        self.assertEqual(self.request("/auth/login", method="POST", body=values, peer="2001:db8:1:2::11")[0], 429)
        self.assertEqual(self.request("/auth/login", method="POST", body=values, peer="2001:db8:1:3::1")[0], 401)
        forwarded = (("X-Forwarded-For", "192.0.2.1, 2001:db8:1:2::12"),)
        self.assertEqual(self.request("/auth/login", method="POST", body=values, peer="127.0.0.1", extra=forwarded)[0], 429)

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

    def test_team_events_are_served_only_to_a_team_whose_inbox_holds_them(self):
        archive = history.AccountHistory(Path(self.directory.name) / "history.sqlite3")
        self.api = WorkspaceAPI(self.store, history_store=archive)
        alice, alice_cookie = self.register()
        bobby, bobby_cookie = self.register("bobby")
        team = self.store.list_teams(alice["user"]["id"])[0]["id"]
        other = self.store.list_teams(bobby["user"]["id"])[0]["id"]
        self.store.add_watch(alice["user"]["id"], team, "alice.near")
        item = {"from": "alice.near", "to": "bob.near", "asset_id": "native:near", "amount": "1",
                "block_timestamp": "4000000000000000000", "receipt_id": "receipt1", "transfer_index": 0,
                "transaction_hash": "tx1", "block_height": "12"}
        identifier, body = history.AccountHistory._event(item)
        with archive._connection(write=True) as connection:
            connection.execute("INSERT INTO event_outbox VALUES (?,?,?,?,1)", (identifier, body, 1, 0))
        self.store.ingest_event(identifier, json.loads(body), notify=True)
        status, served, headers = self.request(f"/teams/{team}/events/{identifier}", cookie=alice_cookie)
        self.assertEqual(status, 200, served)
        self.assertEqual(served, body)
        self.assertEqual(hashlib.sha256(served).hexdigest(), identifier)
        self.assertNotIn("Cache-Control", headers)
        unknown = "b" * 64
        missing = self.request(f"/teams/{team}/events/{unknown}", cookie=alice_cookie)
        self.assertEqual(missing[0], 404)
        for path, cookie, expected in ((f"/teams/{other}/events/", bobby_cookie, 404), (f"/teams/{team}/events/", bobby_cookie, 403),
                                       (f"/teams/{team}/events/", None, 401), (f"/teams/{other}/events/", None, 401)):
            with self.subTest(path=path, cookie=cookie):
                real = self.request(path + identifier, cookie=cookie)
                self.assertEqual(real[0], expected)
                self.assertEqual(real, self.request(path + unknown, cookie=cookie))
        self.assertEqual(self.request(f"/teams/{other}/events/{identifier}", cookie=bobby_cookie), missing)
        nested = b"[" * 100000 + b"]" * 100000
        deep = hashlib.sha256(nested).hexdigest()
        with archive._connection(write=True) as connection:
            connection.execute("INSERT INTO event_outbox VALUES (?,?,?,?,1)", (deep, nested, 1, 0))
            connection.execute("UPDATE event_outbox SET body=? WHERE id=?", (body.decode(), identifier))
        self.store.ingest_event(deep, {**json.loads(body), "receipt_id": "receipt2"}, notify=True)
        for damaged in (identifier, deep):
            with self.subTest(damaged=damaged):
                status, answer, _ = self.request(f"/teams/{team}/events/{damaged}", cookie=alice_cookie)
                self.assertEqual((status, answer["error"]), (503, "read_failed"))
                self.assertEqual(self.request(f"/teams/{other}/events/{damaged}", cookie=bobby_cookie), missing)
        self.api = WorkspaceAPI(self.store)
        status, answer, _ = self.request(f"/teams/{team}/events/{identifier}", cookie=alice_cookie)
        self.assertEqual((status, answer["error"]), (503, "events_unavailable"))
        self.assertEqual(self.request(f"/teams/{other}/events/{identifier}", cookie=bobby_cookie), missing)
