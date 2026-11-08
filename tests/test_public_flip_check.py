"""scripts/public_flip_check.py fails on each planted failure in a temporary git repository, passes a clean
one, and passes on the real tree.
"""

import contextlib
import hashlib
import io
import os
import random
import subprocess
import sys
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts import public_flip_check

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


class _StubResponse:
    def __init__(self, body):
        self._body = body

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def read(self):
        return self._body


def run_main_in(cwd, argv, urlopen_side_effect):
    stdout = io.StringIO()
    previous = os.getcwd()
    try:
        os.chdir(cwd)
        with mock.patch.object(
            public_flip_check.urllib.request, "urlopen", side_effect=urlopen_side_effect,
        ) as mocked:
            with contextlib.redirect_stdout(stdout):
                code = public_flip_check.main(argv)
    finally:
        os.chdir(previous)
    return code, stdout.getvalue(), mocked


def sample_beat_log(url, digest):
    return (
        "# live report beat on testnet, 20261108T100000Z\n"
        "commit: 69e50253\n"
        f"evidence_url: {url}\n"
        f"evidence_sha256 (sha256 of docs/demo/synthetic-report.md): {digest}\n"
        "evidence_check: local\n"
    )


class FetchEvidenceTest(unittest.TestCase):
    URL = "https://raw.githubusercontent.com/Ridwannurudeen/shieldbot-near/69e50253/docs/demo/synthetic-report.md"
    LOG_PATH = "fixtures/runs/live-beat-20261108T100000Z.txt"

    def test_fetch_evidence_passes_on_matching_stubbed_fetch(self):
        body = b"Synthetic report evidence body.\n"
        digest = hashlib.sha256(body).hexdigest()
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            make_repo(tmp, {self.LOG_PATH: sample_beat_log(self.URL, digest)})
            code, out, mocked = run_main_in(
                tmp, ["--fetch-evidence"], lambda url, timeout=None: _StubResponse(body),
            )
        self.assertEqual(code, 0, out)
        self.assertEqual(out.strip(), "public flip check: no failures")
        mocked.assert_called_once_with(self.URL, timeout=30)

    def test_fetch_evidence_fails_naming_the_file_on_hash_mismatch(self):
        expected = hashlib.sha256(b"expected bytes\n").hexdigest()
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            make_repo(tmp, {self.LOG_PATH: sample_beat_log(self.URL, expected)})
            code, out, mocked = run_main_in(
                tmp, ["--fetch-evidence"], lambda url, timeout=None: _StubResponse(b"actual bytes\n"),
            )
        self.assertEqual(code, 1, out)
        mocked.assert_called_once_with(self.URL, timeout=30)
        lines = out.splitlines()
        self.assertTrue(any(line.startswith(f"{self.LOG_PATH}:") and "mismatch" in line for line in lines), out)
        self.assertEqual(lines[-1], "1 failure")

    def test_fetch_evidence_fails_naming_the_file_on_fetch_error(self):
        digest = hashlib.sha256(b"expected bytes\n").hexdigest()
        errors = (
            urllib.error.HTTPError(self.URL, 404, "Not Found", hdrs=None, fp=None),
            urllib.error.URLError("connection refused"),
        )
        for err in errors:
            with self.subTest(error=type(err).__name__):
                with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
                    make_repo(tmp, {self.LOG_PATH: sample_beat_log(self.URL, digest)})
                    code, out, mocked = run_main_in(tmp, ["--fetch-evidence"], err)
                self.assertEqual(code, 1, out)
                mocked.assert_called_once_with(self.URL, timeout=30)
                lines = out.splitlines()
                self.assertTrue(
                    any(line.startswith(f"{self.LOG_PATH}:") and "evidence fetch failed" in line for line in lines),
                    out,
                )
                self.assertEqual(lines[-1], "1 failure")

    def test_fetch_evidence_passes_when_no_live_beat_logs_exist(self):
        def forbid_fetch(*args, **kwargs):
            raise AssertionError("urlopen must not be called when no live-beat logs exist")

        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            make_repo(tmp, {"clean.md": "nothing here\n"})
            code, out, mocked = run_main_in(tmp, ["--fetch-evidence"], forbid_fetch)
        self.assertEqual(code, 0, out)
        mocked.assert_not_called()
        self.assertIn("nothing to check", out)
        self.assertEqual(out.splitlines()[-1], "public flip check: no failures")

    def test_default_run_makes_no_fetch_when_logs_exist(self):
        digest = hashlib.sha256(b"expected bytes\n").hexdigest()

        def forbid_fetch(*args, **kwargs):
            raise AssertionError("urlopen must not be called without --fetch-evidence")

        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            make_repo(tmp, {self.LOG_PATH: sample_beat_log(self.URL, digest)})
            code, out, mocked = run_main_in(tmp, [], forbid_fetch)
        self.assertEqual(code, 0, out)
        mocked.assert_not_called()
        self.assertEqual(out.strip(), "public flip check: no failures")

    def test_fetch_evidence_fails_when_log_lacks_evidence_headers(self):
        def forbid_fetch(*args, **kwargs):
            raise AssertionError("urlopen must not be called on incomplete headers")

        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            make_repo(tmp, {self.LOG_PATH: "# live report beat without headers\n"})
            code, out, mocked = run_main_in(tmp, ["--fetch-evidence"], forbid_fetch)
        self.assertEqual(code, 1, out)
        mocked.assert_not_called()
        self.assertTrue(out.splitlines()[0].startswith(f"{self.LOG_PATH}:"), out)
        self.assertEqual(out.splitlines()[-1], "1 failure")


if __name__ == "__main__":
    unittest.main()
