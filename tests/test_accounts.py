import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from gate import rpc
from intel import accounts, labels, portfolio


class Provider:
    def __init__(self):
        self.requests = []
        self.answers = {}

    def client(self, url, mode, **options):
        provider = self

        class Client:
            last_block_height = 123

            def request(self, method, body):
                provider.requests.append((url, mode, method, copy.deepcopy(body), options))
                self.last_source = {"provider": url.split("/")[2], "request_sha256": "a" * 64,
                                    "fetched_at": "2026-11-10T00:00:00Z"}
                result = provider.answers[(url, method)]
                if callable(result):
                    result = result(body)
                if isinstance(result, Exception):
                    raise result
                return copy.deepcopy(result)

            def view_account(self, account):
                return self.request("view_account", account)

            def get(self, path):
                return self.request(path, None)

            def post(self, path, body):
                return self.request(path, body)

            def call_function(self, contract, method, args, **kwargs):
                return self.request(method, {"contract": contract, "args": args, **kwargs})

        return Client()


def transfer(index, *, amount="-1000000000000000000000001", other="bob.near", subject="alice.near"):
    return {"account_id": subject, "other_account_id": other, "amount": amount, "asset_id": "native:near",
            "asset_type": "Near", "block_height": "123", "block_timestamp": "1794308552765806506",
            "transaction_id": "tx" + str(index), "receipt_id": "receipt" + str(index),
            "transfer_index": index, "signer_id": subject, "predecessor_id": subject,
            "receipt_account_id": subject, "transfer_type": "Near", "human_amount": None,
            "usd_amount": None, "method_name": None}


class AccountDataTest(unittest.TestCase):
    def setUp(self):
        tempfile.gettempdir()
        self.env = mock.patch.dict("os.environ", {}, clear=True)
        self.env.start()
        self.addCleanup(self.env.stop)
        cache = mock.patch.dict(accounts._METADATA_CACHE, {}, clear=True)
        cache.start()
        self.addCleanup(cache.stop)
        self.provider = Provider()
        self.data = accounts.AccountData(labels.Store([], "b" * 64), client_factory=self.provider.client,
                                         clock=lambda: 1794268800)

    def test_final_native_balance_is_exact_and_not_taken_from_an_indexer(self):
        amount = "340282366920938463463374607431768211455"
        self.provider.answers[(rpc.MAINNET_RPC_URL, "view_account")] = {
            "amount": amount, "locked": "0", "storage_usage": 120, "code_hash": "codehash",
            "block_height": 123, "block_hash": "blockhash"}
        result = self.data.profile("alice.near")
        self.assertTrue(result["exists"])
        self.assertEqual(result["native"]["amount_yocto"], amount)
        self.assertEqual(result["native"]["block_height"], "123")
        self.assertEqual(result["source"]["read_kind"], "final_rpc")
        self.assertTrue(all(row[1] == "off" for row in self.provider.requests))

    def test_only_unknown_account_is_not_found_and_missing_balance_is_not_zero(self):
        key = (rpc.MAINNET_RPC_URL, "view_account")
        self.provider.answers[key] = rpc.RpcError({"cause": {"name": "UNKNOWN_ACCOUNT"}})
        self.assertEqual(self.data.profile("alice.near")["native"], None)
        self.assertFalse(self.data.profile("alice.near")["exists"])
        self.provider.answers[key] = {"code_hash": "codehash", "locked": "0", "storage_usage": 0,
                                      "block_height": 123, "block_hash": "blockhash"}
        with self.assertRaises(accounts.AccountReadError):
            self.data.profile("alice.near")
        self.provider.answers[key] = rpc.HttpError(429, rpc.MAINNET_RPC_URL)
        with self.assertRaises(accounts.AccountReadError):
            self.data.profile("alice.near")

    def test_holdings_pagination_unknown_balances_and_metadata_failure(self):
        self.provider.answers[(accounts.API_URL, "/v1/account/alice.near/ft")] = {"account_id": "alice.near", "tokens": [
            {"contract_id": "z.near", "balance": "", "last_update_block_height": None},
            {"contract_id": "a.near", "balance": None, "last_update_block_height": 1},
            {"contract_id": "b.near", "balance": "1000000000000000000000001", "last_update_block_height": 123}]}
        self.provider.answers[(rpc.MAINNET_RPC_URL, "ft_metadata")] = rpc.RpcError({"cause": {"name": "CONTRACT_EXECUTION_ERROR"}})
        result = self.data.holdings("alice.near", offset=1, limit=2)
        self.assertEqual([row["contract_id"] for row in result["items"]], ["b.near", "z.near"])
        self.assertEqual(result["items"][0]["balance"], "1000000000000000000000001")
        self.assertEqual(result["items"][0]["metadata"], None)
        self.assertEqual(result["items"][1]["balance"], None)
        self.assertEqual(result["pagination"], {"offset": 1, "limit": 2, "total": 3, "next_offset": None})
        self.assertEqual(result["source"]["read_kind"], "indexed_inventory")
        self.assertEqual(result["coverage"]["metadata_unavailable"], 2)

    def test_metadata_read_preserves_decimals_and_does_not_leak_icons(self):
        self.provider.answers[(accounts.API_URL, "/v1/account/alice.near/ft")] = {"account_id": "alice.near", "tokens": [
            {"contract_id": "wrap.near", "balance": "0", "last_update_block_height": 123}]}
        self.provider.answers[(rpc.MAINNET_RPC_URL, "ft_metadata")] = {
            "name": "Wrapped NEAR", "symbol": "wNEAR", "decimals": 24, "icon": "https://untrusted.example/icon"}
        result = self.data.holdings("alice.near")
        self.assertEqual(result["items"][0]["metadata"], {"name": "Wrapped NEAR", "symbol": "wNEAR", "decimals": 24})
        self.assertEqual(result["items"][0]["balance"], "0")
        self.assertNotIn("untrusted.example", json.dumps(result))

    def test_metadata_cache_reuses_tokens_across_portfolios_until_24_hours(self):
        now = [1794268800]
        self.data.clock = lambda: now[0]
        self.provider.answers[(accounts.API_URL, "/v1/account/alice.near/ft")] = {
            "account_id": "alice.near", "tokens": [
                {"contract_id": "wrap.near", "balance": "1", "last_update_block_height": 123}]}
        self.provider.answers[(rpc.MAINNET_RPC_URL, "ft_metadata")] = {
            "name": "Wrapped NEAR", "symbol": "wNEAR", "decimals": 24}
        price_factory = mock.Mock()
        price_factory.return_value.get.return_value = {"coins": {}}
        data = portfolio.PortfolioData(self.data, client_factory=price_factory, clock=lambda: now[0])
        with mock.patch.object(self.data, "profile", return_value={
                "account_id": "alice.near", "network": "mainnet", "exists": False}):
            first = data.inspect("alice.near")
            first["assets"][1]["metadata"]["symbol"] = "changed"
            now[0] += 86399
            second = data.inspect("alice.near")
            self.assertEqual(second["assets"][1]["metadata"]["symbol"], "wNEAR")
            self.assertEqual(sum(row[2] == "ft_metadata" for row in self.provider.requests), 1)
            now[0] += 1
            data.inspect("alice.near")
        self.assertEqual(sum(row[2] == "ft_metadata" for row in self.provider.requests), 2)

    def test_metadata_cache_is_shared_by_readers_and_preserves_original_source(self):
        self.provider.answers[(accounts.API_URL, "/v1/account/alice.near/ft")] = {
            "account_id": "alice.near", "tokens": [
                {"contract_id": "wrap.near", "balance": "1", "last_update_block_height": 123}]}
        self.provider.answers[(rpc.MAINNET_RPC_URL, "ft_metadata")] = {
            "name": "Wrapped NEAR", "symbol": "wNEAR", "decimals": 24}
        first = self.data.holdings("alice.near")
        other = accounts.AccountData(self.data.store, client_factory=self.provider.client, clock=self.data.clock)
        second = other.holdings("alice.near")
        self.assertEqual(first["items"][0]["metadata_source"], second["items"][0]["metadata_source"])
        self.assertEqual(sum(row[2] == "ft_metadata" for row in self.provider.requests), 1)

    def test_metadata_cache_evicts_least_recently_used_contract_at_bound(self):
        self.provider.answers[(accounts.API_URL, "/v1/account/alice.near/ft")] = {
            "account_id": "alice.near", "tokens": [
                {"contract_id": contract, "balance": "1", "last_update_block_height": 123}
                for contract in ("a.near", "b.near", "c.near")]}
        self.provider.answers[(rpc.MAINNET_RPC_URL, "ft_metadata")] = {
            "name": "Token", "symbol": "T", "decimals": 6}
        with mock.patch.object(accounts, "MAX_METADATA_ENTRIES", 2):
            self.data.holdings("alice.near", limit=2)
            self.data.holdings("alice.near", limit=1)
            self.data.holdings("alice.near", offset=2, limit=1)
            self.assertEqual(len(accounts._METADATA_CACHE), 2)
            self.assertEqual(set(accounts._METADATA_CACHE), {"a.near", "c.near"})
            self.data.holdings("alice.near", offset=1, limit=1)
        self.assertEqual(sum(row[2] == "ft_metadata" for row in self.provider.requests), 4)

    def test_failed_and_malformed_metadata_is_unknown_and_retried(self):
        self.provider.answers[(accounts.API_URL, "/v1/account/alice.near/ft")] = {
            "account_id": "alice.near", "tokens": [
                {"contract_id": "wrap.near", "balance": "1", "last_update_block_height": 123}]}
        key = (rpc.MAINNET_RPC_URL, "ft_metadata")
        for invalid in (rpc.HttpError(503, rpc.MAINNET_RPC_URL),
                        {"name": "Wrapped NEAR", "symbol": "wNEAR", "decimals": True},
                        {"name": "", "symbol": "wNEAR", "decimals": 24}):
            with self.subTest(invalid=invalid):
                self.provider.answers[key] = invalid
                before = len(self.provider.requests)
                for _ in range(2):
                    result = self.data.holdings("alice.near")
                    self.assertIsNone(result["items"][0]["metadata"])
                    self.assertIsNone(result["items"][0]["metadata_source"])
                    self.assertEqual(result["coverage"]["metadata_unavailable"], 1)
                self.assertEqual(sum(row[2] == "ft_metadata" for row in self.provider.requests[before:]), 2)
        self.provider.answers[key] = {"name": "Wrapped NEAR", "symbol": "wNEAR", "decimals": 24}
        self.assertIsNotNone(self.data.holdings("alice.near")["items"][0]["metadata"])

    def test_portfolio_fetches_inventory_once_across_pages_and_refreshes_next_read(self):
        inventory = {"account_id": "alice.near", "tokens": [
            {"contract_id": f"token{i:02}.near", "balance": "1", "last_update_block_height": 123}
            for i in range(21)]}
        path = "/v1/account/alice.near/ft"
        self.provider.answers[(accounts.API_URL, path)] = inventory
        self.provider.answers[(rpc.MAINNET_RPC_URL, "ft_metadata")] = {
            "name": "Token", "symbol": "T", "decimals": 6}
        price_factory = mock.Mock()
        price_factory.return_value.get.return_value = {"coins": {}}
        data = portfolio.PortfolioData(self.data, client_factory=price_factory, clock=self.data.clock)
        with mock.patch.object(self.data, "profile", return_value={
                "account_id": "alice.near", "network": "mainnet", "exists": False}):
            first = data.inspect("alice.near")
            self.assertEqual(first["coverage"]["tokens_read"], 21)
            self.assertTrue(first["coverage"]["inventory_complete"])
            self.assertEqual(sum(row[2] == path for row in self.provider.requests), 1)
            inventory["tokens"][0]["balance"] = "2"
            second = data.inspect("alice.near")
        self.assertEqual(second["assets"][1]["balance_base_units"], "2")
        self.assertEqual(sum(row[2] == path for row in self.provider.requests), 2)

    def test_history_cursor_is_opaque_and_nanoseconds_keep_their_precision(self):
        def answer(body):
            return {"account_txs": [{"account_id": "alice.near", "transaction_hash": "tx1",
                    "tx_block_height": 123, "tx_index": 0, "tx_block_timestamp": "1794308552765806506",
                    **dict.fromkeys(("is_signer", "is_delegated_signer", "is_real_signer", "is_any_signer", "is_predecessor",
                                     "is_explicit_refund_to", "is_receiver", "is_real_receiver", "is_function_call",
                                     "is_action_arg", "is_event_log", "is_success"), False),
                    "is_success": True, "is_receiver": True}],
                    "resume_token": "opaque-next+/=", "txs_count": 456}
        self.provider.answers[(accounts.TX_URL, "/v0/account")] = answer
        result = self.data.transactions("alice.near", cursor="opaque+/=", limit=2)
        self.assertEqual(self.provider.requests[-1][3]["resume_token"], "opaque+/=")
        self.assertEqual(result["items"][0]["tx_block_timestamp"], "1794308552765806506")
        self.assertEqual(result["items"][0]["timestamp_ms"], 1794308552765)
        self.assertEqual(result["pagination"]["next_cursor"], "opaque-next+/=")
        self.assertNotIn("amount", result["items"][0])

    def test_transfer_sign_is_account_relative_and_unknown_counterparty_stays_unknown(self):
        self.provider.answers[(accounts.TRANSFERS_URL, "/v0/transfers")] = {"transfers": [
            transfer(0), transfer(1, amount="2"), transfer(2, other=None)], "resume_token": None}
        result = self.data.transfers("alice.near")
        outgoing, incoming, unknown = result["items"]
        self.assertEqual((outgoing["from"], outgoing["to"]), ("alice.near", "bob.near"))
        self.assertEqual((incoming["from"], incoming["to"]), ("bob.near", "alice.near"))
        self.assertEqual(outgoing["amount"], "1000000000000000000000001")
        self.assertIsNone(outgoing["usd_amount"])
        self.assertIsNone(unknown["to"])
        self.assertEqual(outgoing["block_timestamp"], "1794308552765806506")

    def test_malformed_provider_amount_never_becomes_a_transfer(self):
        for invalid in ("NaN", "-1e24", "-" + str(2 ** 128), True):
            with self.subTest(invalid=invalid):
                self.provider.answers[(accounts.TRANSFERS_URL, "/v0/transfers")] = {
                    "transfers": [transfer(0, amount=invalid)], "resume_token": None}
                with self.assertRaises(accounts.AccountReadError):
                    self.data.transfers("alice.near")

    def test_neighborhood_has_request_record_and_depth_bounds_without_incident_requirements(self):
        def answer(body):
            account = body["account_id"]
            return {"transfers": [transfer(index, subject=account, other=f"child{index}.near") for index in range(20)],
                    "resume_token": "there-is-more"}
        self.provider.answers[(accounts.TRANSFERS_URL, "/v0/transfers")] = answer
        result = self.data.neighborhood("alice.near", depth=2)
        self.assertLessEqual(len(self.provider.requests), accounts.MAX_GRAPH_ACCOUNTS)
        self.assertLessEqual(result["total_records"], accounts.MAX_GRAPH_ACCOUNTS * accounts.GRAPH_PAGE_LIMIT)
        self.assertTrue(result["coverage"]["truncated"])
        self.assertFalse(result["source"]["recorded"])
        self.assertEqual(result["kind"], "flow")
        self.assertEqual(result["root"], "alice.near")
        self.assertTrue(all(node["depth"] <= 2 for node in result["nodes"]))
        self.assertEqual(len({row["id"] for row in result["records"]}), result["total_records"])
        self.assertTrue(all(row["provenance"] == "onchain" for row in result["records"]))

    def test_graph_deduplicates_same_transfer_read_from_both_accounts_and_counts_unknown_routes(self):
        def answer(body):
            if body["account_id"] == "alice.near":
                return {"transfers": [transfer(0), transfer(1, other=None)], "resume_token": None}
            return {"transfers": [transfer(0, subject="bob.near", other="alice.near", amount="1000000000000000000000001")], "resume_token": None}
        self.provider.answers[(accounts.TRANSFERS_URL, "/v0/transfers")] = answer
        result = self.data.neighborhood("alice.near", depth=2)
        self.assertEqual(result["total_records"], 1)
        self.assertEqual(result["coverage"]["unknown_counterparty_rows"], 1)

    def test_invalid_account_page_and_cursor_never_open_network(self):
        for function, args in ((self.data.profile, ("alice.testnet",)), (self.data.profile, ("../alice.near",)),
                               (self.data.transactions, ("alice.near", "x" * 4097)),
                               (self.data.transactions, ("alice.near", None, 101)),
                               (self.data.holdings, ("alice.near", -1)),
                               (self.data.holdings, ("alice.near", 0, 21)),
                               (self.data.neighborhood, ("alice.near", 3))):
            with self.subTest(args=args), self.assertRaises(accounts.AccountInputError):
                function(*args)
        self.assertEqual(self.provider.requests, [])

    def test_provider_missing_cursor_and_oversized_pages_do_not_claim_history_end(self):
        key = (accounts.TRANSFERS_URL, "/v0/transfers")
        for value in ({"transfers": []}, {"transfers": [transfer(0), transfer(1)], "resume_token": None},
                      {"transfers": [], "resume_token": "repeat"}):
            with self.subTest(value=value), self.assertRaises(accounts.AccountReadError):
                self.provider.answers[key] = value
                self.data.transfers("alice.near", cursor="repeat", limit=1)

    def test_holdings_deadline_keeps_indexed_balances_and_stops_metadata_requests(self):
        self.provider.answers[(accounts.API_URL, "/v1/account/alice.near/ft")] = {"account_id": "alice.near", "tokens": [
            {"contract_id": "a.near", "balance": "1", "last_update_block_height": 1},
            {"contract_id": "b.near", "balance": "2", "last_update_block_height": 2}]}
        self.provider.answers[(rpc.MAINNET_RPC_URL, "ft_metadata")] = TimeoutError("deadline")
        result = self.data.holdings("alice.near")
        self.assertEqual([row["balance"] for row in result["items"]], ["1", "2"])
        self.assertEqual(result["coverage"]["metadata_unavailable"], 2)
        self.assertTrue(result["coverage"]["metadata_timed_out"])
        self.assertEqual(sum(row[2] == "ft_metadata" for row in self.provider.requests), 1)

    def test_native_and_history_timeouts_escape_for_the_route_timeout_status(self):
        self.provider.answers[(rpc.MAINNET_RPC_URL, "view_account")] = TimeoutError("deadline")
        self.provider.answers[(accounts.TX_URL, "/v0/account")] = TimeoutError("deadline")
        self.provider.answers[(accounts.TRANSFERS_URL, "/v0/transfers")] = TimeoutError("deadline")
        for operation in (self.data.profile, self.data.transactions, self.data.transfers, self.data.neighborhood):
            with self.subTest(operation=operation.__name__), self.assertRaises(TimeoutError):
                operation("alice.near")

    def test_fastnear_key_is_only_attached_to_approved_fastnear_hosts(self):
        with tempfile.TemporaryDirectory() as directory:
            key = Path(directory) / "key.txt"
            key.write_text("unit-only-token", encoding="utf-8")
            with mock.patch.dict("os.environ", {"FASTNEAR_API_KEY_FILE": str(key),
                                                "NEAR_MAINNET_RPC_URL": "https://other.example"}):
                data = accounts.AccountData(labels.Store([], "b" * 64), client_factory=self.provider.client)
                self.provider.answers[("https://other.example", "view_account")] = rpc.HttpError(503, "https://other.example")
                with self.assertRaises(accounts.AccountReadError):
                    data.profile("alice.near")
                self.assertEqual(self.provider.requests[-1][4]["headers"], {})
            with mock.patch.dict("os.environ", {"FASTNEAR_API_KEY_FILE": str(key)}):
                data = accounts.AccountData(labels.Store([], "b" * 64), client_factory=self.provider.client)
                self.provider.answers[(rpc.MAINNET_RPC_URL, "view_account")] = rpc.HttpError(503, rpc.MAINNET_RPC_URL)
                with self.assertRaises(accounts.AccountReadError):
                    data.profile("alice.near")
                self.assertEqual(self.provider.requests[-1][4]["headers"], {"Authorization": "Bearer unit-only-token"})
            with mock.patch.dict("os.environ", {"FASTNEAR_API_KEY_FILE": str(key),
                                                "NEAR_MAINNET_RPC_URL": "http://api.fastnear.com"}):
                data = accounts.AccountData(labels.Store([], "b" * 64), client_factory=self.provider.client)
                self.provider.answers[("http://api.fastnear.com", "view_account")] = rpc.HttpError(503, "http://api.fastnear.com")
                with self.assertRaises(accounts.AccountReadError):
                    data.profile("alice.near")
                self.assertEqual(self.provider.requests[-1][4]["headers"], {})


if __name__ == "__main__":
    unittest.main()
