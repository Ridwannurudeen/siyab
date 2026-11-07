import importlib.util
import json
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def load():
    spec = importlib.util.spec_from_file_location("check_hygiene", ROOT / "scripts" / "check_hygiene.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class WordRuleTest(unittest.TestCase):
    def setUp(self):
        self.hygiene = load()
        self.word = "safe"

    def test_authored_paths_reject_the_word(self):
        line = f"the account is {self.word}"
        for path in ("README.md", "AGENTS.md", "intel/seeds/README.md", "mcp/server.py", "scripts/counts.py",
                     "gate/rules.py", "intel/seed.py", "docs/LABEL-RECORD.md"):
            with self.subTest(path=path):
                self.assertTrue(self.hygiene.check_text(path, line))

    def test_quoted_source_data_in_docs_is_rejected_too(self):
        self.assertTrue(self.hygiene.check_text("docs/LABEL-RECORD.md", f'symbol "{self.word.upper()}"'))

    def test_data_paths_and_the_script_itself_are_outside_the_rule(self):
        line = f"the account is {self.word}"
        for path in ("intel/labels.jsonl", "intel/seeds/raw/x.json", "fixtures/rpc/x.json", "tests/test_x.py",
                     "scripts/check_hygiene.py"):
            with self.subTest(path=path):
                self.assertFalse(self.hygiene.check_text(path, line))


class ParsedLabelRowTest(unittest.TestCase):
    def setUp(self):
        self.hygiene = load()

    def check_row(self, row):
        return self.hygiene.check_bytes("intel/labels.jsonl", (json.dumps(row) + "\n").encode())

    def test_hostile_role_word_without_source_url_fails(self):
        hits = self.check_row({"note": "listed as attacker", "category": "incident", "source": {}})
        self.assertEqual(hits, [(1, "hostile role word without a source URL")])

    def test_hostile_role_word_with_source_url_passes(self):
        hits = self.check_row({"note": "listed as attacker", "category": "incident",
                               "source": {"url": "https://example.com/report"}})
        self.assertFalse(hits)

    def test_hostile_role_word_in_category_fails(self):
        hits = self.check_row({"note": "a documented event", "category": "attacker", "source": {}})
        self.assertEqual(hits, [(1, "hostile role word without a source URL")])

    def test_hostile_role_word_in_label_without_source_url_fails(self):
        hits = self.check_row({"label": "attacker", "source": {}})
        self.assertEqual(hits, [(1, "hostile role word without a source URL")])

    def test_hostile_role_word_in_label_with_source_url_passes(self):
        hits = self.check_row({"label": "attacker",
                               "source": {"url": "https://example.com/report"}})
        self.assertFalse(hits)

    def test_hostile_role_word_with_a_url_without_a_host_fails(self):
        for url in ("https://@", "https://user@", "https://"):
            hits = self.check_row({"label": "attacker", "source": {"url": url}})
            self.assertEqual(hits, [(1, "hostile role word without a source URL")], url)

    def test_hostile_word_outside_note_and_category_does_not_trigger(self):
        hits = self.check_row({"note": "a documented event", "category": "incident", "source": {},
                               "other": "attacker"})
        self.assertFalse(hits)

    def test_invalid_json_line_fails(self):
        hits = self.hygiene.check_bytes("intel/labels.jsonl", b"not json\n")
        self.assertEqual(hits, [(1, "invalid JSONL")])

    def test_non_json_constants_fail(self):
        for value in ("NaN", "Infinity", "-Infinity"):
            with self.subTest(value=value):
                line = f'{{"amount": {value}}}\n'.encode()
                hits = self.hygiene.check_bytes("intel/labels.jsonl", line)
                self.assertEqual(hits, [(1, "invalid JSONL")])


class PrivatePlanningFileTest(unittest.TestCase):
    def setUp(self):
        self.hygiene = load()

    def test_tracked_root_fable_file_fails(self):
        self.assertEqual(self.hygiene.check_bytes("FABLE-PLAN.md", b""),
                         [(1, "private planning file")])

    def test_tracked_docs_critique_file_fails(self):
        self.assertEqual(self.hygiene.check_bytes("docs/CRITIQUE-x.md", b""),
                         [(1, "private planning file")])

    def test_every_private_planning_path_pattern_fails(self):
        for path in ("shieldbot-handoff/x.md", "RUN-x.prompt.md", "AUTO-REVIEW-x.md", "VOTE-x.md",
                     "SAINT-x.md", "x-handoff.md"):
            with self.subTest(path=path):
                self.assertEqual(self.hygiene.check_bytes(path, b""), [(1, "private planning file")])


class ImageDigestTest(unittest.TestCase):
    def setUp(self):
        self.hygiene = load()
        self.digest = "ab" * 32
        self.line = f'image_digest = "sha256:{self.digest}"'

    def test_the_exact_digest_line_passes_in_a_crate_manifest(self):
        for path in ("contracts/gate/Cargo.toml", "contracts/registry/Cargo.toml"):
            with self.subTest(path=path):
                self.assertFalse(self.hygiene.check_text(path, self.line))

    def test_anything_else_with_64_hex_in_a_manifest_is_rejected(self):
        for line in (self.line.upper(), self.line + " # pinned", f"# {self.line}", f'key = "{self.digest}"',
                     f'image_digest = "{self.digest}"', " " + self.line):
            with self.subTest(line=line):
                self.assertTrue(self.hygiene.check_text("contracts/gate/Cargo.toml", line))

    def test_the_digest_line_outside_a_crate_manifest_is_rejected(self):
        for path in ("contracts/gate/other.toml", "docs/BUILD.md", "rust-toolchain.toml"):
            with self.subTest(path=path):
                self.assertTrue(self.hygiene.check_text(path, self.line))


if __name__ == "__main__":
    unittest.main()
