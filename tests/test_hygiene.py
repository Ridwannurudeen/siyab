import importlib.util
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
