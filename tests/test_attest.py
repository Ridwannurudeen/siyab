import contextlib
import copy
import hashlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from gate import attest, canon, ed25519, evidence

ROOT = Path(__file__).resolve().parent.parent
VECTORS = json.loads((ROOT / "fixtures" / "attest" / "vectors.json").read_text(encoding="utf-8"))


class AttestTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.public_key = ed25519.public_from_seed(ed25519.TEST_VERIFIER_SEED).hex()
        cls.agent_pk = ed25519.public_from_seed(ed25519.TEST_AGENT_SEED).hex()

    def setUp(self):
        for target in ("urllib.request.urlopen", "gate.rpc._fetch", "gate.rpc._open_with_deadline"):
            patcher = mock.patch(target, side_effect=AssertionError("unexpected network read"))
            patcher.start()
            self.addCleanup(patcher.stop)
        self.config = copy.deepcopy(VECTORS["registry_config"][0]["get_config_result"])
        self.key = copy.deepcopy(VECTORS["attested_keys"][0]["view"])
        self.key["public_key"] = self.public_key
        self.now = self.key["registered_ms"] + 1000
        self.view = {"enabled": True, "registry_id": "attest.testnet", "measurement_timelock_ms": 86400000,
                     "measurements": [{"measurement_id": self.key["measurement_id"], "label": "test",
                                       "active_from_ms": self.now - 1}],
                     "platforms": [self.key["ppid"]], "pending_platforms": None,
                     "denied_keys": [], "keys": [copy.deepcopy(self.key)]}
        self.document = evidence.document(
            subject=evidence.subject(gate="gate.testnet", agent_pk=self.agent_pk, kind="ft", token="token.testnet",
                                     receiver="payee.testnet", amount="123", memo="receipt test"),
            checks=[evidence.check("test", "pass", "synthetic test", {"provider": "test", "request_sha256": None,
                                                                       "fetched_at": None})],
            labels=[], verdict="ALLOW", reasons=[], observed={"block_height": 42, "timestamp_ms": self.now},
            issued_ms=self.now, expires_ms=self.now + 300000, verdict_id="01" * 16, latency_ms=1)
        self.receipt = {"outcome": "ALLOW", "action": "sign", "evidence_hash": "", "verdict": {},
                        "signer": {"public_key_hex": self.public_key, "measurement_id": self.key["measurement_id"],
                                   "registry": "attest.testnet"}}
        self.sign_document()

    def sign_document(self):
        self.data = evidence.canonical_bytes(self.document)
        verdict = {"literal": "ALLOW", "issued_ms": self.document["issued_ms"],
                   "expires_ms": self.document["expires_ms"], "verdict_id": self.document["verdict_id"],
                   "evidence_hash": hashlib.sha256(self.data).hexdigest(), "key_id": "cur"}
        message = attest.rebuild_message(self.document, verdict)
        verdict["signature"] = ed25519.sign(ed25519.TEST_VERIFIER_SEED, canon.digest(message)).hex()
        self.receipt["verdict"] = verdict
        self.receipt["evidence_hash"] = verdict["evidence_hash"]

    def verify(self, **kwargs):
        options = {"gate": "gate.testnet", "agent_pk_hex": self.agent_pk, "attestation": self.view,
                   "registry_config": self.config, "registry_keys": [self.key], "now_ms": self.now}
        options.update(kwargs)
        return attest.verify_receipt(self.receipt, self.data, **options)

    def snapshot(self):
        return {"attestation": self.view, "registry_config": self.config, "registry_keys": [self.key],
                "max_ttl_ms": 300000}

    def test_measurement_vectors(self):
        for vector in VECTORS["measurements"]:
            with self.subTest(name=vector["name"]):
                self.assertEqual(attest.measurement_id(vector["input"]), vector["measurement_id"])

    def test_measurement_exact_fields_widths_and_order(self):
        measurement = VECTORS["measurements"][1]["input"]
        self.assertEqual(attest.measurement_id(dict(reversed(list(measurement.items())))),
                         attest.measurement_id(measurement))
        for field in measurement:
            for value in ("00", measurement[field].upper(), None):
                if value == measurement[field]:
                    continue
                with self.subTest(field=field, value=value), self.assertRaises(attest.AttestError):
                    attest.measurement_id({**measurement, field: value})
            with self.subTest(missing=field), self.assertRaises(attest.AttestError):
                attest.measurement_id({key: value for key, value in measurement.items() if key != field})
        with self.assertRaises(attest.AttestError):
            attest.measurement_id({**measurement, "rtmr3": "00" * 48})

    def test_config_vector_and_bounds(self):
        self.assertEqual(attest.validate_config(self.config), self.config)
        for field, value in (("max_lease_ms", 0), ("max_lease_ms", True), ("ed25519_domain_id", "7"),
                             ("verifier_account_id", "A.near"), ("signer_account_id", "a..near"),
                             ("vote_recipients", []), ("vote_recipients", ["z.near", "a.near"]),
                             ("vote_recipients", ["a.near", "a.near"]),
                             ("vote_recipients", [f"a{i}.near" for i in range(9)])):
            with self.subTest(field=field, value=value), self.assertRaises(attest.AttestError):
                attest.validate_config({**self.config, field: value})
        for field in self.config:
            with self.subTest(missing=field), self.assertRaises(attest.AttestError):
                attest.validate_config({key: value for key, value in self.config.items() if key != field})
        with self.assertRaises(attest.AttestError):
            attest.validate_config({**self.config, "owner": "owner.near"})

    def test_key_view_vector(self):
        vector = VECTORS["attested_keys"][0]
        self.assertEqual(attest.validate_key_view(vector["view"], vector["max_lease_ms"]), vector["view"])

    def test_shared_issue_time_vectors(self):
        for vector in VECTORS["key_issue_times"]:
            with self.subTest(name=vector["name"]):
                view = {**self.key, **vector["times"]}
                if vector["accepted"]:
                    attest.validate_key_view(view, vector["max_lease_ms"])
                else:
                    with self.assertRaises(attest.AttestError):
                        attest.validate_key_view(view, vector["max_lease_ms"])

    def test_key_view_exact_shape(self):
        for field in self.key:
            with self.subTest(missing=field), self.assertRaises(attest.AttestError):
                attest.validate_key_view({key: value for key, value in self.key.items() if key != field}, 86400000)
        with self.assertRaises(attest.AttestError):
            attest.validate_key_view({**self.key, "max_lease_ms": 86400000}, 86400000)

    def test_key_view_rejects_noncanonical_values(self):
        values = {"public_key": "ed25519:" + self.public_key, "measurement_id": "00" * 32,
                  "ppid": "00" * 32, "aux": "AB" * 32, "tcb_status": "OutOfDate", "advisory_ids": [1]}
        for field, value in values.items():
            with self.subTest(field=field), self.assertRaises(attest.AttestError):
                attest.validate_key_view({**self.key, field: value}, 86400000)
        for field in ("issued_at_ms", "registered_ms", "expires_ms", "collateral_expires_ms"):
            for value in (True, -1, 2**64, "1", 1.0, None):
                with self.subTest(field=field, value=value), self.assertRaises(attest.AttestError):
                    attest.validate_key_view({**self.key, field: value}, 86400000)

    def test_key_view_checks_request_and_current_clock(self):
        for changes in ({"public_key": "00" * 32}, {"now_ms": self.key["expires_ms"]},
                        {"now_ms": self.key["registered_ms"] - 1}):
            with self.subTest(changes=changes), self.assertRaises(attest.AttestError):
                attest.validate_key_view(self.key, 86400000, **changes)
        attest.validate_key_view(self.key, 86400000, public_key=self.public_key,
                                 now_ms=self.key["registered_ms"])

    def test_key_lease_formula_capping_and_overflow(self):
        shortened = {**self.key, "expires_ms": self.now + 1, "collateral_expires_ms": self.now + 1}
        attest.validate_key_view(shortened, 86400000)
        for changed in ({**self.key, "expires_ms": self.key["expires_ms"] + 1},
                        {**self.key, "issued_at_ms": 2**64 - 2, "registered_ms": 2**64 - 2,
                         "expires_ms": 2**64 - 1, "collateral_expires_ms": 2**64 - 1}):
            with self.subTest(changed=changed), self.assertRaises(attest.AttestError):
                attest.validate_key_view(changed, 86400000)

    def test_get_key_argument_vectors(self):
        for vector in VECTORS["get_key_args"]:
            self.assertEqual(json.dumps(attest.sync_args(vector["public_key"]), separators=(",", ":")).encode().hex(),
                             vector["arguments_json_hex"])

    def test_registry_views_config_first_same_block_and_clock(self):
        client = mock.Mock()
        client.call_function.side_effect = [self.config, self.key]
        actual = attest.registry_views(client, "attest.testnet", self.public_key, block_id=42, now_ms=self.now)
        self.assertEqual(actual, {"config": self.config, "key": self.key})
        self.assertEqual(client.call_function.call_args_list,
                         [mock.call("attest.testnet", "get_config", {}, block_id=42),
                          mock.call("attest.testnet", "get_key", {"public_key": self.public_key}, block_id=42)])

    def test_registry_views_refuse_absent_and_malformed(self):
        for value in (None, {}, {**self.key, "tcb_status": "Unknown"}):
            client = mock.Mock()
            client.call_function.side_effect = [self.config, value]
            with self.subTest(value=value), self.assertRaises(attest.AttestError):
                attest.registry_views(client, "attest.testnet", self.public_key)
        client = mock.Mock()
        client.call_function.return_value = {}
        with self.assertRaises(attest.AttestError):
            attest.registry_views(client, "attest.testnet", self.public_key)
        self.assertEqual(client.call_function.call_count, 1)

    def test_read_views_pins_every_call_to_one_final_block(self):
        client = mock.Mock()
        client.final_block.return_value = {"header": {"height": 42, "timestamp": self.now * 1000000 + 999999}}
        client.call_function.side_effect = [self.view, {"denylist": [], "paused": False, "max_ttl_ms": 300000},
                                            self.config, self.key]
        result = attest.read_views(client, "gate.testnet", pinned_registry="attest.testnet")
        self.assertEqual(result["now_ms"], self.now)
        self.assertEqual(result["block_height"], 42)
        self.assertEqual(result["registry_keys"], [self.key])
        self.assertTrue(all(call.kwargs == {"block_id": 42} for call in client.call_function.call_args_list))

    def test_read_views_rejects_registry_pin_before_reading_registry(self):
        client = mock.Mock()
        client.final_block.return_value = {"header": {"height": 42, "timestamp": self.now * 1000000}}
        client.call_function.return_value = self.view
        with self.assertRaises(attest.AttestError):
            attest.read_views(client, "gate.testnet", pinned_registry="other.testnet")
        self.assertEqual(client.call_function.call_count, 1)

    def test_signed_receipt_is_verified(self):
        result = self.verify()
        self.assertTrue(result["verified"])
        self.assertEqual(result["public_key"], self.public_key)
        self.assertEqual(result["measurement_id"], self.key["measurement_id"])
        self.assertFalse(result["dcap_verified"])

    def test_client_measurement_and_registry_pins(self):
        self.verify(pinned_measurements=[self.key["measurement_id"]], pinned_registry="attest.testnet")
        for options in ({"pinned_measurements": ["00" * 32]}, {"pinned_registry": "other.testnet"}):
            with self.subTest(options=options), self.assertRaises(attest.AttestError):
                self.verify(**options)

    def test_measurement_revoked_or_pending(self):
        for measurements in ([], [{**self.view["measurements"][0], "active_from_ms": self.now + 1}]):
            with self.subTest(measurements=measurements), self.assertRaises(attest.AttestError):
                self.verify(attestation={**self.view, "measurements": measurements})
        self.verify(attestation={**self.view, "measurements": [{**self.view["measurements"][0],
                                                               "active_from_ms": self.now}]})

    def test_key_denied_or_platform_unapproved(self):
        for changes in ({"denied_keys": [self.public_key]}, {"platforms": ["00" * 16]}, {"platforms": None},
                        {"platforms": []}, {"enabled": False}):
            with self.subTest(changes=changes), self.assertRaises(attest.AttestError):
                self.verify(attestation={**self.view, **changes})

    def test_pending_platform_does_not_authorize_a_key(self):
        changed = {**self.view, "platforms": ["00" * 16],
                   "pending_platforms": {"platforms": [self.key["ppid"]], "active_from_ms": self.now + 1}}
        with self.assertRaises(attest.AttestError):
            self.verify(attestation=changed)

    def test_expired_or_future_cached_and_registry_records(self):
        for now in (self.key["expires_ms"], self.key["registered_ms"] - 1):
            with self.subTest(now=now), self.assertRaises(attest.AttestError):
                self.verify(now_ms=now)

    def test_registry_identity_cannot_change(self):
        for field, value in (("ppid", "00" * 16), ("aux", "ab" * 32),
                             ("issued_at_ms", self.key["issued_at_ms"] - 1)):
            changed = {**self.key, field: value}
            if field == "issued_at_ms":
                changed["expires_ms"] -= 1
            with self.subTest(field=field), self.assertRaises(attest.AttestError):
                self.verify(registry_keys=[changed])

    def test_newer_registry_renewal_preserves_live_cache(self):
        renewed = {**self.key, "issued_at_ms": self.key["issued_at_ms"] + 1,
                   "registered_ms": self.key["registered_ms"] + 1, "expires_ms": self.key["expires_ms"] + 1}
        self.verify(registry_keys=[renewed])

    def test_equal_issue_time_cannot_change_a_cached_record(self):
        changed = {**self.key, "registered_ms": self.key["registered_ms"] + 1}
        with self.assertRaises(attest.AttestError):
            self.verify(registry_keys=[changed])

    def test_cache_and_registry_each_need_a_live_lease(self):
        expired = {**self.key, "expires_ms": self.now, "collateral_expires_ms": self.now}
        future = {**self.key, "registered_ms": self.now + 1}
        for changed in (expired, future):
            with self.subTest(location="cache", changed=changed), self.assertRaises(attest.AttestError):
                self.verify(attestation={**self.view, "keys": [changed]})
            with self.subTest(location="registry", changed=changed), self.assertRaises(attest.AttestError):
                self.verify(registry_keys=[changed])

    def test_four_key_limit_and_duplicate_keys(self):
        for keys in ([self.key] * 2, [self.key] * 5):
            with self.subTest(count=len(keys)), self.assertRaises(attest.AttestError):
                self.verify(attestation={**self.view, "keys": keys})

    def test_four_keys_tried_without_signer_hint(self):
        keys = [{**self.key, "public_key": ed25519.public_from_seed(hashlib.sha256(f"TEST {i}".encode()).digest()).hex()}
                for i in range(3)] + [self.key]
        self.receipt.pop("signer")
        with mock.patch("gate.attest.ed25519.verify", wraps=ed25519.verify) as checked:
            self.verify(attestation={**self.view, "keys": keys}, registry_keys=keys)
        self.assertEqual(checked.call_count, 4)

    def test_legacy_key_id_is_refused(self):
        self.receipt["verdict"]["key_id"] = "prev"
        with self.assertRaises(attest.AttestError):
            self.verify()

    def test_wrong_signature_and_small_order_keys_are_refused(self):
        self.receipt["verdict"]["signature"] = "00" * 64
        with self.assertRaises(attest.AttestError):
            self.verify()
        identity = "01" + "00" * 31
        changed = {**self.key, "public_key": identity}
        self.receipt["verdict"]["signature"] = identity + "00" * 32
        self.receipt.pop("signer")
        with self.assertRaises(attest.AttestError):
            self.verify(attestation={**self.view, "keys": [changed]}, registry_keys=[changed])

    def test_evidence_hash_and_exact_bytes(self):
        for data in (self.data + b"\n", self.data.replace(b'"123"', b'"124"')):
            with self.subTest(data=data), self.assertRaises(attest.AttestError):
                attest.verify_receipt(self.receipt, data, gate="gate.testnet", agent_pk_hex=self.agent_pk,
                                      attestation=self.view, registry_config=self.config, registry_keys=[self.key],
                                      now_ms=self.now)

    def test_evidence_and_receipt_context_must_match(self):
        for field, value in (("outcome", "DENY"), ("action", "ask"), ("evidence_hash", "00" * 32),
                             ("request", {"kind": "near", "token_id": None, "receiver_id": "payee.testnet",
                                          "amount": "123", "memo": "receipt test"}),
                             ("message", "forged"), ("checks", [])):
            original = copy.deepcopy(self.receipt)
            self.receipt[field] = value
            with self.subTest(field=field), self.assertRaises(attest.AttestError):
                self.verify()
            self.receipt = original

    def test_signed_but_malformed_evidence_is_refused(self):
        original = copy.deepcopy(self.document)
        for field, value in (("checks", [None]), ("labels", [1]), ("reasons", [False]),
                             ("observed", {"block_height": True, "timestamp_ms": self.now}),
                             ("subject", {**original["subject"], "amount": 123}),
                             ("subject", {**original["subject"], "memo_sha256": "-"})):
            self.document = {**original, field: value}
            self.sign_document()
            with self.subTest(field=field, value=value), self.assertRaises(attest.AttestError):
                self.verify()

    def test_nonstring_request_memo_is_refused(self):
        self.receipt["request"] = {"kind": "ft", "token_id": "token.testnet", "receiver_id": "payee.testnet",
                                   "amount": "123", "memo": {}}
        with self.assertRaises(ValueError):
            self.verify()

    def test_evidence_subject_is_bound_to_gate_and_agent(self):
        for options in ({"gate": "other.testnet"}, {"agent_pk_hex": "00" * 32}):
            with self.subTest(options=options), self.assertRaises(attest.AttestError):
                self.verify(**options)

    def test_verdict_and_evidence_timestamps_and_id_must_match(self):
        for field in ("issued_ms", "expires_ms", "verdict_id"):
            original = self.receipt["verdict"][field]
            self.receipt["verdict"][field] = "02" * 16 if field == "verdict_id" else original + 1
            with self.subTest(field=field), self.assertRaises(attest.AttestError):
                self.verify()
            self.receipt["verdict"][field] = original

    def test_verdict_time_edges_match_gate(self):
        for offset, accepted in ((30000, True), (30001, False)):
            self.document["issued_ms"] = self.now + offset
            self.document["expires_ms"] = self.now + offset + 300000
            self.sign_document()
            with self.subTest(offset=offset):
                if accepted:
                    self.verify()
                else:
                    with self.assertRaises(attest.AttestError):
                        self.verify()
        self.document["issued_ms"] = self.now
        self.document["expires_ms"] = self.now
        self.sign_document()
        with self.assertRaises(attest.AttestError):
            self.verify()

    def test_verdict_ttl_is_bounded(self):
        with self.assertRaises(attest.AttestError):
            self.verify(max_ttl_ms=299999)
        for value in (True, -1, "300000", 2**64):
            with self.subTest(value=value), self.assertRaises(attest.AttestError):
                self.verify(max_ttl_ms=value)

    def test_receipt_signer_hint_must_match(self):
        for field, value in (("public_key_hex", "ab" * 32), ("measurement_id", "00" * 32),
                             ("registry", "other.testnet")):
            changed = {**self.receipt["signer"], field: value}
            original = self.receipt["signer"]
            self.receipt["signer"] = changed
            with self.subTest(field=field), self.assertRaises(attest.AttestError):
                self.verify()
            self.receipt["signer"] = original

    def test_attestation_shape_and_timelock_are_strict(self):
        for changes in ({"measurement_timelock_ms": 3600000}, {"measurement_timelock_ms": True},
                        {"pending_platforms": {"platforms": [self.key["ppid"]], "active_from_ms": "1"}},
                        {"platforms": [self.key["ppid"], self.key["ppid"]]},
                        {"measurements": self.view["measurements"] * 2}):
            with self.subTest(changes=changes), self.assertRaises(attest.AttestError):
                self.verify(attestation={**self.view, **changes})
        for field in self.view:
            with self.subTest(missing=field), self.assertRaises(attest.AttestError):
                self.verify(attestation={key: value for key, value in self.view.items() if key != field})

    def run_cli(self, argv):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            result = attest.main(argv)
        return result, output.getvalue()

    def test_cli_measure_and_sync_args_are_offline(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "measurement.json"
            path.write_text(json.dumps(self.key["measurement"]), encoding="utf-8")
            code, output = self.run_cli(["measure", str(path)])
            self.assertEqual((code, output.strip()), (0, self.key["measurement_id"]))
        code, output = self.run_cli(["sync-args", self.public_key])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(output), {"public_key": self.public_key})

    def test_cli_offline_snapshot_is_qualified(self):
        with tempfile.TemporaryDirectory() as directory:
            receipt = Path(directory) / "receipt.json"
            snapshot = Path(directory) / "snapshot.json"
            receipt.write_text(json.dumps({**self.receipt, "evidence": self.data.decode()}), encoding="utf-8")
            snapshot.write_text(json.dumps(self.snapshot()), encoding="utf-8")
            code, output = self.run_cli(["verify", str(receipt), "--snapshot", str(snapshot), "--now-ms", str(self.now),
                                         "--gate", "gate.testnet", "--agent-pk", self.agent_pk])
        result = json.loads(output)
        self.assertEqual(code, 0)
        self.assertTrue(result["verified"])
        self.assertEqual(result["outcome"], "UNKNOWN")
        self.assertEqual(result["state_source"], "supplied snapshot")
        self.assertFalse(result["dcap_verified"])

    def test_cli_failure_is_unknown(self):
        code, output = self.run_cli(["sync-args", "AB" * 32])
        self.assertEqual(code, 1)
        self.assertEqual(json.loads(output)["outcome"], "UNKNOWN")

    def test_cli_duplicate_json_keys_and_nonfinite_numbers_are_refused(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "measurement.json"
            for raw in ('{"mrtd":0,"mrtd":1}', '{"mrtd":NaN}', '{"mrtd":Infinity}'):
                path.write_text(raw, encoding="utf-8")
                with self.subTest(raw=raw):
                    code, output = self.run_cli(["measure", str(path)])
                    self.assertEqual(code, 1)
                    self.assertEqual(json.loads(output)["outcome"], "UNKNOWN")

    def test_live_is_explicit_and_never_uses_snapshot_clock(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "receipt.json"
            path.write_text(json.dumps({**self.receipt, "evidence": self.data.decode()}), encoding="utf-8")
            snapshot = {**self.snapshot(), "now_ms": self.now, "block_height": 42}
            with mock.patch("gate.attest.read_views", return_value=snapshot) as read, mock.patch("gate.attest.rpc.Client") as client:
                code, output = self.run_cli(["verify", str(path), "--live", "--rpc-url", "https://rpc.example",
                                             "--gate", "gate.testnet", "--agent-pk", self.agent_pk])
            self.assertEqual(code, 0)
            client.assert_called_once_with("https://rpc.example", mode="off")
            read.assert_called_once()
            self.assertEqual(json.loads(output)["state_source"], "final RPC views")
            self.assertEqual(json.loads(output)["outcome"], "UNKNOWN")
            self.assertEqual(json.loads(output)["payment_eligibility"], "not checked")


if __name__ == "__main__":
    unittest.main()
