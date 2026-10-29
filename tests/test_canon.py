import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from gate import canon, ed25519

ROOT = Path(__file__).resolve().parent.parent
CANON_DIR = ROOT / "fixtures" / "canon"
EVIDENCE_DIR = ROOT / "fixtures" / "evidence"

AGENT = "ab" * 32
VERDICT_ID = "0123456789abcdef0123456789abcdef"
EVIDENCE = "e" * 64

EXPECTED_VECTORS = {
    "01-ft-happy-sandbox": "OK",
    "02-near-happy-sandbox": "OK",
    "03-memo-empty-sandbox": "OK",
    "04-ft-happy-unit": "OK",
    "05-expired-unit": "REFUSED:expired",
    "06-ttl-too-long-unit": "REFUSED:ttl_too_long",
    "07-bad-signature-unit": "REFUSED:bad_signature",
    "08-wrong-literal-unit": "REFUSED:not_allow",
    "09-prev-key-unit": "OK",
    "10-wrong-contract-unit": "REFUSED:bad_signature",
    "11-wrong-agent-unit": "REFUSED:bad_signature",
    "12-unknown-payee-sandbox": "OK",
    "13-unknown-payee-over-cap-sandbox": "REFUSED:unknown_payee_cap",
}


def fields(**changes):
    base = {
        "gate": "gate.testnet",
        "agent_pk_hex": AGENT,
        "kind": "ft",
        "token_id": "token.gate.testnet",
        "receiver_id": "vendor.testnet",
        "amount": "1000000",
        "memo": "invoice 1",
        "issued_ms": 1700000000000,
        "expires_ms": 1700000300000,
        "verdict_id": VERDICT_ID,
        "evidence_sha256": EVIDENCE,
        "key_id": "cur",
    }
    base.update(changes)
    return base


def committed_vectors():
    return [json.loads(path.read_text(encoding="utf-8")) for path in sorted(CANON_DIR.glob("*.json"))]


def vector_message(vector):
    return canon.canonical_message(
        gate=vector["gate_account_id"],
        **{name: vector[name] for name in canon.VECTOR_FIELDS[1:]},
    )


class CanonicalMessageTest(unittest.TestCase):
    def test_exact_bytes(self):
        memo_hash = hashlib.sha256(b"invoice 1").hexdigest()
        expected = (
            f"GATEv1|gate.testnet|{AGENT}|ft|token.gate.testnet|vendor.testnet|1000000|{memo_hash}"
            f"|1700000000000|1700000300000|{VERDICT_ID}|{EVIDENCE}|cur|ALLOW"
        ).encode()
        self.assertEqual(canon.canonical_message(**fields()), expected)

    def test_near_kind_has_dash_token(self):
        message = canon.canonical_message(**fields(kind="near", token_id=None))
        self.assertEqual(message.split(b"|")[4], b"-")

    def test_memo_absent_is_dash(self):
        self.assertEqual(canon.canonical_message(**fields(memo=None)).split(b"|")[7], b"-")

    def test_memo_empty_is_hashed(self):
        field = canon.canonical_message(**fields(memo="")).split(b"|")[7]
        self.assertEqual(field, hashlib.sha256(b"").hexdigest().encode("ascii"))

    def test_memo_hash_is_over_utf8_bytes(self):
        memo = "caf\u00e9"
        field = canon.canonical_message(**fields(memo=memo)).split(b"|")[7]
        self.assertEqual(field, hashlib.sha256(memo.encode("utf-8")).hexdigest().encode("ascii"))

    def test_literal_is_always_allow(self):
        for changes in ({}, {"kind": "near", "token_id": None}, {"key_id": "prev"}, {"memo": None}):
            with self.subTest(changes=changes):
                self.assertTrue(canon.canonical_message(**fields(**changes)).endswith(b"|ALLOW"))

    def test_amount_int_and_string_agree(self):
        self.assertEqual(canon.canonical_message(**fields(amount=1000000)),
                         canon.canonical_message(**fields(amount="1000000")))

    def test_amount_bounds(self):
        top = str(2**128 - 1)
        self.assertEqual(canon.canonical_message(**fields(amount=top)).split(b"|")[6], top.encode())
        self.assertEqual(canon.canonical_message(**fields(amount="1")).split(b"|")[6], b"1")

    def test_digest_is_sha256(self):
        message = canon.canonical_message(**fields())
        self.assertEqual(canon.digest(message), hashlib.sha256(message).digest())
        self.assertEqual(len(canon.digest(message)), 32)

    def test_no_trailing_newline(self):
        self.assertFalse(canon.canonical_message(**fields()).endswith(b"\n"))


class RejectionTest(unittest.TestCase):
    REJECTED = (
        ("kind", "NEAR"),
        ("kind", "nft"),
        ("kind", None),
        ("gate", "Gate.testnet"),
        ("gate", "a"),
        ("gate", "a" * 65),
        ("gate", "gate..testnet"),
        ("gate", ".gate.testnet"),
        ("gate", "gate.testnet."),
        ("gate", "gate-.testnet"),
        ("gate", "-gate.testnet"),
        ("gate", "gate__x.testnet"),
        ("gate", "gate testnet"),
        ("gate", "gate.testnet\n"),
        ("gate", "g\u0661.testnet"),
        ("gate", None),
        ("receiver_id", "vendor@testnet"),
        ("receiver_id", "VENDOR.testnet"),
        ("receiver_id", ""),
        ("token_id", None),
        ("token_id", "-"),
        ("token_id", "Token.testnet"),
        ("agent_pk_hex", "AB" * 32),
        ("agent_pk_hex", "ab" * 31),
        ("agent_pk_hex", "ab" * 33),
        ("agent_pk_hex", "zz" * 32),
        ("agent_pk_hex", b"\xab" * 32),
        ("amount", "0100"),
        ("amount", "0"),
        ("amount", 0),
        ("amount", -1),
        ("amount", "-1"),
        ("amount", "+1"),
        ("amount", " 1"),
        ("amount", "1 "),
        ("amount", "1_000"),
        ("amount", "1.0"),
        ("amount", "1e6"),
        ("amount", ""),
        ("amount", "\u0661"),
        ("amount", 2**128),
        ("amount", str(2**128)),
        ("amount", "9" * 5000),
        ("amount", True),
        ("amount", 1.0),
        ("amount", None),
        ("memo", 7),
        ("memo", b"invoice"),
        ("memo", "\ud800"),
        ("issued_ms", -1),
        ("issued_ms", 2**64),
        ("issued_ms", "1700000000000"),
        ("issued_ms", True),
        ("issued_ms", 1.5),
        ("expires_ms", None),
        ("verdict_id", VERDICT_ID.upper()),
        ("verdict_id", VERDICT_ID[:-1]),
        ("verdict_id", VERDICT_ID + "0"),
        ("verdict_id", "g" * 32),
        ("evidence_sha256", EVIDENCE.upper()),
        ("evidence_sha256", EVIDENCE[:-1]),
        ("evidence_sha256", EVIDENCE + "e"),
        ("key_id", "current"),
        ("key_id", "CUR"),
        ("key_id", None),
    )

    def test_rejections(self):
        for field, value in self.REJECTED:
            with self.subTest(field=field, value=value), self.assertRaises(canon.CanonError):
                canon.canonical_message(**fields(**{field: value}))

    def test_near_kind_rejects_a_token(self):
        for token in ("token.gate.testnet", "-"):
            with self.subTest(token=token), self.assertRaises(canon.CanonError):
                canon.canonical_message(**fields(kind="near", token_id=token))

    def test_expiry_before_issue_is_rejected(self):
        with self.assertRaises(canon.CanonError):
            canon.canonical_message(**fields(issued_ms=1700000000001, expires_ms=1700000000000))
        message = canon.canonical_message(**fields(issued_ms=5, expires_ms=5))
        self.assertEqual(message.split(b"|")[8:10], [b"5", b"5"])
        spoiled = canon.canonical_message(**fields()).replace(b"|1700000300000|", b"|1600000000000|")
        with self.assertRaises(canon.CanonError):
            canon.parse_canonical(spoiled)

    def test_account_id_bounds_accepted(self):
        for value in ("ab", "a" * 64, "a-b_c.d.near", "0" * 64):
            with self.subTest(value=value):
                self.assertEqual(canon.account_id(value, "receiver_id"), value)


class PipePropertyTest(unittest.TestCase):
    def test_pipe_rejected_in_every_string_field(self):
        for field in ("gate", "token_id", "receiver_id", "agent_pk_hex", "amount", "verdict_id",
                      "evidence_sha256", "key_id", "kind"):
            value = fields()[field]
            for spoiled in (value + "|", "|" + value, value[:1] + "|" + value[2:]):
                with self.subTest(field=field, value=spoiled), self.assertRaises(canon.CanonError):
                    canon.canonical_message(**fields(**{field: spoiled}))

    def test_pipe_in_memo_is_hashed_away(self):
        message = canon.canonical_message(**fields(memo="a|b|c"))
        self.assertEqual(message.count(b"|"), canon.FIELD_COUNT - 1)

    def test_every_message_has_fourteen_fields(self):
        for changes in ({}, {"kind": "near", "token_id": None, "memo": None}, {"memo": ""}):
            with self.subTest(changes=changes):
                self.assertEqual(len(canon.canonical_message(**fields(**changes)).split(b"|")), 14)


class ParseTest(unittest.TestCase):
    def test_round_trip(self):
        for changes in ({}, {"kind": "near", "token_id": None, "memo": None}, {"memo": ""},
                        {"key_id": "prev", "amount": str(2**128 - 1), "issued_ms": 0}):
            with self.subTest(changes=changes):
                given = fields(**changes)
                parsed = canon.parse_canonical(canon.canonical_message(**given))
                memo = given.pop("memo")
                given["memo_sha256"] = None if memo is None else hashlib.sha256(memo.encode()).hexdigest()
                given["literal"] = "ALLOW"
                self.assertEqual(parsed, given)

    def test_rejects_non_canonical(self):
        good = canon.canonical_message(**fields()).decode("utf-8")
        parts = good.split("|")
        spoiled = []
        for index, value in ((6, "0100"), (8, "01700000000000"), (0, "GATEv2"), (13, "allow"),
                             (7, "E" * 64), (4, "-"), (11, "e" * 63), (9, "1" * 21)):
            changed = list(parts)
            changed[index] = value
            spoiled.append("|".join(changed))
        spoiled += [good + "|", good + "\n", "|".join(parts[:-1])]
        for text in spoiled:
            with self.subTest(text=text), self.assertRaises(canon.CanonError):
                canon.parse_canonical(text.encode("utf-8"))

    def test_rejects_invalid_utf8(self):
        with self.assertRaises(canon.CanonError):
            canon.parse_canonical(b"GATEv1|\xff")


class VectorTest(unittest.TestCase):
    def test_committed_set(self):
        vectors = committed_vectors()
        self.assertEqual([vector["name"] for vector in vectors], sorted(EXPECTED_VECTORS))
        for vector in vectors:
            with self.subTest(name=vector["name"]):
                self.assertEqual(vector["expect_contract"], EXPECTED_VECTORS[vector["name"]])

    def test_vector_fields_and_order(self):
        keys = ["name", *canon.VECTOR_FIELDS, "literal", "message", "digest_sha256",
                "verifier_pub_hex", "signature_hex", "expect_valid", "expect_contract", "note"]
        for vector in committed_vectors():
            with self.subTest(name=vector["name"]):
                self.assertEqual(list(vector), keys)

    def test_vectors_are_consistent(self):
        verifier = ed25519.public_from_seed(ed25519.TEST_VERIFIER_SEED)
        agents = {ed25519.public_from_seed(ed25519.TEST_AGENT_SEED).hex(),
                  ed25519.public_from_seed(ed25519.TEST_AGENT2_SEED).hex()}
        for vector in committed_vectors():
            with self.subTest(name=vector["name"]):
                message = vector["message"].encode("utf-8")
                expected = vector_message(vector)
                if vector["literal"] != "ALLOW":
                    expected = expected[: -len("ALLOW")] + vector["literal"].encode("utf-8")
                self.assertEqual(message, expected)
                self.assertEqual(len(message.split(b"|")), 14)
                self.assertEqual(vector["digest_sha256"], hashlib.sha256(message).hexdigest())
                self.assertEqual(vector["verifier_pub_hex"], verifier.hex())
                self.assertIn(vector["agent_pk_hex"], agents)
                signature = bytes.fromhex(vector["signature_hex"])
                self.assertEqual(ed25519.verify(verifier, canon.digest(message), signature),
                                 vector["expect_valid"])
                parsed = canon.parse_canonical(message)
                self.assertEqual(parsed["gate"], vector["gate_account_id"])
                self.assertEqual(parsed["literal"], vector["literal"])
                evidence = (EVIDENCE_DIR / f"{vector['evidence_sha256']}.json").read_bytes()
                self.assertEqual(hashlib.sha256(evidence).hexdigest(), vector["evidence_sha256"])
                document = json.loads(evidence)
                self.assertEqual(document["verdict"], vector["literal"])
                self.assertEqual(document["checks"], [])
                subject = document["subject"]
                self.assertEqual(subject["gate"], parsed["gate"])
                self.assertEqual(subject["agent_pk"], parsed["agent_pk_hex"])
                self.assertEqual(subject["token"], parsed["token_id"])
                self.assertEqual(subject["receiver"], parsed["receiver_id"])
                self.assertEqual(subject["amount"], parsed["amount"])
                self.assertEqual(subject["memo_sha256"], parsed["memo_sha256"])
                allow = vector["literal"] == "ALLOW"
                for field in ("issued_ms", "expires_ms", "verdict_id"):
                    self.assertEqual(document[field], parsed[field] if allow else None)

    def test_vectors_07_and_08(self):
        by_name = {vector["name"]: vector for vector in committed_vectors()}
        self.assertEqual([name for name, v in by_name.items() if not v["expect_valid"]],
                         ["07-bad-signature-unit"])
        self.assertEqual([name for name, v in by_name.items() if v["literal"] != "ALLOW"],
                         ["08-wrong-literal-unit"])
        flipped = int(by_name["07-bad-signature-unit"]["signature_hex"], 16)
        original = int(by_name["04-ft-happy-unit"]["signature_hex"], 16)
        self.assertEqual((flipped ^ original).bit_count(), 1)
        self.assertEqual(by_name["07-bad-signature-unit"]["message"],
                         by_name["04-ft-happy-unit"]["message"])
        self.assertTrue(by_name["08-wrong-literal-unit"]["message"].endswith("|DENY"))

    def test_sandbox_verdict_ids_are_distinct(self):
        ids = [v["verdict_id"] for v in committed_vectors() if v["name"].endswith("-sandbox")]
        self.assertEqual(len(ids), len(set(ids)))

    def test_sandbox_notes_mark_the_root_unverified(self):
        for vector in committed_vectors():
            if vector["name"].endswith("-sandbox"):
                with self.subTest(name=vector["name"]):
                    self.assertIn("UNVERIFIED", vector["note"])

    def test_regeneration_is_byte_exact(self):
        root = committed_vectors()[0]["gate_account_id"].removeprefix("gate.")
        with tempfile.TemporaryDirectory() as tmp:
            written, removed = canon.write_vectors(Path(tmp) / "canon", root)
            self.assertEqual(removed, [])
            self.assertEqual(sorted(path.name for path in (Path(tmp) / "canon").iterdir()),
                             sorted(path.name for path in CANON_DIR.glob("*.json")))
            vector_evidence = [path.name for path in sorted(EVIDENCE_DIR.glob("*.json"))
                               if json.loads(path.read_bytes())["reasons"] == [canon.VECTOR_REASON]]
            self.assertEqual(sorted(path.name for path in (Path(tmp) / "evidence").iterdir()),
                             vector_evidence)
            for path in written:
                relative = path.relative_to(tmp)
                with self.subTest(path=relative.as_posix()):
                    self.assertEqual(path.read_bytes(), (ROOT / "fixtures" / relative).read_bytes())

    def test_gate_root_changes_only_sandbox_vectors(self):
        default = {vector["name"]: vector for vector, _ in canon.build_vectors()}
        other = {vector["name"]: vector for vector, _ in canon.build_vectors("sandbox")}
        for name, vector in default.items():
            with self.subTest(name=name):
                if name.endswith("-unit"):
                    self.assertEqual(vector, other[name])
                else:
                    self.assertEqual(other[name]["gate_account_id"], "gate.sandbox")
                    self.assertTrue(other[name]["receiver_id"].endswith(".sandbox"))

    def test_regeneration_removes_stale_vector_evidence_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            evidence_dir = Path(tmp) / "evidence"
            evidence_dir.mkdir()
            other = evidence_dir / "other.json"
            other.write_bytes(b'{"reasons":["a verdict"]}')
            first, _ = canon.write_vectors(Path(tmp) / "canon", "test.near")
            second, removed = canon.write_vectors(Path(tmp) / "canon", "sandbox")
            self.assertTrue(other.exists())
            self.assertEqual(len(removed), 5)
            self.assertTrue(all(path in first and path not in second for path in removed))
            remaining = sorted(path for path in evidence_dir.glob("*.json") if path != other)
            self.assertEqual(remaining, sorted(path for path in second if path.parent == evidence_dir))

    def test_gate_root_is_validated(self):
        with self.assertRaises(canon.CanonError):
            canon.build_vectors("Bad Root")



class VectorRelationTest(unittest.TestCase):
    """Each vector differs from its base in the way its name says, and only that way."""

    MAX_TTL_MS = 300000

    def setUp(self):
        self.v = {vector["name"]: vector for vector in committed_vectors()}

    def ttl(self, name):
        return self.v[name]["expires_ms"] - self.v[name]["issued_ms"]

    def test_04_is_valid_at_the_unit_block_time(self):
        self.assertGreater(self.v["04-ft-happy-unit"]["expires_ms"], canon.UNIT_BLOCK_MS)
        self.assertLessEqual(self.v["04-ft-happy-unit"]["issued_ms"], canon.UNIT_BLOCK_MS)
        self.assertLessEqual(self.ttl("04-ft-happy-unit"), self.MAX_TTL_MS)

    def test_05_is_expired_and_06_is_too_long(self):
        self.assertLessEqual(self.v["05-expired-unit"]["expires_ms"], canon.UNIT_BLOCK_MS)
        self.assertLessEqual(self.ttl("05-expired-unit"), self.MAX_TTL_MS)
        self.assertGreater(self.ttl("06-ttl-too-long-unit"), self.MAX_TTL_MS)
        self.assertGreater(self.v["06-ttl-too-long-unit"]["expires_ms"], canon.UNIT_BLOCK_MS)

    def test_sandbox_vectors_are_valid_far_ahead(self):
        for name, vector in self.v.items():
            if name.endswith("-sandbox"):
                with self.subTest(name=name):
                    self.assertEqual(self.ttl(name), self.MAX_TTL_MS)
                    self.assertGreater(vector["issued_ms"], 4000000000000)

    def test_10_names_another_gate(self):
        gates = {name: vector["gate_account_id"] for name, vector in self.v.items()
                 if name.endswith("-unit")}
        self.assertEqual(gates.pop("10-wrong-contract-unit"), "other.testnet")
        self.assertEqual(set(gates.values()), {"gate.testnet"})

    def test_11_alone_is_agent_2(self):
        agent1 = ed25519.public_from_seed(ed25519.TEST_AGENT_SEED).hex()
        agent2 = ed25519.public_from_seed(ed25519.TEST_AGENT2_SEED).hex()
        for name, vector in self.v.items():
            with self.subTest(name=name):
                expected = agent2 if name == "11-wrong-agent-unit" else agent1
                self.assertEqual(vector["agent_pk_hex"], expected)

    def test_09_alone_uses_the_previous_key(self):
        self.assertEqual([n for n, v in self.v.items() if v["key_id"] != "cur"], ["09-prev-key-unit"])

    def test_12_and_13_differ_only_in_amount(self):
        low, high = self.v["12-unknown-payee-sandbox"], self.v["13-unknown-payee-over-cap-sandbox"]
        self.assertEqual((low["amount"], high["amount"]), ("1", "1000000"))
        self.assertEqual(low["receiver_id"], high["receiver_id"])
        self.assertTrue(low["receiver_id"].startswith("stranger."))
        self.assertNotEqual(low["receiver_id"], self.v["01-ft-happy-sandbox"]["receiver_id"])
        for field in ("gate_account_id", "token_id", "kind", "memo", "issued_ms", "expires_ms"):
            self.assertEqual(low[field], high[field])
            self.assertEqual(low[field], self.v["01-ft-happy-sandbox"][field])

    def test_02_is_native_and_03_has_the_empty_memo(self):
        native = self.v["02-near-happy-sandbox"]
        self.assertEqual((native["kind"], native["token_id"], native["memo"]), ("near", None, None))
        self.assertEqual(native["amount"], str(10**24))
        self.assertEqual(self.v["03-memo-empty-sandbox"]["memo"], "")


if __name__ == "__main__":
    unittest.main()
