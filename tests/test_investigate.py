import shutil
import subprocess
import unittest
from pathlib import Path

from intel import render


class LiveInvestigationTest(unittest.TestCase):
    @unittest.skipUnless(shutil.which("node"), "Node is unavailable")
    def test_live_scope_source_boundaries_and_exact_values(self):
        result = subprocess.run(
            [shutil.which("node"), "tests/investigate_runtime.cjs"],
            cwd=Path(__file__).resolve().parent.parent,
            capture_output=True,
            text=True,
            timeout=15,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_page_exposes_real_read_surfaces_and_explicit_graph_action(self):
        page = render.investigate_page()
        for name in (
            "investigate-form",
            "investigate-account",
            "investigate-profile",
            "investigate-holdings",
            "investigate-transfers",
            "investigate-transactions",
            "investigate-graph",
            "investigate-identity",
            "investigate-read-identity",
            "investigate-history",
            "investigate-history-load",
            "investigate-backfill",
        ):
            self.assertIn(f'id="{name}"', page)
        self.assertIn("Indexed token holdings", page)
        self.assertIn("Load neighborhood", page)
        self.assertIn("No payment is signed", page)
        self.assertNotIn("data-graph-src=", page)
        self.assertIn("<noscript>", page)
