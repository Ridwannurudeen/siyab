import builtins
import importlib
import io
import json
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from gate import base58, ed25519, evidence, rpc, rules, signer
from intel import feed, labels

ROOT = Path(__file__).resolve().parent.parent
FIXTURES = ROOT / "fixtures" / "receivers" / "rpc"
RHEA = json.loads((ROOT / "intel" / "incidents" / "rhea.json").read_text(encoding="utf-8"))
COLLECTOR = next(a["subject"] for a in RHEA["accounts"] if a["role_as_reported"].startswith("the collector"))
# A recorded mainnet access key of intents.near (fixtures/receivers/rpc).
VALID_KEY = "ed25519:oxEocH88sCavwDRXqoMtryK2Qm9Af72jxMsvsQGHQJx"
NOW_MS = json.loads((ROOT / "fixtures" / "receivers" / "set.json").read_text(encoding="utf-8"))["judged_at_ms"]


def no_network(*args, **kwargs):
    raise AssertionError("the test opened the network")


class AccountCheckTest(unittest.TestCase):
    def setUp(self):
        for target in ("urllib.request.urlopen", "gate.rpc._open_with_deadline", "gate.rpc._fetch"):
            patcher = mock.patch(target, side_effect=no_network)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.store = labels.load()
        self.rpc = rpc.Client(rpc.MAINNET_RPC_URL, "replay", fixtures_dir=FIXTURES)
        self.nearblocks = rpc.Client(rpc.NEARBLOCKS_URL, "replay", kind="http", fixtures_dir=FIXTURES)
        self.archival = rpc.Client(rpc.ARCHIVAL_RPC_URL, "replay", fixtures_dir=FIXTURES)
        self.issuers = rules.issuers_for("mainnet")

    def check(self, account):
        module = importlib.import_module("gate.account_check")
        return module.check_account(account, store=self.store, rpc=self.rpc, nearblocks=self.nearblocks,
                                    archival=self.archival, issuers=self.issuers, now_ms=NOW_MS)

    def document(self, answer):
        document = json.loads(answer["evidence"])
        self.assertEqual(evidence.canonical_bytes(document).decode("utf-8"), answer["evidence"])
        self.assertEqual(evidence.evidence_hash(document), answer["evidence_hash"])
        return document

    def test_rhea_collector_denied_with_quoted_issuer_answer(self):
        answer = self.check(COLLECTOR)
        document = self.document(answer)
        check = next(c for c in document["checks"] if c["id"] == "issuer_blacklist")
        self.assertEqual(answer["outcome"], "DENY")
        self.assertEqual(check["status"], "signal")
        self.assertIn('get_blacklist_status answers "Banned"', check["detail"])
        self.assertTrue(any('get_blacklist_status answers "Banned"' in r for r in answer["reasons"]))
        self.assertIsNotNone(check["source"]["fetched_at"])

    def test_contract_full_access_key_is_unknown_naming_count(self):
        answer = self.check("aggregatedex.near")
        checks = {c["id"]: c for c in self.document(answer)["checks"]}
        self.assertEqual(checks["contract_control"]["status"], "unknown")
        self.assertIn("1 full access key;", checks["contract_control"]["detail"])
        self.assertIn("replace the code", checks["contract_control"]["detail"])
        self.assertEqual(answer["outcome"], "ASK")

    def test_contract_function_call_keys_without_full_access_pass(self):
        answer = self.check("intents.near")
        checks = {c["id"]: c for c in self.document(answer)["checks"]}
        self.assertEqual(checks["contract_control"]["status"], "pass")
        self.assertIn("no full access key", checks["contract_control"]["detail"])
        self.assertEqual(answer["outcome"], "ALLOW")

    def test_contract_empty_key_list_passes(self):
        answer = self.check("ledgerbyfigment.poolv1.near")
        checks = {c["id"]: c for c in self.document(answer)["checks"]}
        self.assertEqual(checks["contract_control"]["status"], "pass")
        self.assertEqual(checks["code_without_metadata"]["status"], "pass")
        self.assertIn("not a signal on its own", checks["code_without_metadata"]["detail"])

    def test_synthetic_unreadable_contract_key_list_is_unknown(self):
        with mock.patch.object(self.rpc, "view_access_key_list", side_effect=rpc.TransportError("unreadable")):
            answer = self.check("intents.near")
        checks = {c["id"]: c for c in self.document(answer)["checks"]}
        self.assertEqual(checks["contract_control"]["status"], "unknown")
        self.assertIn("unreadable", checks["contract_control"]["detail"])
        self.assertEqual(answer["outcome"], "ASK")

    def test_synthetic_malformed_contract_key_entries_are_unknown(self):
        for row in (None, {}, {"access_key": {}}, {"access_key": {"permission": "Other"}},
                    {"access_key": {"permission": {"FunctionCall": None}}}):
            with self.subTest(row=row), mock.patch.object(self.rpc, "view_access_key_list",
                                                         return_value={"keys": [row]}):
                answer = self.check("intents.near")
                checks = {c["id"]: c for c in self.document(answer)["checks"]}
                self.assertEqual(checks["contract_control"]["status"], "unknown")
                self.assertEqual(answer["outcome"], "ASK")

    TOO_MANY = rpc.RpcError({"name": "HANDLER_ERROR", "cause": {"name": "TOO_MANY_ACCESS_KEYS", "info": {
        "block_height": 100, "requested_account_id": "intents.near", "limit": 100}}})

    @staticmethod
    def key(index, full=False):
        permission = "FullAccess" if full else {"FunctionCall": {
            "allowance": None, "receiver_id": "intents.near", "method_names": []}}
        return {"public_key": "ed25519:" + base58.encode(bytes([index]) * 32),
                "access_key": {"nonce": 0, "permission": permission}}

    @staticmethod
    def page(keys, block=100, last=True):
        page = {"keys": keys, "block_height": block}
        if last:
            page["last_key"] = keys[-1]["public_key"]
        return page

    def check_paged(self, *pages):
        with (mock.patch.object(self.rpc, "view_access_key_list", side_effect=self.TOO_MANY),
              mock.patch.object(self.rpc, "view_access_key_page", side_effect=list(pages)) as paged):
            answer = self.check("intents.near")
        return {c["id"]: c for c in self.document(answer)["checks"]}["contract_control"], answer, paged

    def test_synthetic_too_many_keys_stops_at_the_first_page_with_a_full_access_key(self):
        control, answer, paged = self.check_paged(self.page([self.key(1), self.key(2)]),
                                                  self.page([self.key(3), self.key(4, full=True)]))
        self.assertEqual(control["status"], "unknown")
        self.assertEqual(control["detail"], "at least 1 full access key among the first 4 keys of the paged "
                         "access key list; whoever holds one can replace the code")
        first = "ed25519:" + base58.encode(bytes([2]) * 32)
        self.assertEqual(paged.call_args_list, [mock.call("intents.near", after_key=None, block_id=None),
                                                mock.call("intents.near", after_key=first, block_id=100)])
        self.assertEqual(answer["outcome"], "ASK")

    def test_synthetic_too_many_keys_with_no_full_access_key_in_a_complete_list_passes(self):
        control, answer, paged = self.check_paged(self.page([self.key(index) for index in range(100)]),
                                                  self.page([self.key(100)], last=False))
        self.assertEqual(control["status"], "pass")
        self.assertEqual(control["detail"], "contract code with no full access key among the 101 keys of the "
                         "complete paged access key list")
        self.assertEqual(paged.call_count, 2)
        self.assertEqual(answer["outcome"], "ALLOW")

    def test_synthetic_too_many_keys_with_an_invalid_refusal_limit_is_unknown(self):
        for info in ({}, {"limit": "100"}, {"limit": True}, {"limit": False},
                     {"limit": -1}, {"limit": 0}, {"limit": 1.5}):
            error = rpc.RpcError({"name": "HANDLER_ERROR", "cause": {
                "name": "TOO_MANY_ACCESS_KEYS", "info": {
                    "block_height": 100, "requested_account_id": "intents.near", **info}}})
            with self.subTest(info=info), mock.patch.object(self, "TOO_MANY", error):
                control, answer, paged = self.check_paged(self.page([self.key(1)], last=False))
                self.assertEqual(control["status"], "unknown")
                self.assertIn("refusal has no positive integer limit", control["detail"])
                self.assertEqual(answer["outcome"], "ASK")
                paged.assert_not_called()

    def test_synthetic_too_many_keys_with_a_list_no_longer_than_the_refused_limit_is_unknown(self):
        for pages in ((self.page([self.key(1), self.key(2)]), self.page([self.key(3)], last=False)),
                      (self.page([self.key(index) for index in range(100)], last=False),),
                      (self.page([self.key(1)]), {"keys": [], "block_height": 100})):
            with self.subTest(read=sum(len(page["keys"]) for page in pages)):
                control, answer, _ = self.check_paged(*pages)
                read = sum(len(page["keys"]) for page in pages)
                self.assertEqual(control["status"], "unknown")
                self.assertEqual(control["detail"], f"the paged access key list ends after {read} keys, but RPC "
                                 "refused the whole list as holding more than 100, so it was not read in full")
                self.assertEqual(answer["outcome"], "ASK")

    def test_synthetic_the_paged_read_runs_under_its_own_time_budget(self):
        module = importlib.import_module("gate.account_check")
        budgets = []

        def page(account_id, *, after_key, block_id):
            budgets.append(rpc._REQUEST_DEADLINE.get() - time.monotonic())
            raise TimeoutError("request deadline exceeded")

        with (mock.patch.object(self.rpc, "view_access_key_list", side_effect=self.TOO_MANY),
              mock.patch.object(self.rpc, "view_access_key_page", side_effect=page)):
            answer = self.check("intents.near")
        control = {c["id"]: c for c in self.document(answer)["checks"]}["contract_control"]
        self.assertEqual(len(budgets), 1)
        self.assertTrue(0 < budgets[0] <= module.KEY_PAGES_S, budgets)
        self.assertEqual(control["status"], "unknown")
        self.assertEqual(control["detail"], "could not read the paged access key list: TimeoutError: request "
                         "deadline exceeded")
        self.assertEqual(answer["outcome"], "ASK")

    def test_synthetic_too_many_keys_past_the_page_cap_is_unknown(self):
        module = importlib.import_module("gate.account_check")
        pages = [self.page([self.key(index)]) for index in range(1, module.KEY_PAGES_MAX + 2)]
        control, answer, paged = self.check_paged(*pages)
        self.assertEqual(control["status"], "unknown")
        self.assertEqual(control["detail"], f"no full access key among the first {module.KEY_PAGES_MAX} keys of "
                         "the paged access key list, and the rest was not read")
        self.assertEqual(paged.call_count, module.KEY_PAGES_MAX)
        self.assertEqual(answer["outcome"], "ASK")

    def test_synthetic_too_many_keys_with_an_unreadable_or_inconsistent_page_is_unknown(self):
        cases = (
            (rpc.TransportError("unreadable"), "TransportError: unreadable"),
            (self.page([self.key(1), self.key(2)], block=101), "MalformedResponse: a page of the access key list "
             "was read at block 101, not block 100"),
            ({"keys": [self.key(3)], "block_height": 100, "last_key": "ed25519:" + base58.encode(bytes([9]) * 32)},
             "MalformedResponse: the page cursor is not the last key of its page"),
            (self.page([self.key(1)]), "MalformedResponse: a page of the access key list repeats the key it "
             "was read after"),
        )
        for second, why in cases:
            with self.subTest(second=second):
                first = self.page([self.key(1)])
                control, answer, _ = self.check_paged(first, second)
                self.assertEqual(control["status"], "unknown")
                self.assertEqual(control["detail"], f"could not read the paged access key list: {why}")
                self.assertEqual(answer["outcome"], "ASK")

    def test_synthetic_other_key_list_errors_read_no_page(self):
        error = rpc.RpcError({"name": "HANDLER_ERROR", "cause": {"name": "UNKNOWN_ACCOUNT", "info": {
            "block_height": 100, "requested_account_id": "intents.near"}}})
        with (mock.patch.object(self.rpc, "view_access_key_list", side_effect=error),
              mock.patch.object(self.rpc, "view_access_key_page") as paged):
            answer = self.check("intents.near")
        control = {c["id"]: c for c in self.document(answer)["checks"]}["contract_control"]
        paged.assert_not_called()
        self.assertEqual(control["detail"], "could not read the access key list: RPC error UNKNOWN_ACCOUNT")

    def test_the_paged_key_list_request_names_its_limit_cursor_and_block(self):
        client = rpc.Client(rpc.MAINNET_RPC_URL, "replay", fixtures_dir=FIXTURES)
        with mock.patch.object(client, "call", return_value={"keys": []}) as call:
            client.view_access_key_page("near")
            client.view_access_key_page("near", after_key=VALID_KEY, block_id=7)
        self.assertEqual(call.call_args_list, [
            mock.call("query", {"request_type": "view_access_key_list", "account_id": "near", "limit": 100,
                                "finality": "final"}),
            mock.call("query", {"request_type": "view_access_key_list", "account_id": "near", "limit": 100,
                                "block_id": 7, "after_key": VALID_KEY}),
        ])
        for block_id in (-1, True, "7", 7.0):
            with self.subTest(block_id=block_id), self.assertRaises(ValueError):
                client.view_access_key_page("near", block_id=block_id)

    def test_a_recorded_page_with_a_cursor_passes_the_response_schema(self):
        keys = [self.key(1), self.key(2)]
        params = {"request_type": "view_access_key_list", "account_id": "near", "limit": 100, "finality": "final"}
        response = {"jsonrpc": "2.0", "id": "gate", "result": {
            "keys": keys, "block_height": 100, "block_hash": "11111111111111111111111111111111",
            "last_key": keys[-1]["public_key"]}}
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / (rpc.fixture_key(rpc.MAINNET_RPC_URL, "query", params) + ".json")
            path.write_text(json.dumps({"request": {"method": "query", "params": params}, "response": response,
                                        "url": rpc.MAINNET_RPC_URL, "fetched_at": "2026-11-15T00:00:00Z"}),
                            encoding="utf-8")
            page = rpc.Client(rpc.MAINNET_RPC_URL, "replay", fixtures_dir=directory).view_access_key_page("near")
        self.assertEqual(page["last_key"], keys[-1]["public_key"])
        self.assertEqual(page["keys"], keys)

    def test_recorded_incident_code_match(self):
        account = next(a["subject"] for a in RHEA["accounts"]
                       if a["role_as_reported"].startswith("one of the four token contracts"))
        params = {"request_type": "view_account", "finality": "final", "account_id": account}
        path = rpc.FIXTURES_DIR / f"{rpc.fixture_key(rpc.MAINNET_RPC_URL, 'query', params)}.json"
        recorded = json.loads(path.read_text(encoding="utf-8"))
        reported = RHEA["code_hash_as_reported"]["value"]
        self.assertEqual(recorded["response"]["result"]["code_hash"], reported)
        self.rpc = rpc.Client(rpc.MAINNET_RPC_URL, "replay", fixtures_dir=rpc.FIXTURES_DIR)
        answer = self.check(account)
        checks = {c["id"]: c for c in self.document(answer)["checks"]}
        self.assertEqual(checks["receiver_exists"]["status"], "pass")
        self.assertEqual(checks["incident_code"]["status"], "signal")
        self.assertIn(reported, checks["incident_code"]["detail"])
        self.assertIn("rhea", checks["incident_code"]["detail"])
        self.assertEqual(checks["contract_control"]["status"], "unknown")
        self.assertIn("no recorded response", checks["contract_control"]["detail"])
        self.assertEqual(answer["outcome"], "DENY")

    def test_synthetic_incident_code_match_signals_even_with_unknown_control(self):
        view_account = self.rpc.view_account
        reported = RHEA["code_hash_as_reported"]["value"]

        def synthetic_account(account):
            return {**view_account(account), "code_hash": reported}

        with mock.patch.object(self.rpc, "view_account", side_effect=synthetic_account):
            answer = self.check("aggregatedex.near")
        checks = {c["id"]: c for c in self.document(answer)["checks"]}
        self.assertEqual(checks["incident_code"]["status"], "signal")
        self.assertIn(reported, checks["incident_code"]["detail"])
        self.assertIn("rhea", checks["incident_code"]["detail"])
        self.assertEqual(checks["contract_control"]["status"], "unknown")
        self.assertEqual(answer["outcome"], "DENY")
        self.assertTrue(any(reported in reason for reason in answer["reasons"]))
        self.assertFalse(any("Every check ran" in reason for reason in answer["reasons"]))

    def test_recorded_global_contract_account_has_code(self):
        answer = self.check("dvzat.sputnik-dao.near")
        checks = {c["id"]: c for c in self.document(answer)["checks"]}
        self.assertIn("contract_control", checks)
        self.assertNotIn("no contract code", answer["evidence"])
        self.assertIn("DtHZA1mUmGYeScEaUoUZYQGGFHSMpnjMCvkr5U9hJXuF", checks["incident_code"]["detail"])

    def test_synthetic_global_contract_hash_matches_incident(self):
        view_account = self.rpc.view_account
        reported = RHEA["code_hash_as_reported"]["value"]

        def synthetic_account(account):
            return {**view_account(account), "global_contract_hash": reported}

        with mock.patch.object(self.rpc, "view_account", side_effect=synthetic_account):
            answer = self.check("dvzat.sputnik-dao.near")
        checks = {c["id"]: c for c in self.document(answer)["checks"]}
        self.assertEqual(checks["incident_code"]["status"], "signal")
        self.assertIn(reported, checks["incident_code"]["detail"])
        self.assertEqual(answer["outcome"], "DENY")

    def test_synthetic_global_contract_by_account_id_is_unknown(self):
        view_account = self.rpc.view_account

        def synthetic_account(account):
            return {**view_account(account), "global_contract_hash": None,
                    "global_contract_account_id": "publisher.near"}

        with mock.patch.object(self.rpc, "view_account", side_effect=synthetic_account):
            answer = self.check("dvzat.sputnik-dao.near")
        checks = {c["id"]: c for c in self.document(answer)["checks"]}
        self.assertEqual(checks["contract_control"]["status"], "unknown")
        self.assertEqual(checks["incident_code"]["status"], "unknown")
        self.assertEqual(checks["incident_code"]["detail"],
                         "runs the global contract published by publisher.near; its hash was not compared")
        self.assertNotEqual(answer["outcome"], "ALLOW")

    def test_synthetic_later_account_read_cannot_erase_incident_match(self):
        view_account = self.rpc.view_account
        reported = RHEA["code_hash_as_reported"]["value"]
        reads = []

        def changing_account(account):
            first = view_account(account)
            reads.append(account)
            if len(reads) == 1:
                return {**first, "code_hash": reported}
            return {**first, "code_hash": rules.EMPTY_CODE_HASH, "block_height": first["block_height"] + 10}

        with mock.patch.object(self.rpc, "view_account", side_effect=changing_account):
            answer = self.check("intents.near")
        document = self.document(answer)
        checks = {c["id"]: c for c in document["checks"]}
        self.assertEqual(reads, ["intents.near"])
        self.assertEqual(checks["incident_code"]["status"], "signal")
        self.assertIn("contract_control", checks)
        self.assertEqual(answer["outcome"], "DENY")
        self.assertEqual(answer["freshness"]["block_height"], view_account("intents.near")["block_height"])

    def test_synthetic_invalid_code_hash_is_unknown(self):
        view_account = self.rpc.view_account
        for code_hash in ("", "0OIl", base58.encode(bytes(31)), base58.encode(b"\x01" * 33)):
            with self.subTest(code_hash=code_hash), mock.patch.object(
                    self.rpc, "view_account", side_effect=lambda account: {**view_account(account),
                                                                            "code_hash": code_hash}):
                answer = self.check("intents.near")
                checks = {c["id"]: c for c in self.document(answer)["checks"]}
                self.assertEqual(checks["incident_code"]["status"], "unknown")
                self.assertIn("not 32 bytes of base58", checks["incident_code"]["detail"])
                self.assertEqual(answer["outcome"], "ASK")

    def test_synthetic_incomplete_access_key_entries_are_unknown(self):
        call = {"allowance": None, "receiver_id": "intents.near", "method_names": ["execute_intents"]}
        rows = (
            {"access_key": {"nonce": 1, "permission": {"FunctionCall": {}}}},
            {"access_key": {"nonce": 1, "permission": {"FunctionCall": call}}},
            {"public_key": VALID_KEY, "access_key": {"permission": {"FunctionCall": call}}},
            {"public_key": VALID_KEY, "access_key": {"nonce": "1", "permission": {"FunctionCall": call}}},
            {"public_key": VALID_KEY, "access_key": {"nonce": True, "permission": "FullAccess"}},
            {"public_key": "", "access_key": {"nonce": 1, "permission": {"FunctionCall": call}}},
            {"public_key": "garbage", "access_key": {"nonce": 1, "permission": {"FunctionCall": call}}},
            {"public_key": "ed25519:x", "access_key": {"nonce": 1, "permission": {"FunctionCall": call}}},
            {"public_key": "ed25519:0OIl", "access_key": {"nonce": 1, "permission": {"FunctionCall": call}}},
            {"public_key": base58.encode_key(bytes(range(1, 65))),
             "access_key": {"nonce": 1, "permission": {"FunctionCall": call}}},
            {"public_key": "secp256k1:" + base58.encode(bytes(range(1, 33))),
             "access_key": {"nonce": 1, "permission": {"FunctionCall": call}}},
            {"public_key": "rsa:" + base58.encode(bytes(range(1, 33))),
             "access_key": {"nonce": 1, "permission": {"FunctionCall": call}}},
            {"public_key": VALID_KEY, "access_key": {"nonce": 1, "permission": {"FunctionCall": {
                **call, "receiver_id": "!"}}}},
            {"public_key": VALID_KEY, "access_key": {"nonce": 1, "permission": {"FunctionCall": {
                **call, "receiver_id": "Intents.near"}}}},
            {"public_key": VALID_KEY, "access_key": {"nonce": 1, "permission": {"FunctionCall": {
                "receiver_id": "intents.near", "method_names": []}}}},
            {"public_key": VALID_KEY, "access_key": {"nonce": 1, "permission": {"FunctionCall": {
                **call, "allowance": 5}}}},
            {"public_key": VALID_KEY, "access_key": {"nonce": 1, "permission": {"FunctionCall": {
                **call, "method_names": "execute_intents"}}}},
            {"public_key": VALID_KEY, "access_key": {"nonce": 1, "permission": {"FunctionCall": {
                "allowance": None, "method_names": []}}}},
        )
        for row in rows:
            with self.subTest(row=row), mock.patch.object(self.rpc, "view_access_key_list",
                                                         return_value={"keys": [row]}):
                answer = self.check("intents.near")
                checks = {c["id"]: c for c in self.document(answer)["checks"]}
                self.assertEqual(checks["contract_control"]["status"], "unknown")
                self.assertEqual(answer["outcome"], "ASK")

    def test_synthetic_complete_function_call_entry_passes(self):
        for public_key in (VALID_KEY, "secp256k1:" + base58.encode(bytes(range(1, 65)))):
            row = {"public_key": public_key, "access_key": {"nonce": 1, "permission": {"FunctionCall": {
                "allowance": "250000000000000000000000", "receiver_id": "intents.near", "method_names": []}}}}
            with self.subTest(public_key=public_key), mock.patch.object(self.rpc, "view_access_key_list",
                                                                       return_value={"keys": [row]}):
                answer = self.check("intents.near")
                checks = {c["id"]: c for c in self.document(answer)["checks"]}
                self.assertEqual(checks["contract_control"]["status"], "pass")

    def check_with_corpus(self, pages, code_hash):
        """The account check of intents.near holding code_hash, over a synthetic incident directory of
        pages ({file name: document or raw text}, or None for an absent directory)."""
        view_account = self.rpc.view_account
        with tempfile.TemporaryDirectory() as directory:
            incidents = Path(directory) / "incidents"
            if pages is not None:
                incidents.mkdir()
                for name, page in pages.items():
                    (incidents / name).write_text(page if isinstance(page, str) else json.dumps(page),
                                                  encoding="utf-8")
            with mock.patch.object(feed, "INCIDENTS_DIR", incidents), \
\
                    mock.patch.object(self.rpc, "view_account",
                                      side_effect=lambda account: {**view_account(account), "code_hash": code_hash}):
                answer = self.check("intents.near")
        return answer, {c["id"]: c for c in self.document(answer)["checks"]}

    def test_synthetic_missing_or_unreadable_incident_corpus_is_unknown(self):
        reported = RHEA["code_hash_as_reported"]["value"]
        for name, pages in (("absent", None), ("empty", {}), ("broken", {"broken.json": "{"}),
                            ("not object", {"list.json": "[]"})):
            with self.subTest(corpus=name):
                answer, checks = self.check_with_corpus(pages, reported)
                self.assertEqual(checks["incident_code"]["status"], "unknown")
                self.assertIn("was not compared", checks["incident_code"]["detail"])
                self.assertEqual(answer["outcome"], "ASK")

    def test_synthetic_mixed_corpus_with_malformed_reported_hash_is_unknown(self):
        reported = RHEA["code_hash_as_reported"]["value"]
        other = {"id": "synthetic-other", "code_hash_as_reported": {"value": base58.encode(bytes(range(1, 33)))}}
        malformed_pages = [{"id": "rhea", "code_hash_as_reported": field}
                           for field in ([], {}, None, "", 0, {"value": ""}, {"value": None}, {"value": []},
                                         {"value": 5}, {"value": "0OIl"}, {"value": base58.encode(bytes(31))})]
        malformed_pages += [{"code_hash_as_reported": RHEA["code_hash_as_reported"]},
                            {"id": "", "code_hash_as_reported": RHEA["code_hash_as_reported"]},
                            "[]", "{"]
        for page in malformed_pages:
            with self.subTest(page=page):
                answer, checks = self.check_with_corpus({"other.json": other, "rhea.json": page}, reported)
                self.assertEqual(checks["incident_code"]["status"], "unknown")
                self.assertIn("was not compared", checks["incident_code"]["detail"])
                self.assertEqual(answer["outcome"], "ASK")

    def test_synthetic_valid_match_survives_a_malformed_page(self):
        reported = RHEA["code_hash_as_reported"]["value"]
        rhea = {"id": "rhea", "code_hash_as_reported": {"value": reported}}
        for page in ("{", "[]", {"id": "synthetic-bad", "code_hash_as_reported": []},
                     {"id": "synthetic-bad", "code_hash_as_reported": {"value": ""}}):
            with self.subTest(page=page):
                answer, checks = self.check_with_corpus({"bad.json": page, "rhea.json": rhea}, reported)
                self.assertEqual(checks["incident_code"]["status"], "signal")
                self.assertIn("rhea", checks["incident_code"]["detail"])
                self.assertEqual(answer["outcome"], "DENY")

    def test_synthetic_clean_corpus_with_pages_reporting_no_hash_passes(self):
        other = {"id": "synthetic-other", "code_hash_as_reported": {"value": base58.encode(bytes(range(1, 33)))}}
        answer, checks = self.check_with_corpus({"other.json": other, "plain.json": {"id": "plain"}},
                                                RHEA["code_hash_as_reported"]["value"])
        self.assertEqual(checks["incident_code"]["status"], "pass")
        self.assertEqual(answer["outcome"], "ALLOW")

    def test_synthetic_invalid_reported_incident_hash_is_unknown(self):
        reported = RHEA["code_hash_as_reported"]["value"]
        valid_other = base58.encode(bytes(range(1, 33)))
        other = {"id": "synthetic", "code_hash_as_reported": {"value": valid_other}}
        for replacement in ("", None):
            rhea = {"id": "rhea", "code_hash_as_reported": {"value": replacement}}
            for account_hash, expected in ((reported, "unknown"), (valid_other, "signal")):
                with self.subTest(replacement=replacement, account_hash=account_hash):
                    answer, checks = self.check_with_corpus({"other.json": other, "rhea.json": rhea}, account_hash)
                    self.assertEqual(checks["incident_code"]["status"], expected)
                    self.assertEqual(answer["outcome"], "ASK" if expected == "unknown" else "DENY")

    def test_synthetic_nonce_and_allowance_protocol_bounds(self):
        def row(nonce, allowance):
            return {"public_key": VALID_KEY, "access_key": {"nonce": nonce, "permission": {"FunctionCall": {
                "allowance": allowance, "receiver_id": "intents.near", "method_names": []}}}}

        cases = ((row(0, "0"), "pass"), (row(2**64 - 1, None), "pass"), (row(1, str(2**128 - 1)), "pass"),
                 (row(2**64, None), "unknown"), (row(-1, None), "unknown"),
                 (row(1, str(2**128)), "unknown"), (row(1, "+1"), "unknown"), (row(1, "-1"), "unknown"),
                 (row(1, " 1"), "unknown"), (row(1, ""), "unknown"))
        for key, expected in cases:
            with self.subTest(key=key), mock.patch.object(self.rpc, "view_access_key_list",
                                                         return_value={"keys": [key]}):
                answer = self.check("intents.near")
                checks = {c["id"]: c for c in self.document(answer)["checks"]}
                self.assertEqual(checks["contract_control"]["status"], expected)
                self.assertEqual(answer["outcome"], "ALLOW" if expected == "pass" else "ASK")

    def test_synthetic_unreadable_account_does_not_pass_incident_code(self):
        self.rpc.last_source = rules._local("synthetic account read")
        with mock.patch.object(self.rpc, "view_account", side_effect=rpc.TransportError("unreadable")):
            answer = self.check("intents.near")
        checks = {c["id"]: c for c in self.document(answer)["checks"]}
        self.assertEqual(checks["incident_code"]["status"], "unknown")
        self.assertEqual(answer["outcome"], "ASK")

    def test_no_contract_omits_contract_control(self):
        answer = self.check(COLLECTOR)
        checks = {c["id"]: c for c in self.document(answer)["checks"]}
        self.assertNotIn("contract_control", checks)
        self.assertEqual(checks["incident_code"]["status"], "pass")

    def test_exact_answer_evidence_subject_and_unsigned_fields(self):
        answer = self.check("intents.near")
        self.assertEqual(set(answer), {"evidence", "evidence_hash", "checks", "reasons", "outcome", "action",
                                       "verdict", "signing", "freshness", "latency_ms"})
        document = self.document(answer)
        self.assertEqual(document["subject"], {"network": "mainnet", "kind": "account", "account": "intents.near"})
        self.assertEqual(answer["checks"], [{"name": c["id"], "status": c["status"], "detail": c["detail"]}
                                            for c in document["checks"]])
        self.assertEqual(answer["freshness"], document["observed"])
        self.assertEqual(answer["reasons"], document["reasons"])
        self.assertEqual(answer["latency_ms"], document["latency_ms"])
        self.assertIsNone(answer["verdict"])
        self.assertEqual(answer["signing"], "never")
        for field in ("issued_ms", "expires_ms", "verdict_id"):
            self.assertIsNone(document[field])
        for check in document["checks"]:
            self.assertEqual(set(check), {"id", "status", "detail", "source"})
            self.assertEqual(set(check["source"]), {"provider", "request_sha256", "fetched_at"})
            self.assertIn(check["status"], evidence.STATUSES)

    def test_public_judge_receives_nominal_native_request_and_empty_policy(self):
        with mock.patch.object(rules, "judge", wraps=rules.judge) as judge:
            answer = self.check("intents.near")
        judge.assert_called_once_with(
            {"kind": "near", "token_id": None, "receiver_id": "intents.near",
             "amount": str(rules.YOCTO_PER_NEAR), "memo": None},
            {"allowlist": [], "caps": []}, self.store, mock.ANY, self.nearblocks,
            now_ms=NOW_MS, injection_scanner=None, archival=self.archival, issuers=self.issuers, rules_version=1)
        self.assertEqual(judge.call_args.args[3].url, self.rpc.url)
        original = rules.judge(*judge.call_args.args, **judge.call_args.kwargs)
        kept = [c for c in original.checks if c["id"] != "memo_injection"]
        document = self.document(answer)
        self.assertEqual(document["checks"][:len(kept)], kept)
        self.assertEqual(document["labels"], original.labels)
        self.assertNotIn("memo_injection", [c["id"] for c in document["checks"]])

    def test_invalid_and_testnet_accounts_rejected_before_reads(self):
        for account in ("", "Bad.near", "testnet", "example.testnet"):
            with self.subTest(account=account), mock.patch.object(self.rpc, "view_account") as view:
                with self.assertRaises(ValueError):
                    self.check(account)
                view.assert_not_called()

    def test_no_signer_calls_and_no_filesystem_writes(self):
        calls = []
        original_open, original_io_open = builtins.open, io.open

        def read_only_open(original, file, mode="r", *args, **kwargs):
            if any(flag in mode for flag in "wax+"):
                raise AssertionError("the account check wrote a file")
            return original(file, mode, *args, **kwargs)

        def profile(frame, event, arg):
            if event == "call":
                calls.append(frame.f_globals.get("__name__", ""))

        module = importlib.import_module("gate.account_check")
        previous = sys.getprofile()
        refuse = AssertionError("the account check called a signer")
        with mock.patch("builtins.open", side_effect=lambda *a, **k: read_only_open(original_open, *a, **k)), \
                mock.patch("io.open", side_effect=lambda *a, **k: read_only_open(original_io_open, *a, **k)), \
                mock.patch.object(Path, "mkdir", side_effect=AssertionError("the account check made a directory")), \
                mock.patch.object(evidence, "write", side_effect=AssertionError("the account check wrote evidence")), \
                mock.patch.object(signer.TestSigner, "sign", side_effect=refuse), \
                mock.patch.object(signer.NaClSigner, "sign", side_effect=refuse), \
                mock.patch.object(signer.PurePythonLiveSigner, "sign", side_effect=refuse), \
                mock.patch.object(ed25519, "sign", side_effect=refuse), \
                mock.patch.object(ed25519, "signature", side_effect=refuse):
            sys.setprofile(profile)
            try:
                answer = module.check_account("intents.near", store=self.store, rpc=self.rpc,
                                              nearblocks=self.nearblocks, archival=self.archival,
                                              issuers=self.issuers, now_ms=NOW_MS)
            finally:
                sys.setprofile(previous)
        self.assertEqual(answer["signing"], "never")
        self.assertFalse(set(calls) & {"gate.signer", "gate.verdict", "gate.agent", "gate.ed25519"})

    def test_rules_version_2_keeps_its_checks_and_version_1_stays_the_default(self):
        module = importlib.import_module("gate.account_check")
        default = self.check("intents.near")
        kwargs = {"store": self.store, "rpc": self.rpc, "nearblocks": self.nearblocks, "archival": self.archival,
                  "issuers": self.issuers, "now_ms": NOW_MS}
        first = module.check_account("intents.near", rules_version=1, **kwargs)
        timeless = ("latency_ms", "evidence", "evidence_hash")
        self.assertEqual({key: value for key, value in default.items() if key not in timeless},
                         {key: value for key, value in first.items() if key not in timeless})
        self.assertEqual({**self.document(default), "latency_ms": 0}, {**self.document(first), "latency_ms": 0})
        self.assertNotIn("code_changed_recently", {c["name"] for c in default["checks"]})
        answer = module.check_account("intents.near", rules_version=2, **kwargs)
        checks = {c["id"]: c for c in self.document(answer)["checks"]}
        # No archival view_account of intents.near at the old block is recorded, so the check cannot run.
        self.assertEqual(checks["code_changed_recently"]["status"], "unknown")
        self.assertEqual(answer["outcome"], "ASK")
        self.assertIn(f"code_changed_recently could not run: {checks['code_changed_recently']['detail']}.",
                      answer["reasons"])

    def test_import_loads_no_signer_module(self):
        probe = ("import sys, gate.account_check; "
                 "print(sorted({'gate.signer', 'gate.verdict', 'gate.agent'} & set(sys.modules)))")
        result = subprocess.run([sys.executable, "-c", probe], cwd=ROOT, capture_output=True, text=True,
                                check=True)
        self.assertEqual(result.stdout.strip(), "[]")


if __name__ == "__main__":
    unittest.main()
