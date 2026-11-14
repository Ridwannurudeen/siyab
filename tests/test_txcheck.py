import ast
import base64
import copy
import dataclasses
import hashlib
import json
import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock

from gate import evidence
from gate.txcheck import check_transaction
from intel import labels

ROOT = Path(__file__).resolve().parent.parent
PUBLIC_KEY = "ed25519:" + "1" * 32
RECORDED_RUN = ROOT / "fixtures/runs/gate-022-transactions-20261109.json"
SIGNER_MODULES = ("gate.signer", "gate.ed25519", "gate.verdict", "gate.agent", "gate.outlayer", "gate.http",
                  "gate.demo", "nacl", "py_near")


def synthetic_transaction(*actions, receiver="recipient.near"):
    return {"signer_id": "sender.near", "receiver_id": receiver, "actions": list(actions)}


def synthetic_call(method, receiver="recipient.near"):
    args = {"receiver_id": receiver, "amount": "1"}
    if method == "ft_transfer_call":
        args["msg"] = ""
    return {"FunctionCall": {"method_name": method, "args": base64.b64encode(
        json.dumps(args).encode()).decode(), "gas": 30000000000000, "deposit": "1"}}


def recorded_mainnet():
    """The one recorded mainnet issuer response whose name starts with the prefix."""
    matches = sorted((ROOT / "fixtures/issuers").glob("0e842edf*.json"))
    assert len(matches) == 1, matches
    return json.loads(matches[0].read_text(encoding="utf-8"))


def recorded_actions(name):
    """The actions of one recorded testnet transaction of the Gate run."""
    return json.loads(RECORDED_RUN.read_text(encoding="utf-8"))["transactions"][name]["transaction"]["actions"]


def synthetic_store(*rows):
    assertions = [labels.Assertion(
        id=str(index) * 64, entity="synthetic", subject=subject, chain="near", label="synthetic label",
        category=category, provenance="third_party", source={}, observed_at="2026-11-13T00:00:00Z",
        confidence=1, rule_id=None, superseded_by=None, note="synthetic")
        for index, (subject, category) in enumerate(rows, 1)]
    return labels.Store(assertions, "5" * 64)


class TransactionCheckTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.store = labels.load()

    def setUp(self):
        for target in ("urllib.request.urlopen", "gate.rpc._open_with_deadline", "gate.rpc._fetch"):
            patch = mock.patch(target, side_effect=AssertionError("network is forbidden"))
            patch.start()
            self.addCleanup(patch.stop)

    def document(self, transaction, store=None):
        answer = check_transaction(transaction, store=self.store if store is None else store)
        self.assertEqual(set(answer), {"evidence", "evidence_hash", "checks", "reasons", "outcome",
                                      "action", "verdict", "signing", "freshness", "latency_ms"})
        self.assertEqual(answer["signing"], "never")
        self.assertIsNone(answer["verdict"])
        document = json.loads(answer["evidence"])
        self.assertEqual(evidence.canonical_bytes(document).decode(), answer["evidence"])
        self.assertEqual(evidence.evidence_hash(document), answer["evidence_hash"])
        self.assertEqual(answer["checks"], [{"name": c["id"], "status": c["status"], "detail": c["detail"]}
                                            for c in document["checks"]])
        for check in document["checks"]:
            self.assertEqual(set(check), {"id", "status", "detail", "source"})
            self.assertEqual(set(check["source"]), {"provider", "request_sha256", "fetched_at"})
        for field in ("issued_ms", "expires_ms", "verdict_id"):
            self.assertIsNone(document[field])
        self.assertEqual(document["subject"], {
            "network": "mainnet", "kind": "transaction", "signer": transaction["signer_id"],
            "receiver": transaction["receiver_id"],
            "actions_sha256": hashlib.sha256(evidence.canonical_bytes(transaction["actions"])).hexdigest()})
        return answer, document

    def finding(self, document, check_id):
        return next(check for check in document["checks"] if check["id"] == check_id)

    def test_synthetic_full_access_addkey_names_public_key(self):
        action = next(action for action in recorded_actions("add-owner-key") if "AddKey" in action)
        answer, document = self.document(synthetic_transaction(action))
        check = self.finding(document, "adds_full_access_key")
        self.assertEqual(check["status"], "signal")
        self.assertIn(action["AddKey"]["public_key"], check["detail"])
        self.assertEqual(answer["outcome"], "DENY")

    def test_synthetic_delete_account_names_beneficiary(self):
        answer, document = self.document(synthetic_transaction(
            {"DeleteAccount": {"beneficiary_id": "beneficiary.near"}}))
        check = self.finding(document, "deletes_account")
        self.assertEqual(check["status"], "signal")
        self.assertIn("beneficiary.near", check["detail"])
        self.assertEqual(answer["outcome"], "DENY")

    def test_synthetic_ft_transfer_call_checks_denied_argument_receiver(self):
        answer, document = self.document(synthetic_transaction(
            synthetic_call("ft_transfer_call", "flagged.near"), receiver="token.near"),
            synthetic_store(("flagged.near", "denylist")))
        check = self.finding(document, "receiver_labels")
        self.assertEqual(check["status"], "signal")
        self.assertIn("flagged.near", check["detail"])
        self.assertEqual(answer["outcome"], "DENY")
        self.assertIn("1" * 64, document["labels"])

    def test_synthetic_ft_transfer_call_to_store_denylist_subject(self):
        denied = next(a for a in self.store.assertions if a.chain == "near" and a.category == "denylist"
                      and a.superseded_by is None)
        answer, document = self.document(synthetic_transaction(
            synthetic_call("ft_transfer_call", denied.subject), receiver="token.near"))
        check = self.finding(document, "receiver_labels")
        self.assertEqual(check["status"], "signal")
        self.assertIn(denied.subject, check["detail"])
        self.assertIn(denied.id, document["labels"])
        self.assertEqual(answer["outcome"], "DENY")

    def test_synthetic_ft_transfer_checks_incident_argument_receiver(self):
        answer, document = self.document(synthetic_transaction(synthetic_call("ft_transfer", "flagged.near")),
                                          synthetic_store(("flagged.near", "incident")))
        self.assertEqual(self.finding(document, "receiver_labels")["status"], "signal")
        self.assertEqual(answer["outcome"], "DENY")

    def test_synthetic_function_call_key_empty_methods_is_unknown(self):
        action = {"AddKey": {"public_key": PUBLIC_KEY, "access_key": {"nonce": 0, "permission": {
            "FunctionCall": {"receiver_id": "contract.near", "method_names": [], "allowance": "1"}}}}}
        answer, document = self.document(synthetic_transaction(action))
        check = self.finding(document, "function_call_key")
        self.assertEqual(check["status"], "unknown")
        self.assertIn(PUBLIC_KEY, check["detail"])
        self.assertEqual(answer["outcome"], "ASK")

    def test_synthetic_function_call_key_no_allowance_is_unknown(self):
        for allowance in (None, "missing"):
            call = {"receiver_id": "contract.near", "method_names": ["run"]}
            if allowance is None:
                call["allowance"] = None
            action = {"AddKey": {"public_key": PUBLIC_KEY, "access_key": {"nonce": 0,
                                  "permission": {"FunctionCall": call}}}}
            with self.subTest(allowance=allowance):
                answer, document = self.document(synthetic_transaction(action))
                self.assertEqual(self.finding(document, "function_call_key")["status"], "unknown")
                self.assertEqual(answer["outcome"], "ASK")

    def test_synthetic_function_call_key_with_limits_passes(self):
        action = {"AddKey": {"public_key": PUBLIC_KEY, "access_key": {"nonce": 0, "permission": {
            "FunctionCall": {"receiver_id": "contract.near", "method_names": ["run"], "allowance": "1"}}}}}
        answer, document = self.document(synthetic_transaction(action))
        self.assertEqual(self.finding(document, "function_call_key")["status"], "pass")
        self.assertEqual(answer["outcome"], "ALLOW")

    def test_synthetic_unmodelled_actions_are_unknown(self):
        for kind in ("Stake", "Delegate", "SignedDelegate", "FutureAction"):
            with self.subTest(kind=kind):
                answer, document = self.document(synthetic_transaction({kind: {}}))
                check = self.finding(document, "unrecognised_action")
                self.assertEqual(check["status"], "unknown")
                self.assertIn(kind, check["detail"])
                self.assertEqual(answer["outcome"], "ASK")

    def test_synthetic_unmodelled_function_call_is_unknown(self):
        action = synthetic_call("withdraw")
        answer, document = self.document(synthetic_transaction(action))
        self.assertEqual(self.finding(document, "unrecognised_action")["status"], "unknown")
        self.assertEqual(answer["outcome"], "ASK")

    def test_synthetic_clean_plain_transfer_has_no_modelled_risk_detail(self):
        recorded = recorded_mainnet()["response"]["transactions"][0]
        action = recorded["receipts"][1]["receipt"]["receipt"]["Action"]["actions"][0]
        self.assertEqual(list(action), ["Transfer"])
        answer, document = self.document(synthetic_transaction(action))
        self.assertEqual(answer["outcome"], "ALLOW")
        self.assertEqual(document["verdict"], "ALLOW")
        self.assertIn("no modelled risk found", [c["detail"] for c in document["checks"]])
        self.assertEqual(answer["reasons"], ["no modelled risk found"])
        self.assertEqual(answer["freshness"], {"block_height": None, "timestamp_ms": None})
        self.assertNotIn("safe", answer["evidence"].lower())

    def test_synthetic_modelled_create_account_allow(self):
        for actions in (["CreateAccount", {"Transfer": {"deposit": "1"}}], [{"CreateAccount": {}}]):
            with self.subTest(actions=actions):
                answer, document = self.document(synthetic_transaction(*actions))
                self.assertEqual(answer["outcome"], "ALLOW")
                self.assertNotIn("unrecognised_action", [check["id"] for check in document["checks"]])

    def test_synthetic_lone_delete_key_is_unknown_naming_the_key(self):
        action = next(action for action in recorded_actions("delete-deploy-key") if "DeleteKey" in action)
        answer, document = self.document(synthetic_transaction(action))
        check = self.finding(document, "unrecognised_action")
        self.assertEqual(check["status"], "unknown")
        self.assertIn(action["DeleteKey"]["public_key"], check["detail"])
        self.assertEqual(answer["outcome"], "ASK")
        self.assertNotIn("no modelled risk found", [check["detail"] for check in document["checks"]])

    def test_synthetic_transfer_target_label_is_checked(self):
        answer, document = self.document(synthetic_transaction({"Transfer": {"deposit": "1"}},
                                          receiver="flagged.near"), synthetic_store(("flagged.near", "denylist")))
        self.assertEqual(self.finding(document, "receiver_labels")["status"], "signal")
        self.assertEqual(answer["outcome"], "DENY")

    def test_synthetic_lookalike_checks_transaction_and_ft_receivers(self):
        store = synthetic_store(("treasury.near", "treasury"))
        for transaction in (synthetic_transaction({"Transfer": {"deposit": "1"}}, receiver="treasurv.near"),
                            synthetic_transaction(synthetic_call("ft_transfer_call", "treasurv.near"))):
            with self.subTest(transaction=transaction):
                answer, document = self.document(transaction, store)
                self.assertEqual(self.finding(document, "lookalike")["status"], "signal")
                self.assertEqual(answer["outcome"], "DENY")
                self.assertIn("1" * 64, document["labels"])

    def test_synthetic_superseded_and_other_chain_labels_do_not_signal(self):
        store = synthetic_store(("recipient.near", "denylist"))
        for field, value in (("superseded_by", "2" * 64), ("chain", "eth")):
            row = store.assertions[0]
            changed = labels.Store([dataclasses.replace(row, **{field: value})], store.sha256)
            with self.subTest(field=field):
                answer, document = self.document(synthetic_transaction({"Transfer": {"deposit": "1"}}), changed)
                self.assertEqual(answer["outcome"], "ALLOW")

    def test_synthetic_deploy_unreported_code_is_unknown(self):
        from intel.feed import code_hash
        action = next(action for action in recorded_actions("deploy") if "DeployContract" in action)
        answer, document = self.document(synthetic_transaction(action))
        check = self.finding(document, "deploys_code")
        self.assertEqual(check["status"], "unknown")
        self.assertIn(code_hash(action["DeployContract"]["code"]), check["detail"])
        self.assertEqual(answer["outcome"], "ASK")

    def test_synthetic_incident_code_deployment_is_signal(self):
        # The incident code is not committed, so only the hash of the synthetic code is replaced; the
        # incident pages are read as the feed reads them.
        reported = json.loads((ROOT / "intel/incidents/rhea.json").read_text(encoding="utf-8"))
        digest = reported["code_hash_as_reported"]["value"]
        with mock.patch("gate.txcheck.code_hash", return_value=digest):
            answer, document = self.document(synthetic_transaction({"DeployContract": {"code": "AA=="}}))
        check = self.finding(document, "deploys_code")
        self.assertEqual(check["status"], "signal")
        self.assertIn(digest, check["detail"])
        self.assertIn(reported["id"], check["detail"])
        self.assertEqual(answer["outcome"], "DENY")

    def test_synthetic_partial_checks_do_not_claim_no_modelled_risk(self):
        answer, document = self.document(synthetic_transaction({"Stake": {}}))
        self.assertEqual(answer["outcome"], "ASK")
        self.assertNotIn("no modelled risk found", [check["detail"] for check in document["checks"]])

    def test_synthetic_duplicate_ft_receiver_raises_before_any_check(self):
        action = synthetic_call("ft_transfer_call")
        args = '{"receiver_id":"recipient.near","receiver_id":"flagged.near","amount":"1","msg":""}'
        action["FunctionCall"]["args"] = base64.b64encode(args.encode()).decode()
        with mock.patch("gate.txcheck.evidence.check") as check:
            with self.assertRaises(ValueError):
                check_transaction(synthetic_transaction(action), store=self.store)
            check.assert_not_called()

    def test_synthetic_signal_wins_over_unknown(self):
        answer, document = self.document(synthetic_transaction({"Stake": {}},
            {"DeleteAccount": {"beneficiary_id": "beneficiary.near"}}))
        self.assertEqual(answer["outcome"], "DENY")
        self.assertEqual(document["verdict"], "DENY")

    def test_synthetic_malformed_input_raises_before_any_check(self):
        malformed = [None, {}, synthetic_transaction(), synthetic_transaction({}),
                     synthetic_transaction({"Transfer": {}}), synthetic_transaction({"Transfer": {"deposit": True}}),
                     synthetic_transaction({"Transfer": {"deposit": "-1"}}),
                     synthetic_transaction({"DeleteAccount": {"beneficiary_id": "invalid!"}}),
                     synthetic_transaction({"FunctionCall": {"method_name": "ft_transfer_call", "args": "!"}}),
                     synthetic_transaction({"DeployContract": {"code": "!"}}),
                     synthetic_transaction({"AddKey": {"public_key": PUBLIC_KEY, "access_key": {}}}),
                     synthetic_transaction({"DeleteKey": {"public_key": "ed25519:" + "0" * 32}}),
                     synthetic_transaction({"DeleteKey": {"public_key": "ed25519:" + "1" * 31}}),
                     synthetic_transaction({"DeleteKey": {"public_key": "rsa:" + "1" * 32}}),
                     synthetic_transaction({"Stake": {"stake": 1.5}}),
                     synthetic_transaction(*recorded_actions("payment-after-handover")),
                     synthetic_transaction({"Transfer": {"deposit": "1"}}, receiver="account.testnet")]
        transaction = synthetic_transaction({"Transfer": {"deposit": "1"}})
        transaction["signer_id"] = "invalid!"
        malformed.append(transaction)
        transaction = synthetic_transaction({"Transfer": {"deposit": "1"}}, {"Transfer": {}})
        malformed.append(transaction)
        for transaction in malformed:
            with self.subTest(transaction=transaction), mock.patch("gate.txcheck.evidence.check") as check:
                with self.assertRaises(ValueError):
                    check_transaction(transaction, store=self.store)
                check.assert_not_called()

    def test_synthetic_malformed_ft_args_raise_before_any_check(self):
        for args in ([], {}, {"receiver_id": "invalid!", "amount": "1", "msg": ""},
                     {"receiver_id": "account.testnet", "amount": "1", "msg": ""}):
            action = synthetic_call("ft_transfer_call")
            action["FunctionCall"]["args"] = base64.b64encode(json.dumps(args).encode()).decode()
            with self.subTest(args=args), mock.patch("gate.txcheck.evidence.check") as check:
                with self.assertRaises(ValueError):
                    check_transaction(synthetic_transaction(action), store=self.store)
                check.assert_not_called()

    def test_synthetic_invalid_ft_args_text_raises_before_any_check(self):
        body = '{"receiver_id":"recipient.near","amount":"1","msg":"%s","extra":%s}'
        for name, raw in (("nan", (body % ("", "NaN")).encode()),
                          ("infinity", (body % ("", "Infinity")).encode()),
                          ("negative infinity", (body % ("", "-Infinity")).encode()),
                          ("utf-16", (body % ("", "1")).encode("utf-16")),
                          ("utf-16 without byte order mark", (body % ("", "1")).encode("utf-16-le")),
                          ("lone surrogate", (body % ("\\ud800", "1")).encode()),
                          ("invalid utf-8", (body % ("", "1")).encode().replace(b'"msg":""', b'"msg":"\xff"')),
                          ("utf-8 byte order mark", b"\xef\xbb\xbf" + (body % ("", "1")).encode())):
            action = synthetic_call("ft_transfer_call")
            action["FunctionCall"]["args"] = base64.b64encode(raw).decode()
            with self.subTest(name=name), mock.patch("gate.txcheck.evidence.check") as check:
                with self.assertRaises(ValueError):
                    check_transaction(synthetic_transaction(action), store=self.store)
                check.assert_not_called()

    def test_synthetic_actions_hash_is_canonical_and_input_is_unchanged(self):
        transaction = synthetic_transaction(synthetic_call("ft_transfer_call"))
        original = copy.deepcopy(transaction)
        first = check_transaction(transaction, store=self.store)
        self.assertEqual(transaction, original)
        self.assertEqual(first, check_transaction(copy.deepcopy(transaction), store=self.store))

    def test_no_signer_function_is_imported(self):
        tree = ast.parse((ROOT / "gate/txcheck.py").read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                names = [alias.name for alias in node.names]
                if isinstance(node, ast.ImportFrom):
                    names += [f"{node.module}.{alias.name}" for alias in node.names]
                for name in names:
                    self.assertFalse(name.startswith(SIGNER_MODULES) or "sign" in name.rsplit(".", 1)[-1], name)
        loaded = subprocess.run([sys.executable, "-c", "import sys, gate.txcheck; print(sorted(sys.modules))"],
                                cwd=ROOT, capture_output=True, text=True, check=True).stdout
        for module in ("gate.signer", "gate.verdict", "gate.agent", "gate.outlayer", "nacl", "py_near"):
            self.assertNotIn(repr(module), loaded)

    def test_recorded_delete_account_action(self):
        transaction = json.loads((ROOT / "fixtures/txcheck/recorded-delete-account.json").read_text(encoding="utf-8"))
        answer, document = self.document(transaction)
        self.assertEqual(self.finding(document, "deletes_account")["status"], "signal")
        self.assertEqual(answer["outcome"], "DENY")

    @unittest.skip("missing fixture: fixtures/txcheck/recorded-ft-transfer-call.json (raw args and labelled receiver)")
    def test_recorded_ft_transfer_call_denied_receiver(self):
        transaction = json.loads((ROOT / "fixtures/txcheck/recorded-ft-transfer-call.json").read_text(encoding="utf-8"))
        answer, document = self.document(transaction)
        self.assertEqual(self.finding(document, "receiver_labels")["status"], "signal")
        self.assertEqual(answer["outcome"], "DENY")
