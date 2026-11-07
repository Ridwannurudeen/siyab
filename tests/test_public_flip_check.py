"""scripts/public_flip_check.py fails on each planted failure in a temporary git repository, passes a clean
one, and passes on the real tree.
"""

import random
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "public_flip_check.py"
BASE58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"


def run_check(cwd):
    return subprocess.run([sys.executable, str(SCRIPT)], cwd=cwd, capture_output=True, text=True)


def make_repo(tmp, files):
    subprocess.run(["git", "init", "-q", tmp], check=True, capture_output=True)
    for name, text in files.items():
        path = Path(tmp) / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=tmp, check=True, capture_output=True)


class PlantedFailuresTest(unittest.TestCase):
    def test_each_planted_failure_is_reported_and_the_exit_is_1(self):
        name = "Op" "us"
        key = "ed25519:" + "".join(random.choices(BASE58, k=88))
        files = {
            "README.md": "Hosted at https://ShieldBotSecurity.online/ today.\n",
            "docs/CRITIQUE-1.md": "notes\n",
            "notes/" + "FA" "BLE" + "-review.md": "notes\n",
            "prompts/RUN-a.prompt.md": "notes\n",
            "plan/BUILD-PLAN.md": "notes\n",
            "review.md": f"line one\nreviewed by {name}\n",
            "keys.txt": f"{key}\n",
            ".env": "A=1\n",
            "clean.md": "nothing here\n",
        }
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            make_repo(tmp, files)
            result = run_check(tmp)
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        for expected in ("README.md:1: names the hosted hostname shieldbotsecurity.online",
                         "docs/CRITIQUE-1.md: planning marker CRITIQUE in the path",
                         "notes/" + "FA" "BLE" + "-review.md: planning marker " + "FA" "BLE" + " in the path",
                         "prompts/RUN-a.prompt.md: RUN- prompt file",
                         "plan/BUILD-PLAN.md: planning marker BUILD-PLAN in the path",
                         f"review.md:2: names {name}",
                         "keys.txt:1: ed25519 private key form",
                         ".env: tracked .env file"):
            with self.subTest(expected=expected):
                self.assertIn(expected, result.stdout.splitlines())
        self.assertNotIn("clean.md", result.stdout)
        self.assertNotIn(key, result.stdout)
        self.assertEqual(result.stdout.splitlines()[-1], "8 failures")

    def test_a_short_key_form_lower_case_names_and_untracked_files_pass(self):
        files = {"README.md": "a key ed25519:" + "".join(random.choices(BASE58, k=44)) + "\n",
                 "notes.md": ("op" "us and sa" "int, lower case\n"),
                 "dao/hos-vote.sputnik-dao.near.html": "account page\n"}
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            make_repo(tmp, files)
            (Path(tmp) / "untracked.md").write_text("reviewed by " + "Fa" "ble" + "\n", encoding="utf-8")
            result = run_check(tmp)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(result.stdout.strip(), "public flip check: no failures")


class RealTreeTest(unittest.TestCase):
    def test_the_real_tree_passes(self):
        result = run_check(ROOT)
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertEqual(result.stdout.strip(), "public flip check: no failures")


if __name__ == "__main__":
    unittest.main()
