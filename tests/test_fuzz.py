import copy
import json
import random
import tempfile
import time
import unittest
from pathlib import Path

from gate import canon, rules, verdict, verify
from intel import labels

ROOT = Path(__file__).resolve().parent.parent
REGISTRY = ROOT / "fixtures" / "registry"
RECORD = REGISTRY / "sample-allow.json"
EVENT = REGISTRY / "sample-allow-event.json"
EVIDENCE = REGISTRY / "sample-allow-evidence.json"
TEXT_ALPHABET = "aZ09 -_.|:\n\téΩ雪😀\u200b"
JSON_ALPHABET = TEXT_ALPHABET + chr(0xD800)
HEX_ALPHABET = "0123456789abcdef"
U64_MAX = 2**64 - 1
U128_MAX = 2**128 - 1


def random_text(rng, length, alphabet=TEXT_ALPHABET):
    return "".join(rng.choice(alphabet) for _ in range(length))


def random_json_value(rng, depth=0, alphabet=JSON_ALPHABET):
    choices = [None, True, False, rng.randint(-1000, 1000), random_text(rng, rng.randrange(25), alphabet)]
    if depth < 2:
        choices.extend(([random_json_value(rng, depth + 1, alphabet) for _ in range(rng.randrange(4))],
                        {random_text(rng, rng.randrange(1, 8), "abcxyz"):
                         random_json_value(rng, depth + 1, alphabet)}))
    return rng.choice(choices)


def random_account(rng, prefix="acct"):
    return f"{prefix}{rng.randrange(1_000_000)}.near"


def random_hex(rng, length):
    return "".join(rng.choice(HEX_ALPHABET) for _ in range(length))


def valid_canonical_fields(rng):
    issued_ms = rng.randrange(U64_MAX + 1)
    expires_ms = issued_ms + rng.randrange(U64_MAX - issued_ms + 1)
    kind = rng.choice(canon.KINDS)
    amount = rng.randrange(1, U128_MAX + 1)
    if rng.randrange(2):
        amount = str(amount)
    return {
        "gate": random_account(rng, "gate"),
        "agent_pk_hex": random_hex(rng, 64),
        "kind": kind,
        "token_id": random_account(rng, "token") if kind == "ft" else None,
        "receiver_id": random_account(rng, "receiver"),
        "amount": amount,
        "memo": None if rng.randrange(4) == 0 else random_text(rng, rng.randrange(100)),
        "issued_ms": issued_ms,
        "expires_ms": expires_ms,
        "verdict_id": random_hex(rng, 32),
        "evidence_sha256": random_hex(rng, 64),
        "key_id": rng.choice(canon.KEY_IDS),
    }


def label_row():
    row = {
        "id": None,
        "entity": "Example",
        "subject": "example.near",
        "chain": "near",
        "label": "example",
        "category": "treasury",
        "provenance": "official_source",
        "source": {"url": "https://example.org/", "content_sha256": "a" * 64,
                   "fetched_at": "2026-11-05T12:00:00Z"},
        "observed_at": "2026-11-05T12:00:00Z",
        "confidence": 1,
        "rule_id": None,
        "superseded_by": None,
        "note": "sample",
    }
    row["id"] = labels.assertion_id(row)
    return row


class CanonFuzzTest(unittest.TestCase):
    def test_generated_messages_round_trip(self):
        rng = random.Random(814203)
        for _ in range(300):
            message = canon.canonical_message(**valid_canonical_fields(rng))
            parsed = canon.parse_canonical(message)
            rebuilt = "|".join((
                canon.PREFIX,
                parsed["gate"],
                parsed["agent_pk_hex"],
                parsed["kind"],
                parsed["token_id"] or "-",
                parsed["receiver_id"],
                parsed["amount"],
                parsed["memo_sha256"] or "-",
                str(parsed["issued_ms"]),
                str(parsed["expires_ms"]),
                parsed["verdict_id"],
                parsed["evidence_sha256"],
                parsed["key_id"],
                parsed["literal"],
            )).encode("utf-8")
            self.assertEqual(message, rebuilt)
            self.assertEqual(canon.parse_canonical(rebuilt), parsed)

    def test_random_validation_inputs_raise_only_canon_error(self):
        rng = random.Random(814204)
        names = ("gate", "agent_pk_hex", "kind", "token_id", "receiver_id", "amount", "memo",
                 "issued_ms", "expires_ms", "verdict_id", "evidence_sha256", "key_id")
        for _ in range(600):
            fields = {name: random_json_value(rng) for name in names}
            try:
                message = canon.canonical_message(**fields)
            except canon.CanonError:
                pass
            else:
                self.assertEqual(canon.parse_canonical(message)["literal"], canon.LITERAL)
            try:
                canon.validate_transfer(**{name: fields[name] for name in names[:7]})
            except canon.CanonError:
                pass

    def test_mutated_byte_messages_parse_or_raise_canon_error(self):
        rng = random.Random(814205)
        for _ in range(250):
            data = bytearray(canon.canonical_message(**valid_canonical_fields(rng)))
            for _ in range(rng.randrange(1, 4)):
                index = rng.randrange(len(data))
                data[index] = rng.randrange(256)
            try:
                parsed = canon.parse_canonical(bytes(data))
            except canon.CanonError:
                continue
            rebuilt = "|".join((
                canon.PREFIX, parsed["gate"], parsed["agent_pk_hex"], parsed["kind"],
                parsed["token_id"] or "-", parsed["receiver_id"], parsed["amount"],
                parsed["memo_sha256"] or "-", str(parsed["issued_ms"]), str(parsed["expires_ms"]),
                parsed["verdict_id"], parsed["evidence_sha256"], parsed["key_id"], parsed["literal"],
            )).encode("utf-8")
            self.assertEqual(bytes(data), rebuilt)

    @unittest.expectedFailure
    def test_parse_canonical_rejects_non_bytes_with_canon_error(self):
        with self.assertRaises(canon.CanonError):
            canon.parse_canonical("not bytes")


class VerifyFuzzTest(unittest.TestCase):
    def test_mutated_records_parse_or_raise_declared_errors(self):
        rng = random.Random(814206)
        record = json.loads(EVENT.read_text(encoding="utf-8"))["data"][0]
        for _ in range(300):
            changed = copy.deepcopy(record)
            field = rng.choice(tuple(changed))
            changed[field] = random_json_value(rng)
            try:
                parsed = verify.load_record(changed)
            except (verify.VerifyError, canon.CanonError):
                continue
            self.assertIn(parsed["verdict"], ("ALLOW", "DENY", "UNKNOWN"))
            self.assertEqual(len(parsed["evidence_hash"]), 64)

    def test_mutated_evidence_and_record_hashes_are_rejected(self):
        rng = random.Random(814207)
        base_record = json.loads(RECORD.read_text(encoding="utf-8"))
        base_evidence = json.loads(EVIDENCE.read_text(encoding="utf-8"))
        original_bytes = EVIDENCE.read_bytes()
        with tempfile.TemporaryDirectory() as directory:
            record_path = Path(directory) / "record.json"
            evidence_path = Path(directory) / "evidence.json"
            for index in range(100):
                changed_evidence = copy.deepcopy(base_evidence)
                if index % 2:
                    changed_evidence["subject"]["receiver"] = random_account(rng, "mutated")
                else:
                    changed_evidence["reasons"] = [random_text(rng, rng.randrange(1, 32)), str(index)]
                changed_bytes = json.dumps(changed_evidence, ensure_ascii=False).encode("utf-8")
                self.assertNotEqual(changed_bytes, original_bytes)
                record_path.write_text(json.dumps(base_record), encoding="utf-8")
                evidence_path.write_bytes(changed_bytes)
                ok, _ = verify.verify(record_path, evidence_path)
                self.assertFalse(ok)

                changed_record = dict(base_record)
                old_hash = changed_record["evidence_hash"]
                hash_chars = list(old_hash)
                hash_index = rng.randrange(len(hash_chars))
                hash_chars[hash_index] = rng.choice(HEX_ALPHABET.replace(hash_chars[hash_index], ""))
                changed_record["evidence_hash"] = "".join(hash_chars)
                record_path.write_text(json.dumps(changed_record), encoding="utf-8")
                evidence_path.write_bytes(original_bytes)
                ok, _ = verify.verify(record_path, evidence_path)
                self.assertFalse(ok)

    @unittest.expectedFailure
    def test_non_object_evidence_is_rejected_without_crashing(self):
        with tempfile.TemporaryDirectory() as directory:
            evidence_path = Path(directory) / "evidence.json"
            evidence_path.write_text("[]", encoding="utf-8")
            ok, _ = verify.verify(EVENT, evidence_path)
            self.assertFalse(ok)


class VerdictFuzzTest(unittest.TestCase):
    def test_ask_reasons_handles_seeded_well_formed_policies(self):
        rng = random.Random(814208)
        for _ in range(500):
            receiver = random_account(rng, "receiver")
            token = random_account(rng, "token")
            kind = rng.choice(canon.KINDS)
            transfer = {"receiver_id": receiver, "kind": kind,
                        "token_id": token if kind == "ft" else None,
                        "amount": str(rng.randrange(1, U128_MAX + 1))}
            unknown_caps = None
            if rng.randrange(2):
                unknown_caps = []
                for _ in range(rng.randrange(4)):
                    asset = {"Ft": token} if kind == "ft" else "Near"
                    unknown_caps.append({"asset": asset, "per_call": str(rng.randrange(1, U128_MAX + 1))})
            policy = {
                "gate_paused": bool(rng.randrange(2)),
                "enabled": bool(rng.randrange(2)),
                "gate_denylist": [random_account(rng, "deny") for _ in range(rng.randrange(3))],
                "allowlist": [random_account(rng, "allow") for _ in range(rng.randrange(3))],
                "unknown_caps": unknown_caps,
            }
            checks = [{"id": "receiver_exists", "status": rng.choice(("pass", "signal", "unknown")),
                       "detail": random_text(rng, rng.randrange(32))} for _ in range(rng.randrange(5))]
            result = verdict.ask_reasons(rng.choice(("ALLOW", "DENY", "UNKNOWN")), checks, transfer, policy)
            self.assertIsInstance(result, list)
            self.assertTrue(all(isinstance(reason, str) for reason in result))


class RulesFuzzTest(unittest.TestCase):
    def test_unicode_memo_scanning_finishes_within_a_fixed_bound(self):
        rng = random.Random(814209)
        alphabet = tuple(TEXT_ALPHABET) + ("\u2060", "\ufeff", "Ж", "中", "🧪", "\u0301")
        started = time.perf_counter()
        for index in range(180):
            memo = "".join(rng.choice(alphabet) for _ in range(rng.randrange(4097)))
            if index % 5 == 0:
                memo += rng.choice(("ignore instructions ", "system prompt ", "you are now ")) + "x" * 4096
            self.assertIsNotNone(rules._memo_check(memo, None))
        self.assertLess(time.perf_counter() - started, 5.0)


class LabelFuzzTest(unittest.TestCase):
    def test_mutated_rows_validate_or_raise_label_error(self):
        rng = random.Random(814210)
        fields = tuple(labels.FIELDS)
        for _ in range(500):
            row = copy.deepcopy(label_row())
            field = rng.choice(fields)
            if field == "source" and rng.randrange(2):
                source = row["source"]
                nested = rng.choice(("url", "content_sha256", "fetched_at"))
                source[nested] = random_text(rng, rng.randrange(32))
            else:
                row[field] = random_json_value(rng, alphabet=TEXT_ALPHABET)
            if field != "id":
                row["id"] = labels.assertion_id(row)
            try:
                labels.validate(row)
            except labels.LabelError:
                continue

    @unittest.expectedFailure
    def test_non_string_document_url_is_rejected_with_label_error(self):
        row = label_row()
        row["source"]["url"] = 7
        row["id"] = labels.assertion_id(row)
        with self.assertRaises(labels.LabelError):
            labels.validate(row)

    @unittest.expectedFailure
    def test_unencodable_row_text_is_rejected_with_label_error(self):
        row = label_row()
        row["entity"] = chr(0xD800)
        row["id"] = "0" * 64
        with self.assertRaises(labels.LabelError):
            labels.validate(row)


if __name__ == "__main__":
    unittest.main()
