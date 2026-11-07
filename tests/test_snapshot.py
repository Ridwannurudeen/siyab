"""intel/snapshot.py and python -m intel snapshot: a scoped export of the label store whose content hash
verifies, whose rows are the store's rows with their provenance, source and age unchanged, and whose bytes
depend only on the store and the scope."""

import contextlib
import hashlib
import io
import json
import tempfile
import unittest
from pathlib import Path

from intel import __main__ as intel_main
from intel import labels, snapshot

ROOT = Path(__file__).resolve().parent.parent
SPEC = ROOT / "fixtures" / "spec"
SCOPE_FILE = SPEC / "snapshot-v1-scope.json"


def scope_file():
    return json.loads(SCOPE_FILE.read_text(encoding="utf-8"))


def rehashed(document):
    """The canonical bytes of the document with its content_sha256 recomputed, so that only the rule under
    test can refuse it."""
    document["content_sha256"] = snapshot.content_sha256(document)
    return snapshot.canonical_bytes(document)


def run(*argv):
    """(exit code, stdout, stderr) of python -m intel with argv."""
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = intel_main.main(list(argv))
    return code, out.getvalue(), err.getvalue()


class CommittedStoreTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.store = labels.load()
        cls.stored = {}
        for line in labels.STORE_PATH.read_text(encoding="utf-8").splitlines():
            if line.strip():
                row = json.loads(line)
                cls.stored[row["id"]] = row
        cls.document = snapshot.build(cls.store, scope_file())
        cls.data = snapshot.canonical_bytes(cls.document)
        cls.rows = json.loads(cls.data)["rows"]

    def test_the_content_hash_verifies(self):
        self.assertEqual(snapshot.check(self.data), self.document)
        body = {key: value for key, value in json.loads(self.data).items() if key != "content_sha256"}
        text = json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        self.assertEqual(hashlib.sha256(text.encode("utf-8")).hexdigest(), self.document["content_sha256"])
        self.assertEqual(self.document["store_sha256"], hashlib.sha256(labels.STORE_PATH.read_bytes()).hexdigest())

    def test_rows_keep_their_provenance_source_and_age(self):
        self.assertTrue(self.rows)
        for row in self.rows:
            stored = self.stored[row["id"]]
            with self.subTest(id=row["id"]):
                self.assertEqual(snapshot.canonical_bytes(row), snapshot.canonical_bytes(stored))
                self.assertEqual(labels.assertion_id(row), row["id"])
                labels.validate(row)
                for key in ("provenance", "source", "observed_at", "confidence", "rule_id", "superseded_by"):
                    self.assertEqual((type(row[key]), row[key]), (type(stored[key]), stored[key]))
        self.assertLessEqual({"onchain", "official_source", "third_party", "inferred"},
                             {row["provenance"] for row in self.rows})
        for row in self.rows:
            if row["provenance"] == "inferred":
                with self.subTest(inferred=row["id"]):
                    self.assertEqual(row["rule_id"], row["source"]["rule_id"])
                    self.assertTrue(row["source"]["inputs"])

    def test_the_rows_are_every_current_row_the_scope_selects_and_no_other(self):
        refusal, payees = scope_file()["select"]
        self.assertEqual(sorted(refusal), ["categories"])
        self.assertEqual(sorted(payees), ["chains", "subjects"])
        expected = sorted(
            (row for row in self.stored.values() if row["superseded_by"] is None and (
                row["category"] in refusal["categories"]
                or (row["chain"] in payees["chains"] and row["subject"] in payees["subjects"]))),
            key=lambda row: (row["chain"], row["subject"], row["id"]))
        self.assertEqual([row["id"] for row in self.rows], [row["id"] for row in expected])
        held = {row["subject"] for row in self.rows}
        self.assertNotIn("v2.ref-finance.near", held)
        self.assertFalse(self.store.by_subject("v2.ref-finance.near", "near"))

    def test_every_inferred_refusal_row_sits_beside_a_cited_incident_row(self):
        cited = {(row["subject"], row["chain"]) for row in self.rows
                 if row["category"] == "incident" and row["provenance"] in ("third_party", "onchain")}
        inferred = [row for row in self.rows
                    if row["provenance"] == "inferred" and row["category"] in ("denylist", "incident")]
        self.assertTrue(inferred)
        for row in inferred:
            with self.subTest(id=row["id"]):
                self.assertIn((row["subject"], row["chain"]), cited)

    def test_the_output_is_deterministic(self):
        self.assertEqual(snapshot.canonical_bytes(snapshot.build(labels.load(), scope_file())), self.data)
        reordered = scope_file()
        reordered["select"] = [{key: list(reversed(values)) for key, values in reversed(selector.items())}
                               for selector in reversed(reordered["select"])]
        self.assertNotEqual(reordered, scope_file())
        self.assertEqual(snapshot.canonical_bytes(snapshot.build(self.store, reordered)), self.data)
        with tempfile.TemporaryDirectory() as tmp:
            written = []
            for name in ("first.json", "second.json"):
                out = Path(tmp) / name
                self.assertEqual(run("snapshot", "--scope", str(SCOPE_FILE), "--out", str(out))[0], 0)
                written.append(out.read_bytes())
        self.assertEqual(written, [self.data, self.data])

    def test_the_command_writes_the_snapshot_and_prints_the_counts_and_both_hashes(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "nested" / "snapshot.json"
            code, printed, errors = run("snapshot", "--scope", str(SCOPE_FILE), "--out", str(out))
            self.assertEqual((code, errors), (0, ""))
            self.assertEqual(snapshot.check(out.read_bytes()), self.document)
        subjects = {(row["chain"], row["subject"]) for row in self.rows}
        self.assertEqual(printed.splitlines(), [
            f"{len(self.rows)} rows over {len(subjects)} subjects written to {out}",
            f"store_sha256 {self.store.sha256}",
            f"content_sha256 {self.document['content_sha256']}",
        ])

    def test_the_command_refuses_an_invalid_scope_and_writes_nothing(self):
        cases = {
            "not-json": "{",
            "no-select": json.dumps({"name": "x"}),
            "unknown-category": json.dumps({"name": "x", "select": [{"categories": ["scam"]}]}),
        }
        with tempfile.TemporaryDirectory() as tmp:
            for name, text in [*cases.items(), ("missing-file", None)]:
                with self.subTest(case=name):
                    scope_path = Path(tmp) / f"{name}.json"
                    if text is not None:
                        scope_path.write_text(text, encoding="utf-8")
                    out = Path(tmp) / f"{name}-snapshot.json"
                    code, printed, errors = run("snapshot", "--scope", str(scope_path), "--out", str(out))
                    self.assertEqual((code, printed), (2, ""))
                    self.assertTrue(errors.startswith(f"stopped: {scope_path}: "), errors)
                    self.assertFalse(out.exists())


class ScopeTest(unittest.TestCase):
    def test_lists_and_selectors_are_sorted(self):
        written = {"name": "n", "select": [{"subjects": ["b.near", "a.near"], "chains": ["near"]},
                                           {"categories": ["incident", "denylist"]}]}
        self.assertEqual(snapshot.scope(written), {"name": "n", "select": [
            {"categories": ["denylist", "incident"]}, {"chains": ["near"], "subjects": ["a.near", "b.near"]}]})
        self.assertEqual(snapshot.scope(snapshot.scope(written)), snapshot.scope(written))

    def test_a_broken_rule_is_refused(self):
        good = {"categories": ["incident"]}
        cases = {
            "not an object": [good],
            "no name": {"select": [good]},
            "an extra key": {"name": "n", "select": [good], "chains": ["near"]},
            "an empty name": {"name": "", "select": [good]},
            "a name that is not a string": {"name": 1, "select": [good]},
            "no selector": {"name": "n", "select": []},
            "select not a list": {"name": "n", "select": good},
            "an empty selector": {"name": "n", "select": [{}]},
            "an unknown selector key": {"name": "n", "select": [{"provenances": ["onchain"]}]},
            "an empty list": {"name": "n", "select": [{"subjects": []}]},
            "a list that is a string": {"name": "n", "select": [{"subjects": "wrap.near"}]},
            "an entry that is not a string": {"name": "n", "select": [{"subjects": [1]}]},
            "an empty entry": {"name": "n", "select": [{"subjects": [""]}]},
            "an upper case chain": {"name": "n", "select": [{"chains": ["NEAR"]}]},
            "an unknown category": {"name": "n", "select": [{"categories": ["scam"]}]},
            "an entry twice": {"name": "n", "select": [{"subjects": ["a.near", "a.near"]}]},
            "a selector twice": {"name": "n", "select": [{"subjects": ["a.near", "b.near"]},
                                                         {"subjects": ["b.near", "a.near"]}]},
        }
        for name, value in cases.items():
            with self.subTest(case=name):
                with self.assertRaises(snapshot.SnapshotError):
                    snapshot.scope(value)


class CheckTest(unittest.TestCase):
    """Every rule of the reader, each broken once on a snapshot of the vector store."""

    @classmethod
    def setUpClass(cls):
        vectors = json.loads((SPEC / "snapshot-v1.json").read_text(encoding="utf-8"))
        cls.store = labels.load(ROOT / vectors["store"])
        cls.scope = vectors["vectors"][0]["scope"]

    def setUp(self):
        self.document = snapshot.build(self.store, self.scope)
        self.data = snapshot.canonical_bytes(self.document)

    def other(self, subject):
        """The one row of the vector store on subject that the snapshot does not hold."""
        held = {row["id"] for row in self.document["rows"]}
        found = [row for row in (json.loads(line) for line in (ROOT / "fixtures" / "spec" / "snapshot-v1-store.jsonl")
                                 .read_text(encoding="utf-8").splitlines())
                 if row["subject"] == subject and row["id"] not in held]
        self.assertEqual(len(found), 1)
        return found[0]

    def refused(self, data, reason):
        with self.assertRaisesRegex(snapshot.SnapshotError, reason):
            snapshot.check(data)

    def test_the_untouched_snapshot_passes(self):
        self.assertEqual(snapshot.check(self.data), self.document)

    def test_a_superseded_row_is_never_exported(self):
        older = self.other("renamed-example.testnet")
        self.assertIsNotNone(older["superseded_by"])
        self.assertIn(older["superseded_by"], {row["id"] for row in self.document["rows"]})
        self.document["rows"].append(older)
        self.document["rows"].sort(key=lambda row: (row["chain"], row["subject"], row["id"]))
        self.refused(rehashed(self.document), "superseded or outside the scope")

    def test_a_row_outside_the_scope_is_refused(self):
        self.document["rows"].insert(0, self.other("0x2CfF890f0378a11913B6129B2E97417a2c302680"))
        self.refused(rehashed(self.document), "superseded or outside the scope")

    def test_an_edited_row_is_refused(self):
        self.document["rows"][0]["note"] += " edited"
        self.refused(rehashed(self.document), r"rows\[0\]: id: ")
        self.setUp()
        self.document["rows"][0]["provenance"] = "onchain"
        self.refused(rehashed(self.document), r"rows\[0\]: ")

    def test_rows_out_of_order_or_twice_are_refused(self):
        rows = self.document["rows"]
        rows[0], rows[1] = rows[1], rows[0]
        self.refused(rehashed(self.document), "sorted by chain, subject and id")
        self.setUp()
        self.document["rows"].insert(1, self.document["rows"][0])
        self.refused(rehashed(self.document), "sorted by chain, subject and id")

    def test_a_changed_content_hash_or_a_dropped_row_is_refused(self):
        dropped = json.loads(self.data)
        dropped["rows"].pop()
        self.refused(snapshot.canonical_bytes(dropped), "content_sha256")
        changed = json.loads(self.data)
        changed["content_sha256"] = hashlib.sha256(b"another document").hexdigest()
        self.refused(snapshot.canonical_bytes(changed), "content_sha256")

    def test_bytes_that_are_not_canonical_are_refused(self):
        self.refused(json.dumps(self.document, indent=2, ensure_ascii=False).encode("utf-8"), "canonical form")
        self.refused(self.data + b"\n", "canonical form")
        self.refused(json.dumps(self.document, sort_keys=True, separators=(",", ":")).encode("utf-8"),
                     "canonical form")
        self.refused(b"\xff", "not a UTF-8 JSON document")

    def test_the_envelope_rules_are_refused(self):
        cases = (
            ({"format": "label-store"}, "format"),
            ({"version": "2"}, "format"),
            ({"store_sha256": "7E"}, "store_sha256"),
            ({"rows": {}}, "rows: a list"),
            ({"scope": {"name": "n", "select": [{"categories": ["incident", "denylist"]}]}}, "normalized form"),
            ({"extra": 1}, "exactly the keys"),
        )
        for change, reason in cases:
            with self.subTest(change=change):
                document = {**self.document, **change}
                self.refused(rehashed(document), reason)


if __name__ == "__main__":
    unittest.main()
