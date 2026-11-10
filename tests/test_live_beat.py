import hashlib
import os
import re
import shlex
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
        self.env.pop("EVIDENCE_CHECK", None)
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

    def test_default_dry_run_matches_explicit_fetch_and_keeps_fetch_step(self):
        default_run = self.run_script()
        explicit_fetch = self.run_script(env={**self.env, "EVIDENCE_CHECK": "fetch"})
        self.assertEqual(default_run.returncode, 0, default_run.stderr)
        self.assertEqual(explicit_fetch.returncode, 0, explicit_fetch.stderr)
        self.assert_nothing_ran()
        digest = hashlib.sha256(EVIDENCE.read_bytes()).hexdigest()
        commit = subprocess.run(
            [GIT, "rev-parse", "HEAD"], capture_output=True, cwd=ROOT, check=True, text=True,
        ).stdout.strip()
        url = f"https://raw.githubusercontent.com/Ridwannurudeen/shieldbot-near/{commit}/docs/demo/synthetic-report.md"
        self.assertIn(f"+ fetch {url} and compare its sha256 with {digest}", default_run.stdout)
        self.assertNotIn("evidence_check: local", default_run.stdout)
        self.assertNotIn("merge-base --is-ancestor", default_run.stdout)
        # Normalize the per-run timestamp and mktemp workdir so the two runs compare line for line.
        normalize = lambda text: [
            re.sub(r"--evidence-dir \S+ --out \S+", "--evidence-dir <workdir> --out <workdir>", line)
            for line in text.splitlines()
            if not line.startswith("dry run: nothing is sent")
            and not line.startswith("# live report beat on ")
        ]
        self.assertEqual(normalize(default_run.stdout), normalize(explicit_fetch.stdout))

    def test_dry_run_with_local_evidence_check_prints_two_local_checks_and_no_fetch(self):
        process = self.run_script(env={**self.env, "EVIDENCE_CHECK": "local"})
        self.assertEqual(process.returncode, 0, process.stderr)
        self.assert_nothing_ran()
        out = process.stdout
        digest = hashlib.sha256(EVIDENCE.read_bytes()).hexdigest()
        commit = subprocess.run(
            [GIT, "rev-parse", "HEAD"], capture_output=True, cwd=ROOT, check=True, text=True,
        ).stdout.strip()
        self.assertIn("evidence_check: local", out)
        self.assertIn(
            "the URL is readable once the repository is public, and scripts/public_flip_check.py --fetch-evidence fetches it then",
            out,
        )
        self.assertNotIn("+ fetch ", out)
        markers = [
            "## a. before the report",
            f"merge-base --is-ancestor {commit} refs/remotes/origin/main",
            f"show {commit}:docs/demo/synthetic-report.md and compare its sha256 with {digest}",
            " subject_summary json-args ",
        ]
        position = 0
        for marker in markers:
            found = out.find(marker, position)
            self.assertNotEqual(found, -1, f"{marker!r} missing after offset {position}:\n{out}")
            position = found + len(marker)

    def test_refuses_bad_evidence_check_value(self):
        for bad in ("skip", "none", "remote", "1"):
            with self.subTest(value=bad):
                env = {**self.env, "EVIDENCE_CHECK": bad}
                self.assert_refused(self.run_script(env=env), "EVIDENCE_CHECK must be fetch or local")

    def test_local_evidence_check_real_run_enforces_pushed_ancestor_and_blob_sha256(self):
        repo = Path(self.tempdir.name) / "repo"
        (repo / "scripts").mkdir(parents=True)
        (repo / "docs" / "demo").mkdir(parents=True)
        shutil.copyfile(ROOT / "scripts" / "lib.sh", repo / "scripts" / "lib.sh")
        shutil.copyfile(ROOT / "scripts" / "live_beat.sh", repo / "scripts" / "live_beat.sh")
        shutil.copyfile(EVIDENCE, repo / "docs" / "demo" / "synthetic-report.md")
        git = lambda *args: subprocess.run(
            [GIT, *args], cwd=repo, check=True, capture_output=True, text=True,
        )
        git("init", "-q")
        git("config", "user.email", "test@example.invalid")
        git("config", "user.name", "test")
        git("config", "core.autocrlf", "false")
        git("add", "-A")
        git("commit", "-q", "-m", "initial")

        live_env = {**self.env, "DRY_RUN": "0", "EVIDENCE_CHECK": "local"}
        run_in_repo = lambda env=live_env: subprocess.run(
            [BASH, "scripts/live_beat.sh"],
            capture_output=True,
            cwd=repo,
            env=env,
            timeout=120,
            check=False,
            text=True,
        )

        # No refs/remotes/origin/main yet: refuses with exit 2 telling the operator to push and fetch.
        missing_remote = run_in_repo()
        self.assertEqual(missing_remote.returncode, 2, missing_remote.stdout + missing_remote.stderr)
        self.assertIn("push the commit and run git fetch", missing_remote.stderr)
        self.assertFalse(self.marker.exists())

        # Commit ahead of refs/remotes/origin/main: refuses with exit 2.
        first_commit = git("rev-parse", "HEAD").stdout.strip()
        git("update-ref", "refs/remotes/origin/main", first_commit)
        (repo / "other.txt").write_text("ahead\n", encoding="utf-8")
        git("add", "other.txt")
        git("commit", "-q", "-m", "ahead of origin")
        ahead = run_in_repo()
        self.assertEqual(ahead.returncode, 2, ahead.stdout + ahead.stderr)
        self.assertIn("push the commit and run git fetch", ahead.stderr)
        self.assertFalse(self.marker.exists())

        # Commit on refs/remotes/origin/main, simulated git show byte mismatch: refuses with exit 2.
        head_commit = git("rev-parse", "HEAD").stdout.strip()
        git("update-ref", "refs/remotes/origin/main", head_commit)
        fake_git_dir = Path(self.tempdir.name) / "fake-git"
        fake_git_dir.mkdir()
        fake_git_called = fake_git_dir / "called"
        fake_git = fake_git_dir / "git"
        fake_git.write_text(
            '#!/bin/sh\n'
            'for arg in "$@"; do\n'
            '  case "$arg" in\n'
            '    *:docs/demo/synthetic-report.md)\n'
            f'      printf "git show\\n" > {shlex.quote(posix(fake_git_called))}\n'
            '      printf "tampered\\n"\n'
            '      exit 0\n'
            '      ;;\n'
            '  esac\n'
            'done\n'
            f'exec "{posix(GIT)}" "$@"\n',
            encoding="utf-8",
        )
        fake_git.chmod(0o755)
        startup = fake_git_dir / "startup.sh"
        original_startup = live_env.get("BASH_ENV")
        startup.write_text(
            (f'source {shlex.quote(original_startup)}\n' if original_startup else "")
            + f'export PATH={shlex.quote(posix(fake_git_dir))}:"$PATH"\n',
            encoding="utf-8",
        )
        tampered_env = {
            **live_env,
            "PATH": os.pathsep.join((str(fake_git_dir), live_env["PATH"])),
            "BASH_ENV": posix(startup),
        }
        mismatch = run_in_repo(env=tampered_env)
        self.assertTrue(fake_git_called.exists(), "the fake git show was not invoked")
        self.assertEqual(mismatch.returncode, 2, mismatch.stdout + mismatch.stderr)
        self.assertIn("has sha256", mismatch.stderr)
        self.assertFalse(self.marker.exists())

        # Commit on refs/remotes/origin/main and matching blob: local checks pass, log records mode and note,
        # and execution proceeds to the first near read (which our stub fails, causing view() to return 1).
        for old_log in (repo / "fixtures" / "runs").glob("live-beat-*.txt"):
            old_log.unlink()
        passed_local = run_in_repo()
        self.assertEqual(passed_local.returncode, 1, passed_local.stdout + passed_local.stderr)
        self.assertTrue(self.marker.exists())
        logs = list((repo / "fixtures" / "runs").glob("live-beat-*.txt"))
        self.assertEqual(len(logs), 1)
        log_text = logs[0].read_text(encoding="utf-8")
        self.assertIn("evidence_check: local", log_text)
        self.assertIn(
            "the URL is readable once the repository is public, and scripts/public_flip_check.py --fetch-evidence fetches it then",
            log_text,
        )
        self.assertIn("evidence page matches", log_text)
        self.assertEqual(set(RUNS.iterdir()), self.runs_before)

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
