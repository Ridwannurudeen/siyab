import json
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BASH = shutil.which("bash")
CURRENT_VERIFIER = "ed25519:2" + "1" * 43
NEW_VERIFIER = "ed25519:3" + "1" * 43
HASH = "a" * 64


@unittest.skipUnless(BASH, "bash is required for operator script tests")
class OperatorScriptTest(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        bin_dir = Path(self.tempdir.name) / "bin"
        bin_dir.mkdir()
        python3 = bin_dir / "python3"
        executable = os.path.abspath(sys.executable)
        drive, tail = os.path.splitdrive(executable)
        if drive:
            executable = f"/{drive[0].lower()}{tail.replace(os.sep, '/')}"
        python3.write_text(f'#!/bin/sh\nexec "{executable}" "$@"\n', encoding="utf-8")
        python3.chmod(0o755)
        self.env = {
            **os.environ,
            "PATH": os.pathsep.join((str(bin_dir), str(Path(BASH).parent))),
            "DRY_RUN": "1",
            "NETWORK": "testnet",
            "OWNER_ACCOUNT_ID": "owner.testnet",
            "GATE_ACCOUNT_ID": "gate.testnet",
            "NEAR_KEYS_DIR": str(Path(self.tempdir.name) / "keys"),
            "VERIFIER_PUB": CURRENT_VERIFIER,
            "MAX_TTL_MS": "900000",
        }
        self.assertIsNone(shutil.which("near", path=self.env["PATH"]))

    def run_script(self, script, *args, env=None):
        return subprocess.run(
            [BASH, f"scripts/{script}", *args],
            capture_output=True,
            cwd=ROOT,
            env=self.env if env is None else env,
            timeout=30,
            check=False,
            text=True,
        )

    def near_command(self, process, method):
        lines = [
            line for line in process.stdout.splitlines()
            if line.startswith("+ near ") and f" {method} " in line
        ]
        self.assertEqual(len(lines), 1, process.stdout)
        return shlex.split(lines[0][2:])

    def assert_transaction(self, process, contract, method, args, deposit):
        self.assertEqual(process.returncode, 0, process.stderr)
        command = self.near_command(process, method)
        self.assertEqual(
            command,
            [
                "near", "contract", "call-function", "as-transaction", contract,
                method, "json-args", json.dumps(args, separators=(",", ":")),
                "prepaid-gas", "100 Tgas", "attached-deposit", deposit,
                "sign-as", "owner.testnet", "network-config", "testnet",
                "sign-with-access-key-file",
                f'{self.env["NEAR_KEYS_DIR"]}/owner.testnet.json', "send",
            ],
        )
        self.assertIn("Summary\n  network: testnet", process.stdout)

    def assert_no_near(self, process):
        self.assertEqual(process.returncode, 2, process.stderr)
        self.assertNotIn("+ near ", process.stdout)

    def test_report_reads_deposit_and_prints_exact_call(self):
        args = {
            "subject": "subject.testnet",
            "kind": "scam",
            "outcome": "loss",
            "evidence_sha256": HASH,
            "evidence_url": "https://evidence.example/report",
        }
        process = self.run_script(
            "report.sh", "reports.testnet", "subject.testnet", "scam", "loss",
            HASH, "https://evidence.example/report",
        )
        self.assert_transaction(
            process, "reports.testnet", "submit_report", args,
            "23004000000000000000000 yoctoNEAR",
        )

    def test_moderate_prints_exact_call(self):
        process = self.run_script("moderate.sh", "reports.testnet", "7", "accept", HASH)
        self.assert_transaction(
            process, "reports.testnet", "moderate",
            {"id": 7, "accept": True, "reason_sha256": HASH}, "1 yoctoNEAR",
        )

    def test_dispute_and_resolution_print_exact_calls(self):
        process = self.run_script(
            "dispute.sh", "reports.testnet", "7", HASH, "https://evidence.example/appeal",
        )
        self.assert_transaction(
            process, "reports.testnet", "dispute",
            {
                "report_id": 7,
                "evidence_sha256": HASH,
                "evidence_url": "https://evidence.example/appeal",
            },
            "23004000000000000000000 yoctoNEAR",
        )

        process = self.run_script(
            "dispute.sh", "reports.testnet", "--resolve", "9", "overturn", HASH,
        )
        self.assert_transaction(
            process, "reports.testnet", "resolve_dispute",
            {"dispute_id": 9, "overturn": True, "reason_sha256": HASH}, "1 yoctoNEAR",
        )

    def test_verifier_rotation_preserves_old_key_for_one_configured_ttl(self):
        started_ms = time.time_ns() // 1_000_000
        process = self.run_script("rotate_verifier.sh", NEW_VERIFIER)
        finished_ms = time.time_ns() // 1_000_000
        self.assertEqual(process.returncode, 0, process.stderr)
        command = self.near_command(process, "set_verifier_keys")
        self.assertEqual(
            command[:7],
            ["near", "contract", "call-function", "as-transaction", "gate.testnet",
             "set_verifier_keys", "json-args"],
        )
        args = json.loads(command[7])
        self.assertEqual(args["current"], NEW_VERIFIER)
        self.assertEqual(args["previous"], CURRENT_VERIFIER)
        self.assertGreaterEqual(args["previous_valid_until_ms"], started_ms + 900000)
        self.assertLessEqual(args["previous_valid_until_ms"], finished_ms + 900000)
        self.assertEqual(
            command[8:],
            [
                "prepaid-gas", "100 Tgas", "attached-deposit", "1 yoctoNEAR",
                "sign-as", "owner.testnet", "network-config", "testnet",
                "sign-with-access-key-file",
                f'{self.env["NEAR_KEYS_DIR"]}/owner.testnet.json', "send",
            ],
        )
        self.assertIn("Summary\n  network: testnet", process.stdout)

    def test_recorder_rotation_prints_exact_call(self):
        process = self.run_script("rotate_recorder.sh", "recorder.testnet")
        self.assert_transaction(
            process, "registry.gate.testnet", "set_recorder",
            {"recorder_id": "recorder.testnet"}, "1 yoctoNEAR",
        )

    def test_dry_run_flag_works_without_environment_switch(self):
        env = {**self.env, "DRY_RUN": "0"}
        process = self.run_script("rotate_recorder.sh", "recorder.testnet", "--dry-run", env=env)
        self.assert_transaction(
            process, "registry.gate.testnet", "set_recorder",
            {"recorder_id": "recorder.testnet"}, "1 yoctoNEAR",
        )

    def test_bad_kind_hash_and_url_exit_before_near(self):
        self.assert_no_near(self.run_script(
            "report.sh", "reports.testnet", "subject.testnet", "not_a_kind", "loss",
            HASH, "https://evidence.example/report",
        ))
        self.assert_no_near(self.run_script(
            "report.sh", "reports.testnet", "subject.testnet", "scam", "not_an_outcome",
            HASH, "https://evidence.example/report",
        ))
        self.assert_no_near(self.run_script(
            "report.sh", "reports.testnet", "subject.testnet", "scam", "loss",
            "bad-hash", "https://evidence.example/report",
        ))
        self.assert_no_near(self.run_script(
            "moderate.sh", "reports.testnet", "7", "accept", "bad-hash",
        ))
        self.assert_no_near(self.run_script(
            "dispute.sh", "reports.testnet", "7", HASH, "http://evidence.example/appeal",
        ))

    def test_bash_syntax_for_each_operator_script(self):
        for script in (
            "report.sh", "moderate.sh", "dispute.sh", "rotate_verifier.sh", "rotate_recorder.sh",
        ):
            with self.subTest(script=script):
                process = subprocess.run(
                    [BASH, "-n", f"scripts/{script}"],
                    capture_output=True,
                    cwd=ROOT,
                    timeout=30,
                    check=False,
                    text=True,
                )
                self.assertEqual(process.returncode, 0, process.stderr)
