import contextlib
import copy
import hashlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from gate import __main__ as gate_main
from gate import evidence, rpc, rules
from intel import labels, live

ROOT = Path(__file__).resolve().parent.parent
SAMPLE = ROOT / "fixtures" / "receipts" / "payment-check.json"
LIMITS = ("receipt consistent (unsigned: this checks the answer against its own evidence, "
          "it does not authenticate the server or prove the checks are true)")


def sample_receipt():
    client = mock.Mock(last_source={"provider": "offline sample", "request_sha256": None,
                                    "fetched_at": None})
    client.view_account.return_value = {"block_height": 123, "code_hash": "11111111111111111111111111111111"}
    client.view_access_key_list.side_effect = rpc.TransportError("stubbed read unavailable")
    client.call_function.side_effect = rpc.TransportError("stubbed read unavailable")
    store = labels.Store([], hashlib.sha256(b"").hexdigest())
    service = live.LiveAPI(store, wall_clock=lambda: 1000, perf_clock=lambda: 10)
    with mock.patch("intel.live.rpc.Client", return_value=client):
        status, answer, _ = service.handle("/live/verdict?receiver=alice.near&token=near&amount=1",
                                           "127.0.0.1", {})
    if status != 200:
        raise AssertionError(answer)
    return answer


def run(path):
    output, errors = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
        try:
            code = gate_main.main(["verify-receipt", str(path)])
        except SystemExit as error:
            code = error.code
    return code, output.getvalue(), errors.getvalue()


class VerifyReceiptTest(unittest.TestCase):
    def setUp(self):
        for target in ("urllib.request.urlopen", "gate.rpc._open_with_deadline", "gate.rpc._fetch"):
            patcher = mock.patch(target, side_effect=AssertionError("unexpected network read"))
            patcher.start()
            self.addCleanup(patcher.stop)
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name) / "receipt.json"
        self.answer = json.loads(SAMPLE.read_text(encoding="utf-8"))

    def check(self, answer, expected=1):
        content = json.dumps(answer) if isinstance(answer, dict) else answer
        self.path.write_text(content, encoding="utf-8", newline="\n")
        code, output, errors = run(self.path)
        self.assertEqual(code, expected, output + errors)
        if expected == 1:
            self.assertTrue(output.startswith("not verified: "), output + errors)
            self.assertEqual(len(output.splitlines()), 1)
            self.assertNotIn("receipt consistent", output)
        return output

    def replace_evidence(self, document):
        self.answer["evidence"] = evidence.canonical_bytes(document).decode("utf-8")
        self.answer["evidence_hash"] = hashlib.sha256(self.answer["evidence"].encode()).hexdigest()

    def test_sample_is_produced_by_the_live_path_with_stubbed_reads(self):
        answer = sample_receipt()
        self.assertEqual(self.answer, answer)
        output = self.check(answer, 0)
        self.assertIn("receiver: alice.near\ntoken: near\namount: 1\nobserved block: 123\n", output)
        self.assertIn("outcome: ASK (ask)", output)
        self.assertEqual(output.splitlines()[-1], LIMITS)

    def test_extra_receipt_signature_is_refused(self):
        self.answer["signature"] = "claimed"
        self.check(self.answer)

    def test_conflicting_extra_receipt_subject_is_refused(self):
        self.answer["subject"] = {"receiver": "other.near"}
        self.check(self.answer)

    def test_evidence_fields_match_the_document_writer(self):
        document = json.loads(self.answer["evidence"])
        expected = evidence.document(
            subject=document["subject"], checks=document["checks"], labels=document["labels"],
            verdict=document["verdict"], reasons=document["reasons"], observed=document["observed"],
            issued_ms=None, expires_ms=None, verdict_id=None, latency_ms=document["latency_ms"])
        self.assertEqual(set(document), set(expected))
        self.replace_evidence(expected)
        self.check(self.answer, 0)
        for field in expected:
            with self.subTest(field=field):
                document = dict(expected)
                del document[field]
                self.replace_evidence(document)
                self.check(self.answer)

    def test_extra_evidence_signature_is_refused_with_matching_hash(self):
        document = json.loads(self.answer["evidence"])
        document["signature"] = "claimed"
        self.replace_evidence(document)
        self.check(self.answer)

    def test_extra_subject_fields_are_refused(self):
        original = json.loads(self.answer["evidence"])
        for field, value in (("gate", "other.near"), ("receiver_id", "other.near")):
            with self.subTest(field=field):
                document = copy.deepcopy(original)
                document["subject"][field] = value
                self.replace_evidence(document)
                self.check(self.answer)

    def test_missing_subject_fields_are_refused(self):
        original = json.loads(self.answer["evidence"])
        for field in original["subject"]:
            with self.subTest(field=field):
                document = copy.deepcopy(original)
                del document["subject"][field]
                self.replace_evidence(document)
                self.check(self.answer)

    def test_nested_fields_match_the_writers(self):
        document = json.loads(self.answer["evidence"])
        source = rules._source(mock.Mock(last_source=rules._local("offline sample")))
        expected = evidence.check("receiver_exists", "pass", "alice.near exists", source)
        self.assertEqual(set(document["checks"][0]), set(expected))
        self.assertEqual(set(document["checks"][0]["source"]), set(source))
        reduced = live.LiveAPI._verdict_shape(
            document["verdict"], document["checks"], document["reasons"],
            self.answer["evidence_hash"], document["observed"], document["latency_ms"])
        self.assertEqual(set(self.answer["checks"][0]), set(reduced["checks"][0]))
        self.assertEqual(set(document["observed"]), {"block_height", "timestamp_ms"})
        self.assertEqual(set(self.answer["freshness"]), set(document["observed"]))
        self.check(self.answer, 0)

    def test_extra_nested_fields_are_refused_with_matching_hash(self):
        original = copy.deepcopy(self.answer)
        for level in ("evidence check", "source", "answer check", "observed", "freshness"):
            for field in ("signature", "extra"):
                with self.subTest(level=level, field=field):
                    self.answer = copy.deepcopy(original)
                    document = json.loads(self.answer["evidence"])
                    targets = {"evidence check": document["checks"][0],
                               "source": document["checks"][0]["source"],
                               "answer check": self.answer["checks"][0],
                               "observed": document["observed"], "freshness": self.answer["freshness"]}
                    targets[level][field] = "claimed"
                    if level == "observed":
                        self.answer["freshness"] = dict(document["observed"])
                    self.replace_evidence(document)
                    self.check(self.answer)

    def test_missing_nested_fields_are_refused_with_matching_hash(self):
        original = copy.deepcopy(self.answer)
        document = json.loads(original["evidence"])
        source = rules._source(mock.Mock(last_source=rules._local("offline sample")))
        expected = evidence.check("receiver_exists", "pass", "alice.near exists", source)
        fields = {"evidence check": expected, "source": source, "answer check": original["checks"][0],
                  "observed": document["observed"], "freshness": original["freshness"]}
        for level, keys in fields.items():
            for field in keys:
                with self.subTest(level=level, field=field):
                    self.answer = copy.deepcopy(original)
                    document = json.loads(self.answer["evidence"])
                    targets = {"evidence check": document["checks"][0],
                               "source": document["checks"][0]["source"],
                               "answer check": self.answer["checks"][0],
                               "observed": document["observed"], "freshness": self.answer["freshness"]}
                    del targets[level][field]
                    if level == "observed":
                        self.answer["freshness"] = dict(document["observed"])
                    self.replace_evidence(document)
                    self.check(self.answer)

    def test_invalid_source_types_are_refused_with_matching_hash(self):
        original = json.loads(self.answer["evidence"])
        for field in ("provider", "request_sha256", "fetched_at"):
            for value in (False, 1, [], {}):
                with self.subTest(field=field, value=value):
                    document = copy.deepcopy(original)
                    document["checks"][0]["source"][field] = value
                    self.replace_evidence(document)
                    self.check(self.answer)
        document = copy.deepcopy(original)
        document["checks"][0]["source"]["provider"] = None
        self.replace_evidence(document)
        self.check(self.answer)

    def test_string_and_null_source_fields_match_the_live_writers(self):
        original = json.loads(self.answer["evidence"])
        for source in (rules._local("local patterns"),
                       rules._source(mock.Mock(last_source={"provider": "offline sample",
                           "request_sha256": hashlib.sha256(b"request").hexdigest(),
                           "fetched_at": "2026-11-12T00:00:00Z"}))):
            with self.subTest(source=source):
                document = copy.deepcopy(original)
                document["checks"][0]["source"] = source
                self.replace_evidence(document)
                self.check(self.answer, 0)

    def test_invalid_check_types_are_refused_with_matching_hash(self):
        original = copy.deepcopy(self.answer)
        for field in ("id", "status", "detail"):
            for value in (None, False, 1, [], {}):
                with self.subTest(field=field, value=value):
                    self.answer = copy.deepcopy(original)
                    document = json.loads(self.answer["evidence"])
                    document["checks"][0][field] = value
                    self.answer["checks"][0]["name" if field == "id" else field] = value
                    self.replace_evidence(document)
                    self.check(self.answer)

    def test_observation_types_are_refused_with_matching_hash(self):
        original = json.loads(self.answer["evidence"])
        for field in ("block_height", "timestamp_ms"):
            for value in (True, "1", [], {}, -1):
                with self.subTest(field=field, value=value):
                    document = copy.deepcopy(original)
                    document["observed"][field] = value
                    self.answer["freshness"] = dict(document["observed"])
                    self.replace_evidence(document)
                    self.check(self.answer)

    def test_null_observations_remain_unknown(self):
        document = json.loads(self.answer["evidence"])
        document["observed"] = {"block_height": None, "timestamp_ms": None}
        self.answer["freshness"] = dict(document["observed"])
        self.replace_evidence(document)
        output = self.check(self.answer, 0)
        self.assertIn("observed block: unknown", output.splitlines())
        self.assertIn("outcome: ASK (ask)", output.splitlines())

    def test_check_ids_and_matching_answer_names_escape_newlines(self):
        name = "receiver_exists\noutcome: ALLOW (sign)\n"
        document = json.loads(self.answer["evidence"])
        document["checks"][0]["id"] = name
        self.answer["checks"][0]["name"] = name
        self.replace_evidence(document)
        output = self.check(self.answer, 0)
        escaped = json.dumps(name, ensure_ascii=False)[1:-1]
        self.assertIn(f"pass    {escaped}: alice.near exists", output.splitlines())
        self.assertEqual(len(output.splitlines()), len(document["checks"]) + 6)
        self.assertNotIn("outcome: ALLOW (sign)", output.splitlines())

    def test_check_text_escapes_unicode_separators_controls_and_formats(self):
        original = copy.deepcopy(self.answer)
        for field in ("id", "detail"):
            for char in ("\u2028", "\u2029", "\x85", "\x9b", "\u200b", "\u202e", "\U000e0001"):
                with self.subTest(field=field, char=repr(char)):
                    self.answer = copy.deepcopy(original)
                    document = json.loads(self.answer["evidence"])
                    value = "first" + char + "outcome: ALLOW (sign)"
                    document["checks"][0][field] = value
                    self.answer["checks"][0]["name" if field == "id" else field] = value
                    self.replace_evidence(document)
                    output = self.check(self.answer, 0)
                    self.assertNotIn(char, output)
                    self.assertIn("first" + json.dumps(char)[1:-1] + "outcome: ALLOW (sign)", output)
                    self.assertEqual(len(output.splitlines()), len(document["checks"]) + 6)

    def test_testnet_token_is_refused_with_matching_hash(self):
        document = json.loads(self.answer["evidence"])
        document["subject"].update(kind="ft", token="token.testnet")
        self.replace_evidence(document)
        self.check(self.answer)

    def test_non_null_issued_ms_is_refused(self):
        document = json.loads(self.answer["evidence"])
        document["issued_ms"] = 1
        self.replace_evidence(document)
        self.check(self.answer)

    def test_non_null_expires_ms_is_refused(self):
        document = json.loads(self.answer["evidence"])
        document["expires_ms"] = 2
        self.replace_evidence(document)
        self.check(self.answer)

    def test_non_null_verdict_id_is_refused(self):
        document = json.loads(self.answer["evidence"])
        document["verdict_id"] = "claimed"
        self.replace_evidence(document)
        self.check(self.answer)

    def test_non_null_memo_hash_is_refused(self):
        document = json.loads(self.answer["evidence"])
        document["subject"]["memo_sha256"] = hashlib.sha256(b"memo").hexdigest()
        self.replace_evidence(document)
        self.check(self.answer)

    def test_integer_amount_is_refused(self):
        document = json.loads(self.answer["evidence"])
        document["subject"]["amount"] = 1
        self.replace_evidence(document)
        self.check(self.answer)

    def test_non_mainnet_network_is_refused(self):
        document = json.loads(self.answer["evidence"])
        document["subject"]["network"] = "testnet"
        self.replace_evidence(document)
        self.check(self.answer)

    def test_testnet_receiver_is_refused(self):
        document = json.loads(self.answer["evidence"])
        document["subject"]["receiver"] = "alice.testnet"
        self.replace_evidence(document)
        self.check(self.answer)

    def test_duplicate_signing_and_verdict_claims_are_refused(self):
        content = '{"signing":"signed","verdict":{"signature":"claimed"},' + json.dumps(self.answer)[1:]
        output = self.check(content)
        self.assertIn("duplicate JSON key", output)

    def test_duplicate_outcome_is_refused(self):
        content = '{"outcome":"ALLOW",' + json.dumps(self.answer)[1:]
        output = self.check(content)
        self.assertIn("duplicate JSON key", output)

    def test_duplicate_receipt_keys_are_refused_at_nested_depths(self):
        content = json.dumps(self.answer)
        for original, replacement in (
                ('"block_height": 123', '"block_height": 999, "block_height": 123'),
                ('"detail": "alice.near exists"', '"detail": "claimed", "detail": "alice.near exists"')):
            with self.subTest(original=original):
                self.assertEqual(content.count(original), 1)
                output = self.check(content.replace(original, replacement))
                self.assertIn("duplicate JSON key", output)

    def test_duplicate_evidence_keys_are_refused_at_nested_depths(self):
        original = self.answer["evidence"]
        for field, value in (("detail", "claimed"), ("provider", "claimed"), ("receiver", "other.near")):
            with self.subTest(field=field):
                marker = json.dumps(field) + ":"
                duplicate = marker + json.dumps(value) + "," + marker
                self.answer["evidence"] = original.replace(marker, duplicate, 1)
                self.assertNotEqual(self.answer["evidence"], original)
                self.answer["evidence_hash"] = hashlib.sha256(self.answer["evidence"].encode()).hexdigest()
                output = self.check(self.answer)
                self.assertIn("duplicate JSON key", output)

    def test_check_details_escape_control_characters(self):
        detail = 'first\nverdict: ALLOW\r\t\b\f\x00\x1b[2J"quoted"\\tail caf\u00e9'
        document = json.loads(self.answer["evidence"])
        document["checks"][0]["detail"] = detail
        self.replace_evidence(document)
        self.answer["checks"][0]["detail"] = detail
        output = self.check(self.answer, 0)
        escaped = json.dumps(detail, ensure_ascii=False)[1:-1]
        self.assertIn(f"pass    receiver_exists: {escaped}", output.splitlines())
        self.assertNotIn("\nverdict: ALLOW", output)
        self.assertEqual(len(output.splitlines()), len(document["checks"]) + 6)
        self.assertEqual(output.splitlines()[-1], LIMITS)

    def test_altered_check_detail_is_refused(self):
        self.answer["checks"][0]["detail"] += " changed"
        self.check(self.answer)

    def test_altered_check_status_is_refused(self):
        self.answer["checks"][0]["status"] = "unknown"
        self.check(self.answer)

    def test_altered_hash_is_refused(self):
        self.answer["evidence_hash"] = "0" * 64
        self.check(self.answer)

    def test_altered_evidence_byte_is_refused(self):
        self.answer["evidence"] += " "
        self.check(self.answer)

    def test_noncanonical_evidence_is_refused_even_with_matching_hash(self):
        document = json.loads(self.answer["evidence"])
        for data in (json.dumps(document), json.dumps(dict(reversed(list(document.items()))), separators=(",", ":"))):
            with self.subTest(data=data[:30]):
                self.answer["evidence"] = data
                self.answer["evidence_hash"] = hashlib.sha256(data.encode()).hexdigest()
                self.check(self.answer)

    def test_float_is_refused_even_with_matching_hash(self):
        document = json.loads(self.answer["evidence"])
        document["latency_ms"] = 0.5
        self.answer["evidence"] = json.dumps(document, sort_keys=True, separators=(",", ":"))
        self.answer["evidence_hash"] = hashlib.sha256(self.answer["evidence"].encode()).hexdigest()
        self.check(self.answer)

    def test_empty_checks_are_refused(self):
        document = json.loads(self.answer["evidence"])
        document["checks"] = []
        self.replace_evidence(document)
        self.answer["checks"] = []
        self.check(self.answer)

    def test_allow_with_unknown_or_signal_is_refused(self):
        for status in ("unknown", "signal"):
            with self.subTest(status=status):
                document = json.loads(self.answer["evidence"])
                document["verdict"] = "ALLOW"
                document["checks"][0]["status"] = status
                self.replace_evidence(document)
                self.answer["checks"][0]["status"] = status
                self.answer.update(outcome="ALLOW", action="sign")
                self.check(self.answer)

    def test_all_reductions_use_the_live_shape(self):
        for verdict, status in (("ALLOW", "pass"), ("ALLOW", "unknown"), ("ALLOW", "signal"),
                                ("DENY", "pass"), ("UNKNOWN", "pass")):
            with self.subTest(verdict=verdict, status=status):
                document = json.loads(self.answer["evidence"])
                document["verdict"] = verdict
                for check in document["checks"]:
                    check["status"] = status
                self.replace_evidence(document)
                self.answer.update(live.LiveAPI._verdict_shape(
                    verdict, document["checks"], document["reasons"], self.answer["evidence_hash"],
                    document["observed"], document["latency_ms"]))
                self.check(self.answer, 0)

    def test_receipt_claiming_a_signature_is_refused(self):
        for field, value in (("verdict", {"signature": "claimed"}), ("signing", "signed")):
            with self.subTest(field=field):
                answer = copy.deepcopy(self.answer)
                answer[field] = value
                self.check(answer)

    def test_missing_fields_are_refused(self):
        for field in self.answer:
            with self.subTest(answer_field=field):
                answer = dict(self.answer)
                del answer[field]
                self.check(answer)
        original = json.loads(self.answer["evidence"])
        for field in original:
            with self.subTest(evidence_field=field):
                document = copy.deepcopy(original)
                del document[field]
                self.replace_evidence(document)
                self.check(self.answer)

    def test_malformed_json_is_refused(self):
        for content in ("{", "[]", "null"):
            with self.subTest(content=content):
                self.path.write_text(content, encoding="utf-8", newline="\n")
                code, output, _ = run(self.path)
                self.assertEqual(code, 1)
                self.assertTrue(output.startswith("not verified: "), output)
        self.answer["evidence"] = "{"
        self.answer["evidence_hash"] = hashlib.sha256(b"{").hexdigest()
        self.check(self.answer)

    def test_reordered_checks_and_changed_reasons_are_refused(self):
        answer = copy.deepcopy(self.answer)
        answer["checks"].reverse()
        self.check(answer)
        self.answer["reasons"] = ["changed"]
        self.check(self.answer)

    def test_changed_displayed_observation_and_latency_are_refused(self):
        for field, value in (("freshness", {"block_height": 999, "timestamp_ms": 1000000}),
                             ("latency_ms", 1)):
            with self.subTest(field=field):
                answer = copy.deepcopy(self.answer)
                answer[field] = value
                self.check(answer)

    def test_invalid_evidence_latency_is_refused(self):
        original = json.loads(self.answer["evidence"])
        for value in (True, "0", {}):
            with self.subTest(value=value):
                document = copy.deepcopy(original)
                document["latency_ms"] = value
                self.replace_evidence(document)
                self.answer["latency_ms"] = value
                self.check(self.answer)

    def test_malformed_evidence_fields_are_refused(self):
        original = json.loads(self.answer["evidence"])
        for field, value in (("subject", {}), ("observed", {}), ("checks", [{}]),
                             ("checks", None), ("reasons", "changed"), ("labels", None)):
            with self.subTest(field=field, value=value):
                document = copy.deepcopy(original)
                document[field] = value
                self.replace_evidence(document)
                self.check(self.answer)
        for value in (None, 1, {}, []):
            self.answer["evidence"] = value
            self.check(self.answer)


if __name__ == "__main__":
    unittest.main()
