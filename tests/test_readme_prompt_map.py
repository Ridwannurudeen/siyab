"""The README table that maps the points of the track's example prompt to what exists, where and its limit.

The table has eight rows in the prompt's order, each quoting its point, and its Where column holds only
file:function references, each a function defined in the named file (for a .rs file, a fn of that name).
"""

import unittest
from pathlib import Path

from tests.test_attack_evidence import declared_functions, table_rows
from tests.test_readme_claims import code_spans

ROOT = Path(__file__).resolve().parent.parent
README = ROOT / "README.md"
HEADER = "| Prompt point |"
POINTS = (
    "allow, warn or block, plus the reasons",
    "account age and activity history",
    "whether the receiver is a contract, and whether its code changed recently",
    "prior interactions between this agent and the receiver",
    "a shared blocklist that other agents can report to, with evidence attached",
    "An agent-side wrapper that refuses to sign when the answer is block and asks a human when it's warn",
    ("a function-call access key scoped to one contract, never a full-access key. Explain the limits of that key "
     "in the README."),
    "a prompt-injected agent tries to pay a known-bad address and gets stopped. Show the log.",
)


class ReadmePromptMapTest(unittest.TestCase):
    def setUp(self):
        self.rows = table_rows(README.read_text(encoding="utf-8"), HEADER)

    def test_eight_rows_quote_the_prompt_points_in_order(self):
        self.assertEqual([row[0] for row in self.rows], [f'"{point}"' for point in POINTS])
        for row in self.rows:
            self.assertEqual(len(row), 4, row[0])

    def test_every_where_reference_is_a_function_defined_in_its_file(self):
        self.assertEqual(len(self.rows), 8)
        for row in self.rows:
            references = code_spans(row[2])
            self.assertTrue(references, row[0])
            self.assertEqual(row[2], ", ".join(f"`{reference}`" for reference in references), row[0])
            for reference in references:
                with self.subTest(reference=reference):
                    self.assertIn(":", reference)
                    path_text, function = reference.rsplit(":", 1)
                    path = ROOT / path_text
                    self.assertTrue(path.is_file(), path_text)
                    self.assertIn(function, declared_functions(path))


if __name__ == "__main__":
    unittest.main()
