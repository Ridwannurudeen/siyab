import base64
import concurrent.futures
from contextlib import closing
import hashlib
import io
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from intel import workspace
from intel.workspace import SQLiteWorkspaceStore, WorkspaceError


class WorkspaceStoreTest(unittest.TestCase):
    def setUp(self):
        alert = patch.object(workspace, "_WATCH_ALERTED", False)
        alert.start()
        self.addCleanup(alert.stop)
        stderr = patch("sys.stderr", new_callable=io.StringIO)
        stderr.start()
        self.addCleanup(stderr.stop)
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "workspace.sqlite3"
        self.now = 1700000000.0
        self.store = SQLiteWorkspaceStore(self.path, clock=lambda: self.now)
        self.alice = self.store.register("alice", "alice-password-123")
        self.bob = self.store.register("bobby", "bobby-password-123")
        self.a = self.alice["user"]["id"]
        self.b = self.bob["user"]["id"]
        self.team = self.store.list_teams(self.a)[0]["id"]

    def error(self, status, function, *args):
        with self.assertRaises(WorkspaceError) as caught:
            function(*args)
        self.assertEqual(caught.exception.status, status)

    def invite(self, role="editor"):
        invitation = self.store.invite_member(self.a, self.team, "bobby", role)
        self.store.join_team(self.b, invitation["token"])
        return invitation

    def test_authentication_session_rotation_and_stored_hashes(self):
        authenticated = self.store.authenticate(self.alice["session_token"])
        self.assertEqual(authenticated["user"], self.alice["user"])
        self.error(401, self.store.login, "alice", "incorrect-password-123")
        self.error(401, self.store.login, "missing", "incorrect-password-123")
        login = self.store.login("alice", "alice-password-123")
        rotated = self.store.change_password(login["session_token"], "alice-password-123", "replacement-password-123")
        self.error(401, self.store.authenticate, self.alice["session_token"])
        self.error(401, self.store.authenticate, login["session_token"])
        self.assertEqual(self.store.authenticate(rotated["session_token"])["user"]["id"], self.a)
        with closing(sqlite3.connect(self.path)) as connection:
            dump = "\n".join(connection.iterdump())
        for secret in ("alice-password-123", "replacement-password-123", rotated["session_token"], rotated["recovery_code"]):
            self.assertNotIn(secret, dump)
        self.store.logout(rotated["session_token"])
        self.error(401, self.store.authenticate, rotated["session_token"])

    def test_access_codes_bypass_public_windows_and_keep_uses_transactional(self):
        self.store.max_hourly_registrations = 2
        code = self.store.create_access_code(2, 1)
        self.error(409, self.store.register, "alice", "alice-password-123", code["code"])
        self.assertEqual(self.store.list_access_codes()[0]["uses_left"], 2)
        reserved = self.store.register("judge", "judge-password-123", code["code"])
        self.assertEqual(self.store.list_access_codes()[0]["uses_left"], 1)
        with self.store._connection() as connection:
            self.assertIsNotNone(connection.execute("SELECT 1 FROM reserved_users WHERE user_id=?", (reserved["user"]["id"],)).fetchone())
            self.assertNotIn(code["code"], "\n".join(connection.iterdump()))
        self.error(429, self.store.register, "public", "public-password-123")
        self.store.grant_reserved("alice")
        self.store.register("public", "public-password-123")
        self.now += 86400
        self.error(403, self.store.register, "expired", "expired-password-123", code["code"])

    def test_access_code_is_checked_before_taken_username_without_burning_uses(self):
        code = self.store.create_access_code(2, 1)["code"]
        for username in ("alice", "unknown"):
            with self.subTest(username=username), self.assertRaises(WorkspaceError) as caught:
                self.store.register(username, "test-password-123", "a" * 43)
            self.assertEqual((caught.exception.status, caught.exception.code), (403, "invalid_access_code"))
        with self.assertRaises(WorkspaceError) as caught:
            self.store.register("alice", "test-password-123", code)
        self.assertEqual((caught.exception.status, caught.exception.code), (409, "username_taken"))
        self.assertEqual(self.store.list_access_codes()[0]["uses_left"], 2)

    def test_access_code_use_exhaustion_revoke_validation_and_budget(self):
        for uses, days in ((0, 1), (51, 1), (1, 0), (1, 61), (True, 1), (1, "1")):
            self.error(400, self.store.create_access_code, uses, days)
        code = self.store.create_access_code(1, 1)
        self.store.register("judge", "judge-password-123", code["code"])
        self.error(403, self.store.register, "second", "second-password-123", code["code"])
        for invalid in ("bad", "", 3):
            self.error(403, self.store.register, "invalid", "invalid-password-123", invalid)
        self.store.create_access_code(50, 1)
        self.store.create_access_code(50, 1)
        self.error(409, self.store.create_access_code, 1, 1)
        self.store.revoke_access_codes()
        self.assertTrue(all(row["uses_left"] == 0 for row in self.store.list_access_codes()))
        self.assertTrue(all(set(row) == {"created_ms", "expires_ms", "uses_left"} for row in self.store.list_access_codes()))
        self.store.create_access_code(1, 1)
        self.error(404, self.store.grant_reserved, "unknown")

    def test_access_code_use_rolls_back_after_later_registration_failure(self):
        code = self.store.create_access_code(1, 1)
        with patch.object(self.store, "_audit", side_effect=sqlite3.OperationalError("storage failure")):
            with self.assertRaises(sqlite3.OperationalError):
                self.store.register("judge", "judge-password-123", code["code"])
        self.assertEqual(self.store.list_access_codes()[0]["uses_left"], 1)
        self.store.register("judge", "judge-password-123", code["code"])

    def test_workspace_environment_caps_are_validated_at_start(self):
        defaults = {"SIYAB_REGISTRATIONS_PER_HOUR": "20", "SIYAB_REGISTRATIONS_PER_DAY": "100",
                    "SIYAB_WATCH_PUBLIC_ACCOUNTS": "90", "SIYAB_WATCH_RESERVED_ACCOUNTS": "30"}
        for name, values in (("SIYAB_REGISTRATIONS_PER_HOUR", ("0", "201", "bad", "1.5")),
                             ("SIYAB_REGISTRATIONS_PER_DAY", ("19", "2001")),
                             ("SIYAB_WATCH_PUBLIC_ACCOUNTS", ("-1", "501")),
                             ("SIYAB_WATCH_RESERVED_ACCOUNTS", ("-1", "101"))):
            for value in values:
                with self.subTest(name=name, value=value), patch.dict(workspace.os.environ, {**defaults, name: value}):
                    with self.assertRaises(ValueError):
                        SQLiteWorkspaceStore(self.path)
        with patch.dict(workspace.os.environ, defaults):
            reopened = SQLiteWorkspaceStore(self.path)
        self.assertEqual((reopened.max_hourly_registrations, reopened.max_daily_registrations,
                          reopened.watch_public_accounts, reopened.watch_reserved_accounts, reopened.watch_alert_accounts),
                         (20, 100, 90, 30, 72))
        with patch.dict(workspace.os.environ, {**defaults, "SIYAB_WATCH_PUBLIC_ACCOUNTS": "0", "SIYAB_WATCH_RESERVED_ACCOUNTS": "0"}):
            reopened = SQLiteWorkspaceStore(self.path)
        self.assertEqual((reopened.watch_public_accounts, reopened.watch_reserved_accounts), (0, 0))

    def test_empty_workspace_environment_caps_use_defaults(self):
        names = ("SIYAB_REGISTRATIONS_PER_HOUR", "SIYAB_REGISTRATIONS_PER_DAY",
                 "SIYAB_WATCH_PUBLIC_ACCOUNTS", "SIYAB_WATCH_RESERVED_ACCOUNTS")
        with patch.dict(workspace.os.environ, dict.fromkeys(names, "")):
            reopened = SQLiteWorkspaceStore(self.path)
        self.assertEqual((reopened.max_hourly_registrations, reopened.max_daily_registrations,
                          reopened.watch_public_accounts, reopened.watch_reserved_accounts, reopened.watch_alert_accounts),
                         (20, 100, 90, 30, 72))

    def test_zero_public_watch_pool_does_not_alert_on_reserved_add_or_import(self):
        with patch.dict(workspace.os.environ, {"SIYAB_WATCH_PUBLIC_ACCOUNTS": "0"}):
            reopened = SQLiteWorkspaceStore(self.path)
        reopened.grant_reserved("alice")
        with patch("sys.stderr", new_callable=io.StringIO) as output:
            reopened.add_watch(self.a, self.team, "one.near")
            reopened.import_workspace(self.a, self.team, [], ["two.near"])
        self.assertEqual(output.getvalue(), "")
        self.assertFalse(workspace._WATCH_ALERTED)
        self.assertEqual(len(reopened.watched_accounts()), 2)

    def test_watch_pools_share_accounts_and_return_public_slots_on_removal(self):
        self.store.watch_public_accounts = 1
        self.store.watch_reserved_accounts = 1
        other = self.store.list_teams(self.b)[0]["id"]
        self.store.add_watch(self.a, self.team, "shared.near")
        self.error(409, self.store.add_watch, self.b, other, "new.near")
        self.store.grant_reserved("bobby")
        self.store.add_watch(self.b, other, "shared.near")
        self.now += 1
        self.store.add_watch(self.a, self.team, "new.near")
        self.error(409, self.store.add_watch, self.b, other, "reserved.near")
        self.assertEqual(self.store.watch_schedule(), [
            {"account": "new.near", "created_ms": int(self.now * 1000), "reserved": 0},
            {"account": "shared.near", "created_ms": int((self.now - 1) * 1000), "reserved": 1}])
        self.store.remove_watch(self.b, other, "shared.near")
        self.assertEqual(len(self.store.watched_accounts()), 2)
        self.error(409, self.store.add_watch, self.a, self.team, "third.near")
        self.store.add_watch(self.a, self.team, "shared.near")

    def test_reserved_additions_above_legacy_pool_cap_fail_but_case_imports_work(self):
        self.store.add_watch(self.a, self.team, "one.near")
        self.store.add_watch(self.a, self.team, "two.near")
        self.store.grant_reserved("alice")
        self.store.watch_reserved_accounts = 1
        other = self.store.create_team(self.a, "Second")["id"]
        self.error(409, self.store.add_watch, self.a, other, "one.near")
        self.error(409, self.store.import_workspace, self.a, other, [], ["one.near"])
        self.assertEqual(self.store.import_workspace(self.a, other, [{"title": "Evidence", "notes": "", "url": "/"}], []),
                         {"created_cases": 1, "added_watches": 0, "skipped_cases": 0})
        self.assertEqual(self.store.add_watch(self.a, self.team, "one.near")["account"], "one.near")
        self.assertEqual(self.store.list_watchlist(self.a, other), [])

    def test_random_tokens_keep_existing_encoding_and_entropy(self):
        entropy = bytes(range(32))
        with patch.object(workspace.secrets, "token_bytes", return_value=entropy) as generate:
            token = workspace._random_token()
        generate.assert_called_once_with(32)
        self.assertRegex(token, r"^[A-Za-z0-9_-]{43}$")
        self.assertEqual(base64.b64decode(token + "=", altchars=b"-_"), entropy)
        tokens = {workspace._random_token() for _ in range(100)}
        self.assertEqual(len(tokens), 100)
        self.assertTrue(all(workspace._TOKEN.fullmatch(value) for value in tokens))

    def test_recovery_is_single_use_and_revokes_sessions(self):
        recovered = self.store.recover("alice", self.alice["recovery_code"], "recovered-password-123")
        self.error(401, self.store.authenticate, self.alice["session_token"])
        self.error(401, self.store.recover, "alice", self.alice["recovery_code"], "another-password-123")
        self.assertNotEqual(recovered["recovery_code"], self.alice["recovery_code"])
        self.now += 8 * 86400
        self.error(401, self.store.authenticate, recovered["session_token"])

    def test_case_isolation_viewer_permissions_and_conflicts(self):
        case = self.store.create_case(self.a, self.team, "Evidence", "<script>literal</script>", "/accounts/wrap.near.html")
        self.error(403, self.store.list_cases, self.b, self.team)
        self.error(403, self.store.update_case, self.b, self.team, case["id"], "Changed", "", "/", 1)
        self.invite("viewer")
        self.assertEqual(self.store.list_cases(self.b, self.team)["cases"][0]["notes"], "<script>literal</script>")
        self.error(403, self.store.create_case, self.b, self.team, "Denied", "", "/")
        self.error(403, self.store.add_watch, self.b, self.team, "wrap.near")
        self.store.set_role(self.a, self.team, self.b, "editor")
        changed = self.store.update_case(self.b, self.team, case["id"], "Updated", "notes", "/", 1)
        self.assertEqual(changed["version"], 2)
        self.error(409, self.store.update_case, self.a, self.team, case["id"], "Lost update", "", "/", 1)
        self.error(409, self.store.delete_case, self.a, self.team, case["id"], 1)
        self.store.delete_case(self.a, self.team, case["id"], 2)
        self.assertEqual(self.store.list_cases(self.a, self.team)["cases"], [])

    def test_membership_in_one_team_does_not_authorize_another_team_resource(self):
        other_team = self.store.list_teams(self.b)[0]["id"]
        other_case = self.store.create_case(self.b, other_team, "Private", "Other team notes", "/")
        self.error(404, self.store.update_case, self.a, self.team, other_case["id"], "Intrusion", "", "/", 1)
        self.error(404, self.store.delete_case, self.a, self.team, other_case["id"], 1)
        self.error(403, self.store.list_watchlist, self.a, other_team)
        self.error(403, self.store.list_members, self.a, other_team)
        self.error(403, self.store.invite_member, self.b, self.team, "thirduser", "viewer")
        self.assertEqual(self.store.list_cases(self.b, other_team)["cases"][0]["notes"], "Other team notes")

    def test_invite_recipient_expiry_replay_and_last_owner(self):
        invitation = self.store.invite_member(self.a, self.team, "bobby", "editor")
        self.error(403, self.store.join_team, self.a, invitation["token"])
        self.store.join_team(self.b, invitation["token"])
        self.error(409, self.store.join_team, self.b, invitation["token"])
        self.error(409, self.store.remove_member, self.a, self.team, self.a)
        self.error(409, self.store.set_role, self.a, self.team, self.a, "viewer")
        expired = self.store.invite_member(self.a, self.team, "thirduser", "viewer")
        third = self.store.register("thirduser", "third-password-123")
        self.now += 86401
        self.error(410, self.store.join_team, third["user"]["id"], expired["token"])

    def test_last_owner_rule_survives_concurrent_demotions(self):
        self.invite()
        self.store.set_role(self.a, self.team, self.b, "owner")
        def demote(user):
            try:
                self.store.set_role(user, self.team, user, "editor")
                return 200
            except WorkspaceError as error:
                return error.status
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(demote, (self.a, self.b)))
        self.assertEqual(sorted(results), [200, 409])
        owners = [row for row in self.store.list_members(self.a, self.team) if row["role"] == "owner"]
        self.assertEqual(len(owners), 1)

    def event(self, title="watch_account_key_added", stamp=None):
        return {"signal": title, "detail": "Observed public chain event", "subject": "wrap.near",
                "observed": {"timestamp_ms": int(self.now * 1000) if stamp is None else stamp, "block_height": 10}}

    def ingest(self, event, notify=True):
        digest = hashlib.sha256(json.dumps(event, sort_keys=True).encode()).hexdigest()
        return digest, self.store.ingest_event(digest, event, notify=notify)

    def test_journal_backfill_late_events_dedup_and_watch_baseline(self):
        old = self.event(stamp=int(self.now * 1000) - 10)
        old_hash, old_seq = self.ingest(old, notify=False)
        self.store.add_watch(self.a, self.team, "wrap.near")
        self.now += 1
        new = self.event()
        digest, seq = self.ingest(new)
        self.assertGreater(seq, old_seq)
        self.assertEqual(self.store.ingest_event(digest, new, notify=True), seq)
        self.assertEqual(self.store.ingest_event(old_hash, old, notify=True), old_seq)
        late = self.event("late_transfer", stamp=int(self.now * 1000) - 10000)
        _, late_seq = self.ingest(late)
        self.assertGreater(late_seq, seq)
        inbox = self.store.list_inbox(self.a, self.team)
        self.assertEqual(len(inbox["items"]), 1)
        self.assertEqual(inbox["items"][0]["event_id"], digest)
        self.error(403, self.store.list_inbox, self.b, self.team)
        self.store.ack_inbox(self.a, self.team, inbox["items"][0]["id"])
        self.assertIsNotNone(self.store.list_inbox(self.a, self.team)["items"][0]["acknowledged_ms"])
        self.assertEqual(len(self.store.events_after()["items"]), 3)

    def test_duplicate_watch_preserves_baseline_and_candidates_are_explicit(self):
        initial = self.store.add_watch(self.a, self.team, "wrap.near")
        self.now += 1
        self.assertEqual(self.store.add_watch(self.a, self.team, "wrap.near"), initial)
        self.ingest(self.event("full_access_key_then_outflow"))
        item = self.store.list_inbox(self.a, self.team)["items"][0]
        self.assertTrue(item["candidate"])
        self.assertFalse(item["external_delivery_eligible"])

    def test_restart_backup_and_audit_are_transactional(self):
        case = self.store.create_case(self.a, self.team, "Persistent", "notes", "/")
        self.store.add_watch(self.a, self.team, "wrap.near")
        backup = Path(self.directory.name) / "backup.sqlite3"
        self.store.backup(backup)
        restored = SQLiteWorkspaceStore(backup, clock=lambda: self.now)
        self.assertEqual(restored.list_cases(self.a, self.team)["cases"][0]["id"], case["id"])
        self.assertEqual(restored.list_watchlist(self.a, self.team)[0]["account"], "wrap.near")
        actions = [row["action"] for row in restored.list_audit(self.a, self.team)["items"]]
        self.assertIn("case.created", actions)
        self.assertIn("watch.added", actions)
        self.error(403, restored.list_audit, self.b, self.team)

    def test_untrusted_values_are_rejected_without_partial_mutations(self):
        for url in ("https://evil.test/", "//evil.test/", "/\\evil.test", "/../index.html", "javascript:alert(1)"):
            self.error(400, self.store.create_case, self.a, self.team, "Bad", "", url)
        self.error(400, self.store.create_case, self.a, self.team, "", "", "/")
        self.error(400, self.store.add_watch, self.a, self.team, "bad!account")
        self.error(400, self.store.add_watch, self.a, self.team, "vendor.testnet")
        self.assertEqual(self.store.list_cases(self.a, self.team)["cases"], [])
        self.error(400, self.store.list_cases, self.a, self.team, 0, 501)
        self.error(400, self.store.ingest_event, "not-a-hash", self.event())

    def test_invalid_unicode_and_nonfinite_events_fail_at_the_boundary(self):
        self.error(400, self.store.create_case, self.a, self.team, "\ud800", "", "/")
        self.error(400, self.store.create_case, self.a, self.team, "Bad URL", "", "/?query=\ud800")
        self.error(400, self.store.register, "surrogate", "password-length\ud800")
        self.error(401, self.store.login, "alice", "password-length\ud800")
        event = self.event()
        event["observed"]["timestamp_ms"] = float("nan")
        self.error(400, self.store.ingest_event, "a" * 64, event)
        self.error(400, self.store.remove_watch, self.a, self.team, {"account": "wrap.near"})
        self.assertEqual(self.store.events_after()["items"], [])

    def test_case_urls_match_supported_investigation_pages(self):
        valid = {"title": "Preserved", "notes": "Original", "url": "/"}
        saved = self.store.create_case(self.a, self.team, **valid)
        before = self.store.list_audit(self.a, self.team)
        for url in ("/foo", "/404.html", "/live/accounts/wrap.near/profile", "/workspace-api/session",
                    "/graph-data/" + "a" * 64 + ".json", "/accounts/nested/wrap.near.html"):
            with self.subTest(url=url):
                self.error(400, self.store.create_case, self.a, self.team, "Bad", "", url)
                self.error(400, self.store.update_case, self.a, self.team, saved["id"], "Bad", "", url, 1)
                self.error(400, self.store.import_workspace, self.a, self.team, [valid, {**valid, "url": url}], ["wrap.near"])
        self.assertEqual(self.store.list_cases(self.a, self.team)["cases"], [saved])
        self.assertEqual(self.store.list_watchlist(self.a, self.team), [])
        self.assertEqual(self.store.list_audit(self.a, self.team), before)
        paths = ["/"] + ["/" + page + ".html" for page in
                          ("index", "workspace", "investigate", "feed", "verify", "issuers", "top-receivers")]
        paths += ["/" + section + "/sample-1.near.html" for section in
                  ("accounts", "entities", "flows", "clusters", "incidents", "records")]
        for url in paths:
            with self.subTest(valid_url=url):
                self.store.create_case(self.a, self.team, "Valid", "", url + "?query=value#section")

    def test_import_is_explicit_atomic_and_idempotent_without_overwriting_notes(self):
        original = self.store.create_case(self.a, self.team, "Existing", "Original notes", "/investigate.html?account=wrap.near")
        cases = [{"title": "Existing", "notes": "Original notes", "url": original["url"]},
                 {"title": "New", "notes": "Imported notes", "url": "/accounts/wrap.near.html"}]
        result = self.store.import_workspace(self.a, self.team, cases, ["wrap.near", "wrap.near"])
        self.assertEqual(result, {"created_cases": 1, "added_watches": 1, "skipped_cases": 1})
        audit = self.store.list_audit(self.a, self.team)["items"]
        self.assertEqual(self.store.import_workspace(self.a, self.team, cases, ["wrap.near"]),
                         {"created_cases": 0, "added_watches": 0, "skipped_cases": 2})
        self.assertEqual(self.store.list_audit(self.a, self.team)["items"], audit)
        rows = self.store.list_cases(self.a, self.team)["cases"]
        self.assertEqual(next(row for row in rows if row["id"] == original["id"])["notes"], "Original notes")
        self.assertEqual(next(row for row in rows if row["title"] == "New")["version"], 1)
        changed_notes = [{"title": "Existing", "notes": "Different imported notes", "url": original["url"]}]
        self.assertEqual(self.store.import_workspace(self.a, self.team, changed_notes, [])["created_cases"], 1)
        self.assertEqual(self.store.list_cases(self.a, self.team)["total"], 3)

    def test_import_rejects_invalid_later_items_and_cross_team_or_viewer_writes(self):
        valid = {"title": "Good", "notes": "Local", "url": "/"}
        before = self.store.list_audit(self.a, self.team)
        self.error(400, self.store.import_workspace, self.a, self.team, [valid, {**valid, "url": "https://evil.test"}], ["wrap.near"])
        self.error(400, self.store.import_workspace, self.a, self.team, [valid], ["wrap.near", "vendor.testnet"])
        self.error(400, self.store.import_workspace, self.a, self.team, [valid], ["testnet"])
        self.error(400, self.store.import_workspace, self.a, self.team, [{**valid, "id": "untrusted"}], [])
        self.error(400, self.store.import_workspace, self.a, self.team, [valid] * 51, [])
        self.error(400, self.store.import_workspace, self.a, self.team, [], ["wrap.near"] * 101)
        self.error(403, self.store.import_workspace, self.b, self.team, [valid], ["wrap.near"])
        self.assertEqual(self.store.list_audit(self.a, self.team), before)
        self.invite("viewer")
        after_invite = self.store.list_audit(self.a, self.team)
        self.error(403, self.store.import_workspace, self.b, self.team, [valid], ["wrap.near"])
        self.assertEqual(self.store.list_cases(self.a, self.team)["cases"], [])
        self.assertEqual(self.store.list_watchlist(self.a, self.team), [])
        self.assertEqual(self.store.list_audit(self.a, self.team), after_invite)
        self.error(400, self.store.add_watch, self.a, self.team, "testnet")
        self.error(400, self.store.remove_watch, self.a, self.team, "testnet")

    def test_import_checks_capacity_after_dedup_before_any_write(self):
        now = int(self.now * 1000)
        with closing(sqlite3.connect(self.path)) as connection:
            connection.executemany("INSERT INTO cases VALUES (?,?,?,?,?,?,?,?,?,?)",
                                   [(f"case-{index}", self.team, f"Case {index}", "", "/", 1, now, now, self.a, self.a)
                                    for index in range(499)])
            connection.commit()
        cases = [{"title": "First new", "notes": "", "url": "/"}, {"title": "Second new", "notes": "", "url": "/"}]
        self.error(409, self.store.import_workspace, self.a, self.team, cases, ["wrap.near"])
        self.assertEqual(self.store.list_cases(self.a, self.team)["total"], 499)
        self.assertEqual(self.store.list_watchlist(self.a, self.team), [])
        duplicate = {"title": "Case 0", "notes": "", "url": "/"}
        self.assertEqual(self.store.import_workspace(self.a, self.team, [duplicate, cases[0]], ["wrap.near"]),
                         {"created_cases": 1, "added_watches": 1, "skipped_cases": 1})
        with closing(sqlite3.connect(self.path)) as connection:
            connection.executemany("INSERT INTO watchlist VALUES (?,?,?,?)",
                                   [(self.team, f"watch-{index}.near", now, 0) for index in range(99)])
            connection.commit()
        self.error(409, self.store.import_workspace, self.a, self.team, [duplicate], ["new-watch.near"])
        self.assertEqual(len(self.store.list_watchlist(self.a, self.team)), 100)

    def test_import_rolls_back_data_and_audit_on_a_storage_failure(self):
        cases = [{"title": title, "notes": "", "url": "/"} for title in ("First", "Second")]
        before = self.store.list_audit(self.a, self.team)
        original_audit = self.store._audit
        calls = 0
        def failing_audit(*args, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise sqlite3.OperationalError("simulated storage failure")
            return original_audit(*args, **kwargs)
        with patch.object(self.store, "_audit", side_effect=failing_audit):
            with self.assertRaises(sqlite3.OperationalError):
                self.store.import_workspace(self.a, self.team, cases, ["wrap.near"])
        self.assertEqual(self.store.list_cases(self.a, self.team)["cases"], [])
        self.assertEqual(self.store.list_watchlist(self.a, self.team), [])
        self.assertEqual(self.store.list_audit(self.a, self.team), before)

    def test_concurrent_import_retries_create_each_record_once(self):
        cases = [{"title": "Concurrent", "notes": "Preserved", "url": "/investigate.html?account=wrap.near"}]
        def import_batch(_):
            return self.store.import_workspace(self.a, self.team, cases, ["wrap.near"])
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(import_batch, range(2)))
        self.assertEqual(sum(row["created_cases"] for row in results), 1)
        self.assertEqual(sum(row["added_watches"] for row in results), 1)
        self.assertEqual(sum(row["skipped_cases"] for row in results), 1)
        self.assertEqual(self.store.list_cases(self.a, self.team)["total"], 1)
        self.assertEqual(len(self.store.list_watchlist(self.a, self.team)), 1)

    def test_fourth_team_create_and_join_are_refused_without_removing_memberships(self):
        self.store.create_team(self.a, "Second")
        self.store.create_team(self.a, "Third")
        other = self.store.list_teams(self.b)[0]["id"]
        invitation = self.store.invite_member(self.b, other, "alice", "editor")
        for function, args in ((self.store.create_team, (self.a, "Fourth")),
                               (self.store.join_team, (self.a, invitation["token"]))):
            with self.assertRaises(WorkspaceError) as caught:
                function(*args)
            self.assertEqual((caught.exception.status, caught.exception.code), (409, "team_limit"))
            self.assertIn("at most 3 teams", caught.exception.message)
        self.assertEqual(len(self.store.list_teams(self.a)), 3)
        with self.store._connection() as connection:
            self.assertIsNone(connection.execute("SELECT used_ms FROM invites").fetchone()[0])

    def test_team_limit_messages_use_the_current_cap_for_create_and_join(self):
        other = self.store.list_teams(self.b)[0]["id"]
        invitation = self.store.invite_member(self.b, other, "alice", "editor")
        with patch.object(workspace, "MAX_TEAMS", 1):
            for function, args in ((self.store.create_team, (self.a, "Second")),
                                   (self.store.join_team, (self.a, invitation["token"]))):
                with self.subTest(function=function.__name__), self.assertRaises(WorkspaceError) as caught:
                    function(*args)
                self.assertEqual((caught.exception.status, caught.exception.code), (409, "team_limit"))
                self.assertEqual(caught.exception.message, "An account can belong to at most 1 teams.")

    def test_reopening_keeps_three_legacy_memberships_above_the_new_cap(self):
        with self.store._connection(write=True) as connection:
            for index in range(5):
                team = f"legacy-{index}"
                connection.execute("INSERT INTO teams VALUES (?,?,?)", (team, team, 0))
                connection.execute("INSERT INTO memberships VALUES (?,?,?)", (team, self.a, "owner"))
        self.store = SQLiteWorkspaceStore(self.path, clock=lambda: self.now)
        self.assertEqual(len(self.store.list_teams(self.a)), 6)
        with self.assertRaises(WorkspaceError) as caught:
            self.store.create_team(self.a, "Seventh")
        self.assertEqual(caught.exception.code, "team_limit")
        self.assertEqual(len(self.store.list_teams(self.a)), 6)

    def test_watch_limit_counts_distinct_accounts_by_adding_user_across_teams(self):
        self.invite()
        second = self.store.create_team(self.a, "Second")["id"]
        for index in range(5):
            self.store.add_watch(self.a, self.team if index < 3 else second, f"watch-{index}.near")
        self.store.add_watch(self.a, second, "watch-0.near")
        self.store.add_watch(self.b, self.team, "watch-0.near")
        self.store.add_watch(self.b, self.team, "bobby.near")
        before = self.store.list_audit(self.a, second)
        with self.assertRaises(WorkspaceError) as caught:
            self.store.add_watch(self.a, second, "eleventh.near")
        self.assertEqual((caught.exception.status, caught.exception.code), (409, "watch_limit"))
        self.assertEqual(self.store.list_audit(self.a, second), before)
        self.store.remove_watch(self.a, self.team, "watch-0.near")
        self.error(409, self.store.add_watch, self.a, second, "eleventh.near")
        self.store.remove_watch(self.a, second, "watch-0.near")
        self.store.add_watch(self.a, second, "eleventh.near")

    def test_departing_a_team_does_not_release_its_added_watches(self):
        self.invite()
        self.store.set_role(self.a, self.team, self.b, "owner")
        for index in range(5):
            self.store.add_watch(self.a, self.team, f"watch-{index}.near")
        self.store.remove_member(self.b, self.team, self.a)
        other = self.store.create_team(self.a, "Other")["id"]
        with self.assertRaises(WorkspaceError) as caught:
            self.store.add_watch(self.a, other, "eleventh.near")
        self.assertEqual(caught.exception.code, "watch_limit")
        self.assertEqual(len(self.store.list_watchlist(self.b, self.team)), 5)

    def test_global_watch_limit_refuses_only_new_distinct_accounts(self):
        with self.store._connection(write=True) as connection:
            connection.executemany("INSERT INTO watchlist VALUES (?,?,?,?)",
                                   [(self.team, f"watch-{index}.near", 0, 0) for index in range(90)])
        other = self.store.list_teams(self.b)[0]["id"]
        before = self.store.list_audit(self.b, other)
        with self.assertRaises(WorkspaceError) as caught:
            self.store.add_watch(self.b, other, "new.near")
        self.assertEqual((caught.exception.status, caught.exception.code), (409, "watch_limit"))
        self.assertEqual(self.store.list_audit(self.b, other), before)
        self.store.add_watch(self.b, other, "watch-0.near")
        self.assertEqual(len(self.store.watched_accounts()), 90)
        self.store.remove_watch(self.a, self.team, "watch-0.near")
        self.error(409, self.store.add_watch, self.b, other, "new.near")
        self.store.remove_watch(self.b, other, "watch-0.near")
        self.store.add_watch(self.b, other, "new.near")
        self.assertEqual(len(self.store.watched_accounts()), 90)

    def test_watch_alert_prints_once_at_eighty_percent_across_store_reopens(self):
        with self.store._connection(write=True) as connection:
            connection.executemany("INSERT INTO watchlist VALUES (?,?,?,?)",
                                   [(self.team, f"watch-{index}.near", 0, 0) for index in range(70)])
        with patch.object(workspace, "_WATCH_ALERTED", False), patch("sys.stderr", new_callable=io.StringIO) as output:
            self.store.add_watch(self.a, self.team, "ninety-five.near")
            self.assertEqual(output.getvalue(), "")
            self.store.import_workspace(self.a, self.team, [], ["ninety-six.near"])
            self.assertEqual(len(output.getvalue().splitlines()), 1)
            self.assertIn("72", output.getvalue())
            self.assertIn("90", output.getvalue())
            reopened = SQLiteWorkspaceStore(self.path, clock=lambda: self.now)
            reopened.add_watch(self.a, self.team, "ninety-seven.near")
            reopened.remove_watch(self.a, self.team, "ninety-six.near")
            reopened.add_watch(self.a, self.team, "ninety-six.near")
            self.assertEqual(len(output.getvalue().splitlines()), 1)

    def test_import_watch_caps_are_checked_before_cases_and_audit_change(self):
        self.store.import_workspace(self.a, self.team, [], [f"watch-{index}.near" for index in range(4)])
        before = self.store.list_audit(self.a, self.team)
        with self.assertRaises(WorkspaceError) as caught:
            self.store.import_workspace(self.a, self.team, [{"title": "New", "notes": "", "url": "/"}],
                                        ["tenth.near", "eleventh.near"])
        self.assertEqual(caught.exception.code, "watch_limit")
        self.assertEqual(len(self.store.list_watchlist(self.a, self.team)), 4)
        self.assertEqual(self.store.list_cases(self.a, self.team)["cases"], [])
        self.assertEqual(self.store.list_audit(self.a, self.team), before)
        with self.store._connection(write=True) as connection:
            connection.executemany("INSERT INTO watchlist VALUES (?,?,?,?)",
                                   [(self.team, f"legacy-{index}.near", 0, 0) for index in range(110)])
        with self.assertRaises(WorkspaceError) as caught:
            self.store.import_workspace(self.b, self.store.list_teams(self.b)[0]["id"], [],
                                        ["one-new.near", "two-new.near"])
        self.assertEqual(caught.exception.code, "watch_limit")
        self.assertEqual(len(self.store.watched_accounts()), 114)

    def test_legacy_watch_attribution_and_over_cap_watches_survive_reopen(self):
        with self.store._connection(write=True) as connection:
            for index in range(11):
                account = f"legacy-{index}.near"
                connection.execute("INSERT INTO watchlist VALUES (?,?,?,?)", (self.team, account, 0, 0))
                self.store._audit(connection, self.team, self.a, "watch.added", account)
        reopened = SQLiteWorkspaceStore(self.path, clock=lambda: self.now)
        self.assertEqual(len(reopened.list_watchlist(self.a, self.team)), 11)
        with self.assertRaises(WorkspaceError) as caught:
            reopened.add_watch(self.a, self.team, "new.near")
        self.assertEqual(caught.exception.code, "watch_limit")
        self.assertEqual(len(reopened.list_watchlist(self.a, self.team)), 11)

    def test_concurrent_watch_additions_cannot_exceed_the_user_limit(self):
        for index in range(4):
            self.store.add_watch(self.a, self.team, f"watch-{index}.near")
        def add(index):
            try:
                self.store.add_watch(self.a, self.team, f"concurrent-{index}.near")
                return 201
            except WorkspaceError as error:
                return error.status
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            self.assertEqual(sorted(pool.map(add, range(2))), [201, 409])
        self.assertEqual(len(self.store.list_watchlist(self.a, self.team)), 5)

    def test_internal_watched_accounts_deduplicates_across_teams(self):
        other_team = self.store.list_teams(self.b)[0]["id"]
        self.store.add_watch(self.a, self.team, "wrap.near")
        self.store.add_watch(self.b, other_team, "wrap.near")
        self.store.add_watch(self.b, other_team, "another.near")
        self.assertEqual(self.store.watched_accounts(), ["another.near", "wrap.near"])
        self.store.remove_watch(self.a, self.team, "wrap.near")
        self.assertEqual(self.store.watched_accounts(), ["another.near", "wrap.near"])
        self.store.remove_watch(self.b, other_team, "wrap.near")
        self.assertEqual(self.store.watched_accounts(), ["another.near"])


if __name__ == "__main__":
    unittest.main()
