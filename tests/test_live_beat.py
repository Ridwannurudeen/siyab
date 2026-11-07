import hashlib
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BASH = shutil.which("bash")
GIT = shutil.which("git")
EVIDENCE = ROOT / "docs" / "demo" / "synthetic-report.md"
RUNS = ROOT / "fixtures" / "runs"


def posix(path):
    drive, tail = os.path.splitdrive(os.path.abspath(path))
    if drive:
        return f"/{drive[0].lower()}{tail.replace(os.sep, '/')}"
    return str(path)


@unittest.skipUnless(BASH and GIT, "bash and git are required for the live beat script tests")
class LiveBeatScriptTest(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        bin_dir = Path(self.tempdir.name) / "bin"
        bin_dir.mkdir()
        python3 = bin_dir / "python3"
        python3.write_text(f'#!/bin/sh\nexec "{posix(sys.executable)}" "$@"\n', encoding="utf-8")
        python3.chmod(0o755)
        self.marker = Path(self.tempdir.name) / "near-called"
        near = bin_dir / "near"
        near.write_text(f'#!/bin/sh\necho "$@" >> "{posix(self.marker)}"\nexit 97\n', encoding="utf-8")
        near.chmod(0o755)
        self.env = {
            **os.environ,
            "PATH": os.pathsep.join((str(bin_dir), str(Path(BASH).parent), str(Path(GIT).parent))),
            "DRY_RUN": "1",
            "NETWORK": "testnet",
            "OWNER_ACCOUNT_ID": "owner.testnet",
            "REPORTER_ACCOUNT_ID": "reporter.testnet",
            "DISPUTER_ACCOUNT_ID": "disputer.testnet",
            "REPORTS_ACCOUNT_ID": "reports.gate.testnet",
            "GATE_ACCOUNT_ID": "gate.testnet",
            "COUNTERPARTY_ACCOUNT_ID": "subject.testnet",
            "NEAR_KEYS_DIR": str(Path(self.tempdir.name) / "keys"),
        }
        self.runs_before = set(RUNS.iterdir())

    def run_script(self, *args, env=None):
        return subprocess.run(
            [BASH, "scripts/live_beat.sh", *args],
            capture_output=True,
            cwd=ROOT,
            env=self.env if env is None else env,
            timeout=120,
            check=False,
            text=True,
        )

    def assert_nothing_ran(self):
        self.assertFalse(self.marker.exists(), "near was invoked")
        self.assertEqual(set(RUNS.iterdir()), self.runs_before, "a run log was written")

    def assert_refused(self, process, text):
        self.assertEqual(process.returncode, 2, process.stdout + process.stderr)
        self.assertIn(text, process.stderr)
        self.assertNotIn("+ ", process.stdout)
        self.assert_nothing_ran()

    def test_dry_run_prints_every_step_in_order(self):
        process = self.run_script()
        self.assertEqual(process.returncode, 0, process.stderr)
        self.assert_nothing_ran()
        out = process.stdout
        markers = [
            "## a. before the report",
            " subject_summary json-args ",
            "python3 -m gate verdict",
            " totals ",
            "## b. file the report",
            "scripts/report.sh reports.gate.testnet subject.testnet theft near_miss",
            " submit_report ",
            "sign-as reporter.testnet",
            " totals ",
            " get_report ",
            "## c. pending",
            '"pending":1',
            "1 pending report(s) await moderation; no effect",
            "## d. accept",
            "scripts/moderate.sh reports.gate.testnet 0 accept",
            " moderate ",
            "sign-as owner.testnet",
            "## e. accepted",
            '"accepted":1,"contested":0,"deny":true,"latest_accepted_id":0,"latest_deny_id":0',
            "deny report 0 is accepted; 0 under dispute; pending 0",
            "expect: verdict: DENY",
            "## f. dispute",
            "scripts/dispute.sh reports.gate.testnet 0",
            " dispute ",
            "sign-as disputer.testnet",
            '"accepted":1,"contested":1,"deny":true',
            "deny report 0 is accepted; 1 under dispute; pending 0",
            "expect: verdict: DENY",
            "## g. overturn",
            "scripts/dispute.sh reports.gate.testnet --resolve 0 overturn",
            " resolve_dispute ",
            '"accepted":0,"contested":0,"deny":false',
            "no community report; pending 0",
            "dry run complete: nothing was sent",
        ]
        position = 0
        for marker in markers:
            found = out.find(marker, position)
            self.assertNotEqual(found, -1, f"{marker!r} missing after offset {position}:\n{out}")
            position = found + len(marker)
        verdicts = [line for line in out.splitlines() if "python3 -m gate verdict" in line]
        self.assertEqual(len(verdicts), 5)
        for line in verdicts:
            self.assertIn("GATE_FIXTURES_MODE=off", line)
            self.assertIn("REPORTS_ACCOUNT_ID=reports.gate.testnet", line)
            self.assertIn("--receiver subject.testnet", line)
            self.assertIn("--token token.gate.testnet", line)
            self.assertNotIn("--live", line)
        self.assertNotIn("gate pay", out)
        self.assertNotIn("request_transfer", out)

    def test_dry_run_flag_works_without_environment_switch(self):
        env = {key: value for key, value in self.env.items() if key != "DRY_RUN"}
        process = self.run_script("--dry-run", env=env)
        self.assertEqual(process.returncode, 0, process.stderr)
        self.assertIn(" resolve_dispute ", process.stdout)
        self.assertIn("dry run complete: nothing was sent", process.stdout)
        self.assert_nothing_ran()

    def test_evidence_hash_and_url_come_from_the_file_and_head(self):
        process = self.run_script()
        self.assertEqual(process.returncode, 0, process.stderr)
        digest = hashlib.sha256(EVIDENCE.read_bytes()).hexdigest()
        commit = subprocess.run(
            [GIT, "rev-parse", "HEAD"], capture_output=True, cwd=ROOT, check=True, text=True,
        ).stdout.strip()
        url = f"https://raw.githubusercontent.com/Ridwannurudeen/shieldbot-near/{commit}/docs/demo/synthetic-report.md"
        self.assertIn(f"evidence_sha256 (sha256 of docs/demo/synthetic-report.md): {digest}", process.stdout)
        self.assertIn(f"theft near_miss {digest} {url}", process.stdout)
        self.assertIn(f"scripts/dispute.sh reports.gate.testnet 0 {digest} {url}", process.stdout)
        self.assertIn(f'\\"evidence_sha256\\":\\"{digest}\\"', process.stdout)

    def test_refuses_reporter_equal_to_owner(self):
        env = {**self.env, "REPORTER_ACCOUNT_ID": "owner.testnet"}
        self.assert_refused(self.run_script(env=env), "REPORTER_ACCOUNT_ID must differ from OWNER_ACCOUNT_ID")

    def test_refuses_disputer_equal_to_reporter_or_owner(self):
        for account in ("reporter.testnet", "owner.testnet"):
            with self.subTest(account=account):
                env = {**self.env, "DISPUTER_ACCOUNT_ID": account}
                self.assert_refused(self.run_script(env=env), "DISPUTER_ACCOUNT_ID must differ")

    def test_refuses_subject_equal_to_a_signer(self):
        for account in ("owner.testnet", "reporter.testnet", "disputer.testnet"):
            with self.subTest(account=account):
                env = {**self.env, "COUNTERPARTY_ACCOUNT_ID": account}
                self.assert_refused(self.run_script(env=env), "the subject COUNTERPARTY_ACCOUNT_ID must differ")

    def test_refuses_each_missing_variable(self):
        for name in (
            "OWNER_ACCOUNT_ID",
            "REPORTER_ACCOUNT_ID",
            "DISPUTER_ACCOUNT_ID",
            "REPORTS_ACCOUNT_ID",
            "GATE_ACCOUNT_ID",
            "COUNTERPARTY_ACCOUNT_ID",
            "NEAR_KEYS_DIR",
        ):
            with self.subTest(name=name):
                env = {key: value for key, value in self.env.items() if key != name}
                self.assert_refused(self.run_script(env=env), f"missing required environment variable: {name}")

    def test_refuses_mainnet_and_a_mainnet_gate(self):
        self.assert_refused(self.run_script(env={**self.env, "NETWORK": "mainnet"}), "NETWORK must be testnet")
        self.assert_refused(
            self.run_script(env={**self.env, "GATE_ACCOUNT_ID": "gate.near"}),
            "GATE_ACCOUNT_ID must be a .testnet account",
        )

    def test_bash_syntax(self):
        process = subprocess.run(
            [BASH, "-n", "scripts/live_beat.sh"],
            capture_output=True,
            cwd=ROOT,
            timeout=30,
            check=False,
            text=True,
        )
        self.assertEqual(process.returncode, 0, process.stderr)


if __name__ == "__main__":
    unittest.main()
