import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from intel import accounts, history, identity, live, portfolio


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
        target = "/live/account-data/" + "a" * 64 + ".json"
        with mock.patch.object(self.archive, "get_graph_bytes", return_value=b"{}\n") as read:
            self.assertEqual(service.handle(target, "203.0.113.5", {})[:2], (200, b"{}\n"))
            self.assertEqual(service.handle(target, "203.0.113.5", {})[0], 429)
            read.assert_called_once_with("a" * 64)
        with mock.patch.object(self.archive, "backfill_account", side_effect=TimeoutError("private failure")):
            status, body, _ = self.request("/live/accounts/wrap.near/backfill?kind=transfers")
        self.assertEqual(status, 504)
        self.assertNotIn("private failure", str(body))

    def test_public_event_route_answers_a_real_event_id_exactly_as_an_unknown_one(self):
        item = {"from": "alice.near", "to": "bob.near", "asset_id": "native:near", "amount": "1",
                "block_timestamp": "1000000000000", "receipt_id": "receipt1", "transfer_index": 0,
                "transaction_hash": "tx1", "block_height": "12"}
        identifier, body = history.AccountHistory._event(item)
        with self.archive._connection(write=True) as connection:
            connection.execute("INSERT INTO event_outbox VALUES (?,?,?,?,NULL)", (identifier, body, 1, 0))
        self.assertEqual(self.archive.get_event_bytes(identifier), body)
        real = self.request("/live/account-events/" + identifier + ".json")
        unknown = self.request("/live/account-events/" + "b" * 64 + ".json")
        self.assertEqual(real[0], 404)
        self.assertEqual(real, unknown)
        self.assertEqual(real, self.request("/live/account-events/" + identifier + ".json?x=1"))

    def test_damaged_graph_bodies_answer_503_instead_of_failing(self):
        nested = b"[" * 100000 + b"]" * 100000
        text = '{"kind":"flow"}\n'
        with self.archive._connection(write=True) as connection:
            connection.execute("INSERT INTO graphs VALUES (?,?,?)", (hashlib.sha256(nested).hexdigest(), nested, 0))
            connection.execute("INSERT INTO graphs VALUES (?,?,?)", (hashlib.sha256(text.encode()).hexdigest(), text, 0))
        for identifier in (hashlib.sha256(nested).hexdigest(), hashlib.sha256(text.encode()).hexdigest()):
            with self.subTest(identifier=identifier):
                with self.assertRaises(history.HistoryReadError):
                    self.archive.get_graph_bytes(identifier)
                status, body, _ = self.request("/live/account-data/" + identifier + ".json")
                self.assertEqual((status, body["error"]), (503, "read_failed"))

    def test_retention_and_read_budget_failures_return_explicit_incomplete_coverage(self):
        with mock.patch.object(self.archive, "backfill_account", side_effect=history.HistoryCapacityError("Account backfill budget reached.")):
            status, body, headers = self.request("/live/accounts/wrap.near/backfill?kind=transfers")
        self.assertEqual(status, 503)
        self.assertEqual(body["error"], "archive_capacity")
        self.assertFalse(body["coverage"]["history_complete"])
        self.assertTrue(body["coverage"]["new_observations_paused"])
        self.assertEqual(headers["Retry-After"], "60")

    def test_full_archive_still_returns_the_successful_live_read_with_archive_paused(self):
        payload = {"account_id": "wrap.near", "network": "mainnet", "items": [],
                   "source": {"provider": "test-provider"}, "observed_at": "2026-11-10T00:00:00Z"}
        graph = {"kind": "flow", "root": "wrap.near", "source": {"network": "mainnet"},
                 "nodes": [], "edges": [], "records": []}
        with mock.patch.object(history, "MAX_ARCHIVE_PAGES", 0), mock.patch.object(history, "MAX_ARCHIVE_GRAPHS", 0), \
                mock.patch.object(accounts.AccountData, "transfers", return_value=payload), \
                mock.patch.object(accounts.AccountData, "neighborhood", return_value=graph):
            self.assertEqual(self.request("/live/accounts/wrap.near/transfers")[:2], (200, {**payload, "archive_paused": True}))
            self.assertEqual(self.request("/live/accounts/wrap.near/graph")[:2], (200, {**graph, "archive_paused": True}))
        with self.archive._connection() as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM pages").fetchone()[0], 0)
        current = {"account_id": "wrap.near", "network": "mainnet", "kind": "current"}
        for message in ("Portfolio retention capacity reached; existing observations remain available.",
                        "Portfolio capture budget reached; retry after one minute."):
            with self.subTest(message=message):
                service = live.LiveAPI(portfolio_data=mock.Mock(**{"inspect.return_value": current}),
                                       portfolio_history=mock.Mock(**{"save.side_effect": portfolio.PortfolioCapacityError(message)}))
                self.assertEqual(service.handle("/live/accounts/wrap.near/portfolio", "203.0.113.5", {})[:2],
                                 (200, {**current, "archive_paused": True}))

    def test_malformed_retained_rows_are_not_reused_and_a_tampered_identical_page_answers_503(self):
        pages = {"holdings": {"account_id": "wrap.near", "network": "mainnet", "items": [
                     {"contract_id": "token.near", "balance": "1", "balance_status": "indexed", "last_update_block_height": 1,
                      "metadata": None, "metadata_source": None}],
                     "pagination": {"offset": 0, "limit": 20, "total": 1, "next_offset": None},
                     "source": {"provider": "test-provider"}, "observed_at": "2026-11-10T00:00:00Z"},
                 "transfers": {"account_id": "wrap.near", "network": "mainnet", "items": [
                     {"id": "transfer1", "block_timestamp": "1794308552765806506"}],
                     "source": {"provider": "test-provider"}, "observed_at": "2026-11-10T00:00:00Z"}}
        for kind, payload in pages.items():
            for table in ("history_rows", "pages"):
                with self.subTest(kind=kind, table=table), mock.patch.object(accounts.AccountData, kind, return_value=payload):
                    self.archive.record_page("wrap.near", kind, payload)
                    with self.archive._connection(write=True) as connection:
                        connection.execute("UPDATE " + table + " SET body=? WHERE kind=?", (b"null", kind))
                    status, body, _ = live.LiveAPI(history_store=self.archive).handle("/live/accounts/wrap.near/" + kind, "203.0.113.5", {})
                    if table == "history_rows":
                        self.assertEqual(status, 200)
                        self.assertEqual(self.archive.list_records("wrap.near", kind)["items"], payload["items"])
                    else:
                        self.assertEqual((status, body["error"]), (503, "read_failed"))

    def test_archive_browsing_answers_503_for_a_malformed_retained_row_or_page(self):
        payload = {"account_id": "alice.near", "network": "mainnet", "items": [
            {"id": "transfer1", "block_timestamp": "1794308552765806506"}],
                   "source": {"provider": "test-provider"}, "observed_at": "2026-11-10T00:00:00Z"}
        self.archive.record_page("alice.near", "transfers", payload)
        route = "/live/accounts/alice.near/history?kind=transfers"
        self.assertEqual(self.request(route)[1]["items"], payload["items"])
        with self.archive._connection() as connection:
            row = connection.execute("SELECT body FROM history_rows").fetchone()[0]
            retained = connection.execute("SELECT body FROM pages").fetchone()[0]
        for table, body in (("history_rows", b"null"), ("history_rows", history._canonical({"id": "transfer2", "block_timestamp": "1794308552765806506"})),
                            ("pages", b"null"), ("pages", history._canonical(dict(payload, observed_at="2026-11-09T00:00:00Z"))),
                            ("pages", history._canonical(dict(payload, items=[])))):
            with self.subTest(table=table, body=body):
                with self.archive._connection(write=True) as connection:
                    connection.execute("UPDATE history_rows SET body=?", (row,))
                    connection.execute("UPDATE pages SET body=?", (retained,))
                    connection.execute("UPDATE " + table + " SET body=?", (body,))
                status, answer, _ = self.request(route)
                self.assertEqual((status, answer["error"]), (503, "read_failed"))

    def test_archive_browsing_answers_503_for_a_damaged_cursor_anchor_or_a_retained_testnet_page(self):
        items = [{"id": "transfer" + str(index), "block_timestamp": "179430855276580650" + str(index)} for index in (3, 2, 1)]
        payload = {"account_id": "alice.near", "network": "mainnet", "items": items,
                   "source": {"provider": "test-provider"}, "observed_at": "2026-11-10T00:00:00Z"}
        first = self.archive.record_page("alice.near", "transfers", payload)
        route = "/live/accounts/alice.near/history?kind=transfers"
        cursor = self.request(route + "&limit=1")[1]["pagination"]["next_before"]
        self.assertEqual(self.request(route + "&before=" + str(cursor))[1]["items"], items[1:])
        with self.archive._connection(write=True) as connection:
            anchor = connection.execute("SELECT body,stamp FROM history_rows WHERE seq=?", (cursor,)).fetchone()
            connection.execute("UPDATE history_rows SET body=?,stamp=? WHERE seq=?", (b"null", "0".zfill(20), cursor))
        status, answer, _ = self.request(route + "&before=" + str(cursor))
        self.assertEqual((status, answer["error"]), (503, "read_failed"))
        body = history._canonical(dict(payload, network="testnet"))
        identifier = hashlib.sha256(body).hexdigest()
        with self.archive._connection(write=True) as connection:
            connection.execute("UPDATE history_rows SET body=?,stamp=? WHERE seq=?", (anchor["body"], anchor["stamp"], cursor))
            connection.execute("INSERT INTO pages VALUES (?,?,?,?,?)", (identifier, "alice.near", "transfers", body, 0))
            connection.execute("UPDATE history_rows SET page_id=?", (identifier,))
            connection.execute("DELETE FROM pages WHERE id=?", (first["page_id"],))
        status, answer, _ = self.request(route)
        self.assertEqual((status, answer["error"]), (503, "read_failed"))
        fresh = dict(payload, observed_at="2026-11-10T00:01:00Z")
        with mock.patch.object(accounts.AccountData, "transfers", return_value=fresh):
            self.assertEqual(self.request("/live/accounts/alice.near/transfers")[0], 200)
        status, answer, _ = self.request(route)
        self.assertEqual(status, 200)
        self.assertEqual((answer["network"], answer["items"]), ("mainnet", items))
        self.assertEqual(answer["observations"], [{"page_id": hashlib.sha256(history._canonical(fresh)).hexdigest(),
                                                   "source": payload["source"], "observed_at": "2026-11-10T00:01:00Z"}])


if __name__ == "__main__":
    unittest.main()
