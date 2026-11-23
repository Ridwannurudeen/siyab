import base64
import concurrent.futures
import http.client
import json
import shutil
import sqlite3
import subprocess
import tempfile
import threading
import unittest
from contextlib import closing
from email.message import Message
from pathlib import Path
from unittest.mock import Mock

from intel import api
from intel.near_auth import NearAuth
from intel.private_workspace import PrivateWorkspace, VAULT_BODY_BYTES
from intel.workspace import SQLiteWorkspaceStore
from intel.workspace_api import WorkspaceAPI


class PrivateWorkspaceTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.now = [1000]
        self.store = SQLiteWorkspaceStore(Path(self.directory.name) / "workspace.sqlite3", clock=lambda: self.now[0])
        self.auth = Mock()
        self.auth.verify.return_value = {"account_id": "alice.near", "public_key": "ed25519:" + "1" * 32}
        self.private = PrivateWorkspace(self.store, "https://siyab.xyz", auth=self.auth)
        self.routes = WorkspaceAPI(self.store)
        self.routes.private = self.private

    def request(self, path, *, method="GET", body=None, cookie=None, csrf=None, origin="https://siyab.xyz"):
        headers = Message()
        headers["Origin"] = origin
        headers["Content-Type"] = "application/json"
        if cookie:
            headers["Cookie"] = cookie
        if csrf:
            headers["X-CSRF-Token"] = csrf
        return self.routes.handle(method, "/workspace-api/near/" + path, "203.0.113.1", headers,
                                  json.dumps(body or {}).encode())

    def login(self, account="alice.near", public_key=None):
        self.auth.issue.return_value = {"challenge_id": "a" * 43, "account_id": account}
        self.auth.verify.return_value = {"account_id": account, "public_key": public_key or "ed25519:" + "1" * 32}
        issued = self.request("challenge", method="POST", body={"account_id": account})
        self.assertEqual(issued[0], 201, issued[1])
        binding = issued[2]["Set-Cookie"].split(";", 1)[0]
        verified = self.request("verify", method="POST", body={"challenge_id": "a" * 43}, cookie=binding)
        self.assertEqual(verified[0], 200, verified[1])
        cookie = verified[2]["Set-Cookie"].split(";", 1)[0]
        self.assertNotEqual(cookie, binding)
        return cookie, verified[1]["csrf_token"]

    def envelope(self, revision=1, account="alice.near", public_key=None, size=32):
        return {"version": 1, "algorithm": "AES-256-GCM", "derivation": "NEP413-HKDF-SHA256-v1",
                "origin": "https://siyab.xyz", "account_id": account,
                "public_key": public_key or "ed25519:" + "1" * 32, "vault_id": "workspace:" + str(revision),
                "iv": base64.b64encode(bytes(range(12))).decode(),
                "ciphertext": base64.b64encode(b"\x83" * size).decode()}

    def save(self, cookie, csrf, *, revision=0, envelope=None):
        return self.request("vault", method="POST", cookie=cookie, csrf=csrf,
                            body={"revision": revision, "vault": envelope or self.envelope(revision + 1)})

    def test_sign_in_does_not_upload_or_read_browser_data(self):
        cookie, csrf = self.login()
        result = self.request("session", cookie=cookie)
        self.assertTrue(result[1]["authenticated"])
        self.assertEqual(result[1]["account_id"], "alice.near")
        self.assertNotIn("session_token", result[1])
        self.assertEqual(self.request("vault", cookie=cookie)[1]["revision"], 0)
        self.assertEqual(self.request("vault", cookie=cookie)[1]["vault"], None)
        self.assertEqual(self.save(cookie, csrf)[0], 200)

    def test_ciphertext_only_in_database_and_backup(self):
        cookie, csrf = self.login()
        vault = self.envelope()
        result = self.save(cookie, csrf, envelope=vault)
        self.assertEqual(result[0], 200, result[1])
        restored = self.request("vault", cookie=cookie)
        self.assertEqual(restored[1]["vault"], vault)
        with self.store._connection() as connection:
            dumped = "\n".join(connection.iterdump())
            for table in ("users", "cases", "watchlist", "audit"):
                self.assertEqual(connection.execute("SELECT COUNT(*) FROM " + table).fetchone()[0], 0)
        self.assertIn(vault["ciphertext"], dumped)
        for text in ("private notes", "secret-title", "watched.near"):
            self.assertNotIn(text, dumped)
        self.assertNotIn(cookie.split("=", 1)[1], dumped)
        backup = Path(self.directory.name) / "backup.sqlite3"
        self.store.backup(backup)
        with closing(sqlite3.connect(backup)) as connection:
            stored = connection.execute("SELECT envelope_json FROM near_vaults").fetchone()[0]
        self.assertEqual(json.loads(stored), vault)

    @unittest.skipUnless(shutil.which("node"), "Node is unavailable")
    def test_real_browser_ciphertext_reaches_storage_without_private_content(self):
        result = subprocess.run([shutil.which("node"), "tests/near_workspace_runtime.cjs", "emit-envelope"],
                                cwd=Path(__file__).resolve().parent.parent, capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        envelope = json.loads(result.stdout)
        cookie, csrf = self.login(public_key=envelope["public_key"])
        self.assertEqual(self.save(cookie, csrf, envelope=envelope)[0], 200)
        self.assertEqual(self.request("vault", cookie=cookie)[1]["vault"], envelope)
        with self.store._connection() as connection:
            dumped = "\n".join(connection.iterdump())
        for value in ("Confidential title", "Confidential notes", "case-confidential", "confidential-watch-20261121.near"):
            self.assertNotIn(value, dumped)
            self.assertNotIn(value, result.stdout)

    def test_concurrent_saves_cannot_overwrite_the_same_revision(self):
        cookie, csrf = self.login()
        start = threading.Barrier(2)

        def save():
            start.wait(timeout=5)
            return self.save(cookie, csrf)[0]

        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: save(), range(2)))
        self.assertEqual(sorted(results), [200, 409])
        self.assertEqual(self.request("vault", cookie=cookie)[1]["revision"], 1)

    def test_session_is_bound_to_origin(self):
        cookie, _ = self.login()
        other = PrivateWorkspace(self.store, "https://other.example", auth=self.auth)
        headers = Message()
        headers["Cookie"] = cookie
        result = other.handle(["session"], "GET", None, headers)
        self.assertFalse(result[1]["authenticated"])

    def test_anonymous_session_read_does_not_clear_an_existing_cookie(self):
        result = self.request("session")
        self.assertEqual(result[0], 200)
        self.assertFalse(result[1]["authenticated"])
        self.assertNotIn("Set-Cookie", result[2])

    def test_challenge_cookie_survives_session_read_in_another_tab(self):
        self.auth.issue.return_value = {"challenge_id": "a" * 43}
        issued = self.request("challenge", method="POST", body={"account_id": "alice.near"})
        binding = issued[2]["Set-Cookie"].split(";", 1)[0]
        self.assertTrue(binding.startswith("__Host-siyab-near-challenge="))
        self.assertIn("Max-Age=300", issued[2]["Set-Cookie"])
        for flag in ("HttpOnly", "SameSite=Strict", "Secure", "Path=/"):
            self.assertIn(flag, issued[2]["Set-Cookie"])
        other_tab = self.request("session", cookie=binding)
        self.assertFalse(other_tab[1]["authenticated"])
        self.assertNotIn("Set-Cookie", other_tab[2])
        self.assertEqual(self.request("vault", cookie=binding)[0], 401)
        verified = self.request("verify", method="POST", body={"challenge_id": "a" * 43}, cookie=binding)
        self.assertEqual(verified[0], 200)
        self.auth.verify.assert_called_once_with({"challenge_id": "a" * 43}, binding.split("=", 1)[1])
        self.auth.issue.assert_called_once_with("alice.near", binding.split("=", 1)[1],
                                                client_address="203.0.113.1")

    def test_challenge_route_passes_transport_address_to_real_auth(self):
        self.private.auth = NearAuth(self.store, "https://siyab.xyz", clock=lambda: self.now[0])
        result = self.request("challenge", method="POST", body={"account_id": "alice.near"})
        self.assertEqual(result[0], 201, result[1])
        with self.store._connection() as connection:
            row = connection.execute("SELECT client_bucket FROM near_auth_challenges").fetchone()
        self.assertEqual(row[0], "203.0.113.1/32")

    def test_challenge_does_not_replace_an_existing_session_cookie(self):
        cookie, _ = self.login()
        issued = self.request("challenge", method="POST", body={"account_id": "alice.near"}, cookie=cookie)
        binding = issued[2]["Set-Cookie"].split(";", 1)[0]
        self.assertNotEqual(binding.split("=", 1)[0], cookie.split("=", 1)[0])
        self.assertTrue(self.request("session", cookie=cookie + "; " + binding)[1]["authenticated"])
        self.assertEqual(self.request("verify", method="POST", body={"challenge_id": "a" * 43}, cookie=cookie)[0], 401)
        self.assertEqual(self.request("verify", method="POST", body={"challenge_id": "a" * 43},
                                      cookie=cookie + "; " + binding)[0], 200)
        self.assertFalse(self.request("session", cookie=cookie)[1]["authenticated"])

    def test_duplicate_challenge_cookie_is_rejected(self):
        binding = "__Host-siyab-near-challenge=" + "a" * 43
        self.assertEqual(self.request("verify", method="POST", cookie=binding + "; " + binding)[0], 400)

    def test_reset_allows_a_changed_key_to_start_a_new_vault(self):
        cookie, csrf = self.login()
        self.assertEqual(self.save(cookie, csrf)[0], 200)
        changed_key = "ed25519:" + "2" * 32
        changed, changed_csrf = self.login(public_key=changed_key)
        self.assertEqual(self.save(changed, changed_csrf, revision=1,
                                  envelope=self.envelope(2, public_key=changed_key))[0], 409)
        reset = self.request("vault/reset", method="POST", cookie=changed, csrf=changed_csrf,
                             body={"confirmation": "DELETE alice.near"})
        self.assertEqual(reset[0], 200, reset[1])
        self.assertEqual(reset[1], {"reset": True, "revision": 2})
        self.assertNotIn("Set-Cookie", reset[2])
        self.assertEqual(self.request("vault", cookie=changed)[1]["revision"], 2)
        self.assertIsNone(self.request("vault", cookie=changed)[1]["vault"])
        self.assertEqual(self.save(changed, changed_csrf, revision=2,
                                  envelope=self.envelope(3, public_key=changed_key))[0], 200)
        self.assertEqual(self.save(changed, changed_csrf, revision=3,
                                  envelope=self.envelope(4, public_key=changed_key))[0], 200)

    def test_reset_keeps_revisions_monotonic_and_refuses_stale_device_saves(self):
        first, first_csrf = self.login()
        second, second_csrf = self.login()
        self.assertEqual(self.save(first, first_csrf)[0], 200)
        for revision in (2, 3):
            reset = self.request("vault/reset", method="POST", cookie=second, csrf=second_csrf,
                                 body={"confirmation": "DELETE alice.near"})
            self.assertEqual(reset[1], {"reset": True, "revision": revision})
            saved = self.request("vault", cookie=first)[1]
            self.assertEqual(saved["revision"], revision)
            self.assertIsNone(saved["vault"])
            self.assertEqual(self.save(first, first_csrf, revision=revision - 1)[0], 409)
        self.assertEqual(self.save(second, second_csrf, revision=3)[1]["revision"], 4)
        self.assertEqual(self.request("vault", cookie=first)[1]["revision"], 4)

    def test_reset_before_first_save_leaves_a_tombstone(self):
        cookie, csrf = self.login()
        reset = self.request("vault/reset", method="POST", cookie=cookie, csrf=csrf,
                             body={"confirmation": "DELETE alice.near"})
        self.assertEqual(reset[1], {"reset": True, "revision": 1})
        self.assertEqual(self.save(cookie, csrf)[0], 409)
        self.assertEqual(self.save(cookie, csrf, revision=1)[1]["revision"], 2)

    def test_reset_and_save_are_serialized_without_resurrecting_stale_data(self):
        cookie, csrf = self.login()
        self.assertEqual(self.save(cookie, csrf)[0], 200)
        start = threading.Barrier(2)

        def reset():
            start.wait(timeout=5)
            return self.request("vault/reset", method="POST", cookie=cookie, csrf=csrf,
                                body={"confirmation": "DELETE alice.near"})

        def save():
            start.wait(timeout=5)
            return self.save(cookie, csrf, revision=1)

        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            resetting, saving = pool.submit(reset), pool.submit(save)
            reset_result, save_result = resetting.result(), saving.result()
        self.assertEqual(reset_result[0], 200)
        self.assertIn(save_result[0], (200, 409))
        expected = 3 if save_result[0] == 200 else 2
        self.assertEqual(reset_result[1]["revision"], expected)
        final = self.request("vault", cookie=cookie)[1]
        self.assertIsNone(final["vault"])
        self.assertEqual(final["revision"], expected)

    def test_reset_refuses_revision_overflow_without_removing_ciphertext(self):
        cookie, csrf = self.login()
        self.assertEqual(self.save(cookie, csrf)[0], 200)
        with self.store._connection(write=True) as connection:
            connection.execute("UPDATE near_vaults SET revision=?", (2**53 - 1,))
        before = self.request("vault", cookie=cookie)[1]
        result = self.request("vault/reset", method="POST", cookie=cookie, csrf=csrf,
                              body={"confirmation": "DELETE alice.near"})
        self.assertEqual((result[0], result[1]["error"]), (409, "invalid_revision"))
        self.assertEqual(self.request("vault", cookie=cookie)[1], before)

    def test_reset_requires_authentication_exact_origin_csrf_and_typed_account(self):
        cookie, csrf = self.login()
        self.assertEqual(self.save(cookie, csrf)[0], 200)
        for supplied_cookie, supplied_csrf, origin, body, expected in (
                (None, csrf, "https://siyab.xyz", {"confirmation": "DELETE alice.near"}, 401),
                (cookie, None, "https://siyab.xyz", {"confirmation": "DELETE alice.near"}, 403),
                (cookie, "a" * 43, "https://siyab.xyz", {"confirmation": "DELETE alice.near"}, 403),
                (cookie, csrf, "https://evil.example", {"confirmation": "DELETE alice.near"}, 403),
                (cookie, csrf, "https://siyab.xyz/", {"confirmation": "DELETE alice.near"}, 403),
                (cookie, csrf, "", {"confirmation": "DELETE alice.near"}, 403),
                (cookie, csrf, "https://siyab.xyz", {}, 400),
                (cookie, csrf, "https://siyab.xyz", {"confirmation": "DELETE bob.near"}, 400),
                (cookie, csrf, "https://siyab.xyz", {"confirmation": "DELETE alice.near "}, 400),
                (cookie, csrf, "https://siyab.xyz", {"confirmation": True}, 400),
                (cookie, csrf, "https://siyab.xyz", {"confirmation": "DELETE alice.near", "account_id": "bob.near"}, 400)):
            with self.subTest(body=body, origin=origin, expected=expected):
                result = self.request("vault/reset", method="POST", cookie=supplied_cookie, csrf=supplied_csrf,
                                      origin=origin, body=body)
                self.assertEqual(result[0], expected, result[1])
                self.assertIsNotNone(self.request("vault", cookie=cookie)[1]["vault"])
        self.assertEqual(self.request("vault/reset", cookie=cookie)[0], 404)

    def test_reset_requires_a_session_less_than_five_minutes_old(self):
        cookie, csrf = self.login()
        self.assertEqual(self.save(cookie, csrf)[0], 200)
        self.now[0] += 300
        result = self.request("vault/reset", method="POST", cookie=cookie, csrf=csrf,
                              body={"confirmation": "DELETE alice.near"})
        self.assertEqual((result[0], result[1]["error"]), (403, "near_reauthentication_required"))
        self.assertIsNotNone(self.request("vault", cookie=cookie)[1]["vault"])
        cookie, csrf = self.login()
        self.now[0] += 299.999
        self.assertEqual(self.request("vault/reset", method="POST", cookie=cookie, csrf=csrf,
                                      body={"confirmation": "DELETE alice.near"})[0], 200)

    def test_reset_refuses_clock_rollback_and_expired_session(self):
        cookie, csrf = self.login()
        self.assertEqual(self.save(cookie, csrf)[0], 200)
        self.now[0] -= 1
        result = self.request("vault/reset", method="POST", cookie=cookie, csrf=csrf,
                              body={"confirmation": "DELETE alice.near"})
        self.assertEqual(result[0], 403)
        self.now[0] += 3602
        self.assertEqual(self.request("vault/reset", method="POST", cookie=cookie, csrf=csrf,
                                      body={"confirmation": "DELETE alice.near"})[0], 401)

    def test_reset_deletes_only_the_session_account_and_origin_vault(self):
        cookie, csrf = self.login()
        self.assertEqual(self.save(cookie, csrf)[0], 200)
        bob, bob_csrf = self.login("bob.near")
        self.assertEqual(self.save(bob, bob_csrf, envelope=self.envelope(account="bob.near"))[0], 200)
        with self.store._connection(write=True) as connection:
            connection.execute("INSERT INTO near_vaults SELECT ?,account_id,public_key,revision,envelope_json,updated_ms "
                               "FROM near_vaults WHERE origin=? AND account_id=?",
                               ("https://other.example", "https://siyab.xyz", "alice.near"))
        self.assertEqual(self.request("vault/reset", method="POST", cookie=cookie, csrf=csrf,
                                      body={"confirmation": "DELETE alice.near"})[0], 200)
        self.assertIsNotNone(self.request("vault", cookie=bob)[1]["vault"])
        self.assertTrue(self.request("session", cookie=cookie)[1]["authenticated"])
        with self.store._connection() as connection:
            remaining = connection.execute("SELECT origin,account_id FROM near_vaults ORDER BY origin,account_id").fetchall()
            self.assertEqual([tuple(row) for row in remaining],
                             [("https://other.example", "alice.near"), ("https://siyab.xyz", "alice.near"),
                              ("https://siyab.xyz", "bob.near")])
            tombstone = connection.execute("SELECT revision,envelope_json FROM near_vaults WHERE origin=? AND account_id=?",
                                           ("https://siyab.xyz", "alice.near")).fetchone()
            self.assertEqual(tuple(tombstone), (2, "null"))

    def test_plaintext_fields_and_unversioned_payload_are_rejected(self):
        cookie, csrf = self.login()
        for vault in ({"watchlist": ["watched.near"], "cases": [{"notes": "private notes"}]},
                      {**self.envelope(), "notes": "private notes"},
                      {**self.envelope(), "version": 2},
                      {**self.envelope(), "version": True},
                      {**self.envelope(), "ciphertext": "private notes"}):
            with self.subTest(vault=vault):
                result = self.save(cookie, csrf, envelope=vault)
                self.assertEqual(result[0], 400, result[1])
        self.assertIsNone(self.request("vault", cookie=cookie)[1]["vault"])

    def test_authentication_origin_and_csrf_are_required(self):
        self.assertEqual(self.request("vault")[0], 401)
        self.assertEqual(self.request("verify", method="POST")[0], 401)
        cookie, csrf = self.login()
        self.assertEqual(self.save(cookie, None)[0], 403)
        self.assertEqual(self.save(cookie, "a" * 43)[0], 403)
        self.assertEqual(self.request("vault", method="POST", cookie=cookie, csrf=csrf,
                                      origin="https://evil.example", body={})[0], 403)
        self.assertEqual(self.request("session?x=1", cookie=cookie)[0], 400)
        self.assertEqual(self.request("logout", method="GET", cookie=cookie)[0], 404)

    def test_expiry_logout_and_session_cookie_flags(self):
        cookie, csrf = self.login()
        self.now[0] += 3601
        self.assertFalse(self.request("session", cookie=cookie)[1]["authenticated"])
        self.assertEqual(self.request("vault", cookie=cookie)[0], 401)
        cookie, csrf = self.login()
        result = self.request("logout", method="POST", cookie=cookie, csrf=csrf)
        self.assertEqual(result[0], 200)
        self.assertIn("HttpOnly", result[2]["Set-Cookie"])
        self.assertIn("SameSite=Strict", result[2]["Set-Cookie"])
        self.assertIn("Secure", result[2]["Set-Cookie"])
        self.assertEqual(self.request("vault", cookie=cookie)[0], 401)

    def test_account_isolation_key_binding_and_optimistic_concurrency(self):
        cookie, csrf = self.login()
        self.assertEqual(self.save(cookie, csrf)[0], 200)
        self.assertEqual(self.save(cookie, csrf)[0], 409)
        for field, value in (("origin", "https://evil.example"), ("account_id", "bob.near"),
                             ("public_key", "ed25519:" + "2" * 32), ("vault_id", "workspace:1")):
            vault = {**self.envelope(2), field: value}
            self.assertEqual(self.save(cookie, csrf, revision=1, envelope=vault)[0], 400)
        bob, bob_csrf = self.login("bob.near")
        self.assertIsNone(self.request("vault", cookie=bob)[1]["vault"])
        self.assertEqual(self.save(bob, bob_csrf, envelope=self.envelope(account="bob.near"))[0], 200)
        changed, changed_csrf = self.login(public_key="ed25519:" + "2" * 32)
        self.assertEqual(self.save(changed, changed_csrf, revision=1,
                                  envelope=self.envelope(2, public_key="ed25519:" + "2" * 32))[0], 409)

    def test_ciphertext_bounds_and_canonical_base64(self):
        cookie, csrf = self.login()
        for field, value in (("iv", base64.b64encode(bytes(13)).decode()), ("iv", "!!!!"),
                             ("ciphertext", base64.b64encode(bytes(15)).decode()),
                             ("ciphertext", base64.b64encode(bytes(524305)).decode()),
                             ("iv", base64.b64encode(bytes(12)).decode() + "\n")):
            result = self.save(cookie, csrf, envelope={**self.envelope(), field: value})
            self.assertEqual(result[0], 400, result[1])
        self.assertEqual(self.save(cookie, csrf, envelope=self.envelope(size=65536))[0], 200)

    def test_duplicate_cookie_is_rejected(self):
        cookie, _ = self.login()
        for duplicate in (cookie, cookie.replace("=", " ="), cookie.replace("=", "\t=")):
            with self.subTest(duplicate=duplicate):
                self.assertEqual(self.request("session", cookie=cookie + "; " + duplicate)[0], 400)

    def test_challenge_and_verify_have_separate_rate_limits(self):
        self.auth.issue.return_value = {"challenge_id": "a" * 43}
        for _ in range(5):
            self.assertEqual(self.request("challenge", method="POST", body={"account_id": "alice.near"})[0], 201)
        self.assertEqual(self.request("challenge", method="POST", body={"account_id": "alice.near"})[0], 429)
        self.assertEqual(self.auth.issue.call_count, 5)

    def test_http_path_accepts_bounded_ciphertext_and_keeps_other_body_limit(self):
        cookie, csrf = self.login()
        server = api.make_server("127.0.0.1", 0, workspace_routes=self.routes)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            for path, body, expected in (("near/vault", {"revision": 0, "vault": self.envelope(size=65536)}, 200),
                                         ("auth/login", {"password": "x" * 40000}, 413),
                                         ("near/vault", {"padding": "x" * VAULT_BODY_BYTES}, 413)):
                with self.subTest(path=path, expected=expected):
                    connection = http.client.HTTPConnection("127.0.0.1", server.server_address[1], timeout=10)
                    try:
                        connection.request("POST", "/workspace-api/" + path, json.dumps(body),
                                           {"Origin": "https://siyab.xyz", "Content-Type": "application/json",
                                            "Cookie": cookie, "X-CSRF-Token": csrf})
                        response = connection.getresponse()
                        data = response.read()
                        self.assertEqual(response.status, expected, data)
                        self.assertEqual(response.getheader("Cache-Control"), "no-store")
                    finally:
                        connection.close()
        finally:
            server.shutdown()
            server.server_close()
            thread.join()


if __name__ == "__main__":
    unittest.main()
