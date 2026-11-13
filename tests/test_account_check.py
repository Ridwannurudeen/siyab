import builtins
import importlib
import io
import json
import subprocess
import sys
import tempfile
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
            now_ms=NOW_MS, injection_scanner=None, archival=self.archival, issuers=self.issuers)
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

    def test_import_loads_no_signer_module(self):
        probe = ("import sys, gate.account_check; "
                 "print(sorted({'gate.signer', 'gate.verdict', 'gate.agent'} & set(sys.modules)))")
        result = subprocess.run([sys.executable, "-c", probe], cwd=ROOT, capture_output=True, text=True,
                                check=True)
        self.assertEqual(result.stdout.strip(), "[]")


if __name__ == "__main__":
    unittest.main()
