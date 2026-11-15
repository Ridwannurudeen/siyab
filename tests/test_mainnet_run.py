import hashlib
import json
import re
import unittest
from pathlib import Path

from gate import base58, canon, ed25519, evidence

ROOT = Path(__file__).resolve().parent.parent
RECORD = ROOT / "fixtures" / "runs" / "gate-mainnet-20261115.txt"
ALLOW_HASH = "b7805115bbbafceb9af3b9891e796b68c19e8f1bdd119a237d552ec0879f9518"
DENY_HASH = "ac6558987c40d07920a1c74dcf2a8dd56a6c832619cb719ddd6953557295665e"
ALLOW_ID = "cda042b301257caf3cdbbce62a424f3b"
PUBLIC_KEY = "ed25519:77nn97D9twagzd1h5HYsdxySe4sNrCLwQwR7vi66Y9ZW"
TRANSACTIONS = (
    ("deploy + new", "4adrzFfU4F9fQDgo2o1xnb5mvoDf9QrdrzBQViTAZALq"),
    ("register_agent", "AeYvbSX7H8f1oRCgaRG5UiRhrNXrCArioHkD7Qzujx9X"),
    ("set_allowlist", "7TtHtnBTdkx66gyV4TEpXQkGyEKmLWfA9aEBo7NNJXkt"),
    ("refused request_transfer", "CMfWZvQnK5NUH5PqndbophW3RSas5BadQxhFhbgDboK7"),
    ("allowed request_transfer", "HTjDizXtxSmb96PqdBTyEBqKbzvarKiCeck8GNGzFNCW"),
    ("set_paused true", "EFb3MPRvz7PrB1TQ6LG86NAteM4ZdmmUDFAmD1eCS8xc"),
    ("revoke_agent", "ZvP8BEnBMXAdpsnxyd67Y67JS8tyosA2Ha6RwdjUu2S"),
)


class MainnetRunTest(unittest.TestCase):
    def setUp(self):
        self.record = RECORD.read_text(encoding="utf-8")

    def test_evidence_bytes_hash_to_their_names(self):
        for digest in (ALLOW_HASH, DENY_HASH):
            with self.subTest(digest=digest):
                path = ROOT / "fixtures" / "evidence" / f"{digest}.json"
                self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), path.stem)
                self.assertTrue(evidence.check_file(path))
                self.assertIn(path.relative_to(ROOT).as_posix(), self.record)

    def test_allow_matches_record_and_mainnet_signature(self):
        verdict_path = ROOT / "fixtures" / f"{ALLOW_ID}.verdict.json"
        verdict = json.loads(verdict_path.read_bytes())
        document = json.loads((ROOT / "fixtures" / "evidence" / f"{ALLOW_HASH}.json").read_bytes())
        subject_line = re.search(r"^ALLOW subject: (.+)$", self.record, re.MULTILINE)
        self.assertIsNotNone(subject_line)
        subject = json.loads(subject_line.group(1))
        self.assertEqual(subject, {
            "gate": "siyabgate.near",
            "agent_pk": base58.decode_key("ed25519:4H9pDNonqT2MMjAzcmPRWYkAcYLYwQZpLwym4dp6Fqm6").hex(),
            "kind": "near",
            "token": None,
            "receiver": "maryamnurudeen.near",
            "amount": "10000000000000000000000",
            "memo_sha256": None,
        })
        self.assertEqual(document["subject"], subject)
        self.assertEqual(verdict["literal"], "ALLOW")
        self.assertEqual(document["verdict"], verdict["literal"])
        self.assertEqual(verdict["evidence_hash"], ALLOW_HASH)
        self.assertEqual(verdict["verdict_id"], ALLOW_ID)
        for field in ("issued_ms", "expires_ms", "verdict_id"):
            self.assertEqual(verdict[field], document[field])
            self.assertIn(f"{field} {verdict[field]}", self.record)
        self.assertIn(f"evidence {verdict['evidence_hash']}", self.record)
        self.assertIn(f"Verifier public key: {PUBLIC_KEY}", self.record)
        self.assertIn(verdict_path.relative_to(ROOT).as_posix(), self.record)
        message = canon.canonical_message(
            gate=subject["gate"], agent_pk_hex=subject["agent_pk"], kind=subject["kind"],
            token_id=subject["token"], receiver_id=subject["receiver"], amount=subject["amount"],
            memo=None, issued_ms=verdict["issued_ms"], expires_ms=verdict["expires_ms"],
            verdict_id=verdict["verdict_id"], evidence_sha256=verdict["evidence_hash"],
            key_id=verdict["key_id"])
        self.assertTrue(ed25519.verify(base58.decode_key(PUBLIC_KEY), canon.digest(message),
                                       bytes.fromhex(verdict["signature"])))

    def test_deny_has_no_signature(self):
        path = ROOT / "fixtures" / f"{DENY_HASH}.verdict.json"
        verdict = json.loads(path.read_bytes())
        self.assertEqual(verdict, {"literal": "DENY", "evidence_hash": DENY_HASH})
        document = json.loads((ROOT / "fixtures" / "evidence" / f"{DENY_HASH}.json").read_bytes())
        self.assertEqual(document["verdict"], "DENY")
        self.assertIsNone(document["verdict_id"])
        self.assertIn(f"evidence {verdict['evidence_hash']}", self.record)
        self.assertIn(path.relative_to(ROOT).as_posix(), self.record)

    def test_every_run_transaction_is_named_in_step_order(self):
        steps = re.findall(r"^([1-7]) (.+?)  tx (\S+)$", self.record, re.MULTILINE)
        self.assertEqual(steps, [(str(index), step, tx) for index, (step, tx)
                                 in enumerate(TRANSACTIONS, start=1)])

    def test_revocation_readbacks_are_retained(self):
        self.assertIn("agent_revoked; get_agent null", self.record)
        self.assertIn("only ed25519:7TPhZL78c12nxfn4RSk6f8rd59f3AA1rsMD5JSk6rAmz FullAccess", self.record)
        self.assertIn("get_policy paused true", self.record)
        self.assertIn("Repo: siyab-hold 30d0d4bbb", self.record)


if __name__ == "__main__":
    unittest.main()
