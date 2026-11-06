"""docs/spec/: every fixture path a page cites exists, every vector a page names reproduces with the repository's own
functions, and every format page states its version."""

import hashlib
import json
import re
import unittest
from pathlib import Path

from gate import base58, canon, ed25519, evidence, verify
from intel import labels

ROOT = Path(__file__).resolve().parent.parent
SPEC = ROOT / "docs" / "spec"
FIXTURES = ROOT / "fixtures"
PAGES = ("label-assertion-v1.md", "evidence-v1.md", "verdict-v1.md", "registry-record-v1.md", "report-v1.md")
CITED_PATH = re.compile(r"`(fixtures/[^`\s]*)`")
VERSION_LINE = re.compile(r"^Version: (\d+)( \(.+\))?$", re.MULTILINE)
VIEW_KEYS = ["verdict", "evidence_hash", "observed_block", "recorded_ms", "verdict_id"]
EVENT_KEYS = ["standard", "version", "event", "data"]


def page(name):
    return (SPEC / name).read_text(encoding="utf-8")


def load_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def test_key():
    return (FIXTURES / "keys" / "test-verifier.pub").read_text(encoding="ascii").splitlines()[0]


class PageTest(unittest.TestCase):
    def test_every_cited_fixture_path_exists(self):
        for name in ("README.md", *PAGES):
            cited = CITED_PATH.findall(page(name))
            if name != "README.md":
                self.assertTrue(cited, f"{name} cites no fixture")
            for path in cited:
                with self.subTest(page=name, path=path):
                    # A shortened hash is written as its first 8 hex and "...": match it as a prefix.
                    pattern = path.replace("...", "*")
                    if "*" in pattern:
                        self.assertTrue(list(ROOT.glob(pattern)), f"no file matches {path}")
                    else:
                        self.assertTrue((ROOT / path).exists(), f"{path} does not exist")

    def test_every_format_page_states_its_version(self):
        for name in PAGES:
            with self.subTest(page=name):
                match = VERSION_LINE.search(page(name))
                self.assertIsNotNone(match, f"{name} has no Version line")
                self.assertEqual(f"-v{match.group(1)}.md", name[name.rindex("-v"):])

    def test_the_index_lists_every_page(self):
        index = page("README.md")
        self.assertEqual(sorted(path.name for path in SPEC.glob("*-v*.md")), sorted(PAGES))
        for name in PAGES:
            with self.subTest(page=name):
                self.assertIn(f"]({name})", index)


class LabelAssertionTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.store = labels.load()
        cls.lines = set(labels.STORE_PATH.read_text(encoding="utf-8").splitlines())

    def test_vectors_reproduce(self):
        document = load_json(FIXTURES / "spec" / "label-assertion-v1.json")
        self.assertEqual((document["format"], document["version"]), ("label-assertion", "1"))
        self.assertEqual({vector["row"]["provenance"] for vector in document["vectors"]}, set(labels.PROVENANCES))
        for vector in document["vectors"]:
            with self.subTest(name=vector["name"]):
                row = vector["row"]
                labels.validate(row)
                self.assertEqual(labels.assertion_id(row), vector["id"])
                self.assertEqual(row["id"], vector["id"])
                self.assertEqual(hashlib.sha256(vector["canonical_body"].encode("utf-8")).hexdigest(), vector["id"])
                body = {key: value for key, value in row.items() if key not in ("id", "superseded_by")}
                self.assertEqual(json.loads(vector["canonical_body"]), body)
                self.assertEqual(labels.row_line(row), vector["line"])
                self.assertEqual(vector["line"] in self.lines, vector["in_store"])

    def test_registry_label_events_name_store_rows(self):
        events = load_json(FIXTURES / "registry" / "labels-20261103.json")
        self.assertIn(f"{len(events)} `label_recorded` events", page("label-assertion-v1.md"))
        by_id = {assertion.id: assertion for assertion in self.store.assertions}
        for event in events:
            data = event["data"][0]
            with self.subTest(assertion_hash=data["assertion_hash"]):
                self.assertEqual(list(event), EVENT_KEYS)
                self.assertEqual(list(data), ["subject", "chain", "label", "category", "provenance", "assertion_hash"])
                assertion = by_id[data["assertion_hash"]]
                self.assertIsNone(assertion.superseded_by)
                for key in ("subject", "chain", "label", "category", "provenance"):
                    self.assertEqual(getattr(assertion, key), data[key])


class EvidenceTest(unittest.TestCase):
    def test_stored_documents_check(self):
        paths = sorted((FIXTURES / "evidence").glob("*.json"))
        self.assertIn(f"{len(paths)} stored documents", page("evidence-v1.md"))
        for path in paths:
            with self.subTest(path=path.name):
                self.assertTrue(evidence.check_file(path))

    def test_worked_example_is_vector_04(self):
        vector = load_json(FIXTURES / "canon" / "04-ft-happy-unit.json")
        data = (FIXTURES / "evidence" / f"{vector['evidence_sha256']}.json").read_bytes()
        self.assertEqual(evidence.canonical_bytes(json.loads(data)), data)
        self.assertEqual(json.loads(data)["subject"]["memo_sha256"], hashlib.sha256(b"invoice 1").hexdigest())
        self.assertIn(f"`{vector['evidence_sha256'][:8]}...json`", page("evidence-v1.md"))


class VerdictTest(unittest.TestCase):
    def test_vectors_regenerate_and_verify(self):
        paths = sorted((FIXTURES / "canon").glob("*.json"))
        self.assertIn(f"{len(paths)} vectors", page("verdict-v1.md"))
        committed = [load_json(path) for path in paths]
        root = committed[0]["gate_account_id"].removeprefix("gate.")
        built = canon.build_vectors(root)
        self.assertEqual([vector for vector, _ in built], committed)
        for vector, document in built:
            with self.subTest(name=vector["name"]):
                self.assertEqual(evidence.evidence_hash(document), vector["evidence_sha256"])
                self.assertTrue(evidence.check_file(FIXTURES / "evidence" / f"{vector['evidence_sha256']}.json"))
                message = vector["message"].encode("utf-8")
                parsed = canon.parse_canonical(message)
                self.assertEqual(parsed["literal"], vector["literal"])
                self.assertEqual(canon.digest(message).hex(), vector["digest_sha256"])
                valid = ed25519.verify(bytes.fromhex(vector["verifier_pub_hex"]), canon.digest(message),
                                       bytes.fromhex(vector["signature_hex"]))
                self.assertEqual(valid, vector["expect_valid"])

    def test_worked_example_is_vector_04(self):
        vector = load_json(FIXTURES / "canon" / "04-ft-happy-unit.json")
        shown = page("verdict-v1.md")
        parts = vector["message"].split("|")
        for index in (2, 7, 11):
            parts[index] = parts[index][:8] + "..."
        self.assertIn("|".join(parts), shown)
        self.assertIn(f"`{vector['digest_sha256'][:8]}...`", shown)
        self.assertIn(f"`{vector['signature_hex'][:8]}...`", shown)

    def test_test_verifier_key_file(self):
        public = ed25519.public_from_seed(ed25519.TEST_VERIFIER_SEED)
        self.assertEqual((FIXTURES / "keys" / "test-verifier.pub").read_bytes(),
                         f"{base58.encode_key(public)}\n{public.hex()}\n".encode("ascii"))


class RegistryRecordTest(unittest.TestCase):
    def test_sample_allow_verifies(self):
        registry = FIXTURES / "registry"
        for record in ("sample-allow.json", "sample-allow-event.json"):
            with self.subTest(record=record):
                ok, lines = verify.verify(registry / record, registry / "sample-allow-evidence.json",
                                          registry / "sample-allow-verdict.json", test_key())
                self.assertTrue(ok, lines)

    def test_testnet_records_check_against_their_evidence(self):
        registry = FIXTURES / "registry"
        paths = [registry / "event-087cff-20261103.json", *sorted(registry.glob("testnet-*.json"))]
        self.assertEqual(len(paths), 9)
        for path in paths:
            with self.subTest(path=path.name):
                raw = load_json(path)
                record = verify.load_record(raw)
                if record["shape"] == "event":
                    self.assertEqual(list(raw), EVENT_KEYS)
                    self.assertEqual(list(raw["data"][0]), ["subject", *VIEW_KEYS[:3], "verdict_id"])
                else:
                    self.assertEqual(list(raw), VIEW_KEYS)
                self.assertIn(raw.get("verdict") or raw["data"][0]["verdict"], ("Allow", "Deny", "Unknown"))
                evidence_path = FIXTURES / "evidence" / f"{record['evidence_hash']}.json"
                ok, lines = verify.verify(path, evidence_path)
                self.assertTrue(ok, lines)
                document = load_json(evidence_path)
                if record["verdict"] == "ALLOW":
                    self.assertEqual(record["verdict_id"], document["verdict_id"])
                else:
                    self.assertEqual(record["verdict_id"], record["evidence_hash"][:32])


class ReportTest(unittest.TestCase):
    def test_body_vector_reproduces(self):
        vector = load_json(FIXTURES / "records" / "body-vector.json")
        body = vector["input"]
        self.assertEqual(list(body), ["evidence_sha256", "evidence_url", "kind", "outcome", "subject", "submitter"])
        self.assertTrue(all(isinstance(value, str) for value in body.values()))
        self.assertIn("naïve".encode("utf-8"), evidence.canonical_bytes(body))
        self.assertEqual(evidence.evidence_hash(body), vector["expected_sha256"])
        self.assertIn(f"`{vector['expected_sha256'][:8]}...`", page("report-v1.md"))

    def test_enumerations_match_the_canonical_json(self):
        enums = load_json(ROOT / "intel" / "record_enums.json")
        shown = page("report-v1.md")
        for name, values in enums.items():
            with self.subTest(name=name):
                self.assertIn(f"| {name} | " + ", ".join(f"`{value}`" for value in values) + " |", shown)


if __name__ == "__main__":
    unittest.main()
