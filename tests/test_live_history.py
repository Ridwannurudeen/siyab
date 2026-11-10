import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from intel import accounts, history, identity, live


class LiveHistoryTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.archive = history.AccountHistory(Path(temporary.name) / "history.sqlite3")
        self.api = live.LiveAPI(history_store=self.archive)

    def request(self, path):
        return self.api.handle(path, "203.0.113.5", {})

    def test_successful_provider_pages_persist_with_provenance(self):
        payload = {"account_id": "wrap.near", "network": "mainnet", "items": [],
                   "source": {"provider": "FastNEAR"}, "observed_at": "2026-11-10T00:00:00Z"}
        with mock.patch.object(accounts.AccountData, "transfers", return_value=payload):
            self.assertEqual(self.request("/live/accounts/wrap.near/transfers")[0], 200)
        with self.archive._connection() as connection:
            stored = connection.execute("SELECT body FROM pages").fetchone()[0]
        self.assertEqual(json.loads(stored), payload)

    def test_archive_queries_bypass_cache_and_retain_sources(self):
        route = "/live/accounts/wrap.near/history?kind=transactions&limit=1"
        self.assertEqual(self.request(route)[1]["items"], [])
        payload = {"account_id": "wrap.near", "network": "mainnet", "items": [
            {"transaction_hash": "tx1", "tx_block_timestamp": "1794308552765806506"}],
                   "source": {"provider": "FastNEAR"}, "observed_at": "2026-11-10T00:00:00Z"}
        self.archive.record_page("wrap.near", "transactions", payload)
        status, body, _ = self.request(route)
        self.assertEqual(status, 200)
        self.assertEqual(body["items"], payload["items"])
        self.assertEqual(body["observations"][0]["source"], payload["source"])
        self.assertFalse(body["coverage"]["history_complete"])

    def test_identity_dispatch_and_graph_snapshot_immutable_exact_bytes(self):
        with mock.patch.object(identity.IdentityData, "inspect", return_value={"coverage": "unknown"}) as inspect:
            self.assertEqual(self.request("/live/accounts/wrap.near/identity")[:2], (200, {"coverage": "unknown"}))
            inspect.assert_called_once_with("wrap.near")
        graph = {"kind": "flow", "root": "wrap.near", "source": {"network": "mainnet"},
                 "nodes": [], "edges": [], "records": []}
        with mock.patch.object(accounts.AccountData, "neighborhood", return_value=graph):
            status, body, _ = self.request("/live/accounts/wrap.near/graph")
        self.assertEqual(status, 200)
        identifier = body["snapshot"]["id"]
        status, raw, headers = self.request(body["snapshot"]["url"])
        self.assertEqual(status, 200)
        self.assertEqual(hashlib.sha256(raw).hexdigest(), identifier)
        self.assertEqual(json.loads(raw), graph)
        self.assertIn("immutable", headers["Cache-Control"])

    def test_backfill_dispatch_is_bounded_and_not_cached(self):
        route = "/live/accounts/wrap.near/backfill?kind=transfers&cursor=opaque%2B%2F%3D&max_pages=1"
        with mock.patch.object(self.archive, "backfill_account", return_value={"pages": 1}) as backfill:
            self.assertEqual(self.request(route)[:2], (200, {"pages": 1}))
            self.assertEqual(self.request(route)[:2], (200, {"pages": 1}))
            self.assertEqual(backfill.call_count, 2)
            self.assertEqual(backfill.call_args.args[1:], ("wrap.near", "transfers"))
            self.assertEqual(backfill.call_args.kwargs, {"cursor": "opaque+/=", "max_pages": 1})

    def test_archive_and_snapshot_unavailability_and_strict_inputs(self):
        for path in ("/live/accounts/wrap.near/history?kind=transfers", "/live/accounts/wrap.near/backfill?kind=transactions"):
            self.assertEqual(live.LiveAPI().handle(path, "203.0.113.5", {})[0], 503)
        for path in ("/live/accounts/foo.testnet/identity", "/live/accounts/wrap.near/identity?extra=1",
                     "/live/accounts/wrap.near/history?kind=profile", "/live/accounts/wrap.near/history?kind=transfers&before=-1",
                     "/live/accounts/wrap.near/backfill?kind=transfers&max_pages=3", "/live/accounts/wrap.near/backfill?kind=transfers&kind=transactions"):
            self.assertEqual(self.request(path)[0], 400, path)
        self.assertEqual(self.request("/live/account-data/" + "a" * 64 + ".json")[0], 404)
        for path, query in (("/live/account-data/../private", ""), ("/live/account-data/" + "a" * 64 + ".json", "x=1")):
            self.assertEqual(self.request(path + ("?" + query if query else ""))[0], 400)

    def test_all_successful_account_page_kinds_are_recorded(self):
        for kind in ("profile", "holdings", "transactions", "transfers"):
            payload = {"account_id": "wrap.near", "network": "mainnet", "source": {"provider": "FastNEAR"}, "items": []}
            with self.subTest(kind=kind), mock.patch.object(accounts.AccountData, kind, return_value=payload), \
                    mock.patch.object(self.archive, "record_page", wraps=self.archive.record_page) as record:
                self.assertEqual(self.request("/live/accounts/wrap.near/" + kind)[0], 200)
                record.assert_called_once_with("wrap.near", kind, payload)

    def test_snapshot_and_backfill_requests_share_rate_and_deadline_enforcement(self):
        service = live.LiveAPI(history_store=self.archive, rate_per_minute=1)
        target = "/live/account-events/" + "a" * 64 + ".json"
        with mock.patch.object(self.archive, "get_event_bytes", return_value=b"{}\n") as read:
            self.assertEqual(service.handle(target, "203.0.113.5", {})[:2], (200, b"{}\n"))
            self.assertEqual(service.handle(target, "203.0.113.5", {})[0], 429)
            read.assert_called_once_with("a" * 64)
        with mock.patch.object(self.archive, "backfill_account", side_effect=TimeoutError("private failure")):
            status, body, _ = self.request("/live/accounts/wrap.near/backfill?kind=transfers")
        self.assertEqual(status, 504)
        self.assertNotIn("private failure", str(body))

    def test_retention_and_read_budget_failures_return_explicit_incomplete_coverage(self):
        with mock.patch.object(self.archive, "backfill_account", side_effect=history.HistoryCapacityError("Account backfill budget reached.")):
            status, body, headers = self.request("/live/accounts/wrap.near/backfill?kind=transfers")
        self.assertEqual(status, 503)
        self.assertEqual(body["error"], "archive_capacity")
        self.assertFalse(body["coverage"]["history_complete"])
        self.assertTrue(body["coverage"]["new_observations_paused"])
        self.assertEqual(headers["Retry-After"], "60")


if __name__ == "__main__":
    unittest.main()
