import json
import os
import shlex
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
BASH = shutil.which("bash")


@unittest.skipUnless(os.name == "posix" and BASH, "run script tests in Linux or WSL")
class DeployAttestTest(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory(prefix="attest-deploy-test-")
        self.addCleanup(self.tempdir.cleanup)
        self.directory = Path(self.tempdir.name)
        self.config_path = self.directory / "public-config.json"
        self.config = {
            "verifier_account_id": "tee-verifier-2026-11-02.testnet",
            "signer_account_id": "signer.testnet",
            "ed25519_domain_id": 7,
            "max_lease_ms": 86400000,
            "vote_recipients": ["vote.testnet"],
        }
        bin_dir = self.directory / "bin"
        bin_dir.mkdir()
        self.marker = self.directory / "near-called"
        near = bin_dir / "near"
        near.write_text('#!/bin/sh\nprintf called > "$NEAR_TEST_MARKER"\nexit 77\n')
        near.chmod(0o755)
        self.env = {
            **os.environ,
            "PATH": os.pathsep.join((str(bin_dir), "/usr/bin", "/bin")),
            "NETWORK": "testnet",
            "DRY_RUN": "0",
            "NEAR_KEYS_DIR": "",
            "NEAR_TEST_MARKER": str(self.marker),
        }

    def run_script(self, *flags, registry="registry.testnet", config=None, env=None):
        self.config_path.write_text(json.dumps(self.config if config is None else config))
        result = subprocess.run(
            [BASH, str(ROOT / "scripts/deploy_attest.sh"), *flags,
             registry, str(self.config_path)],
            cwd=self.directory,
            env={**self.env, **(env or {})},
            text=True,
            capture_output=True,
            timeout=30,
            check=False,
        )
        self.assertFalse(self.marker.exists(), "the deployment script invoked near")
        return result

    def assert_refusal(self, result, code, message):
        self.assertEqual(result.returncode, code, result.stderr)
        self.assertIn(message, result.stderr)
        self.assertNotIn("+ near", result.stdout)

    def assert_commands(self, result):
        self.assertEqual(result.returncode, 0, result.stderr)
        commands = [shlex.split(line[2:]) for line in result.stdout.splitlines()
                    if line.startswith("+ ")]
        self.assertEqual(len(commands), 2)
        deploy, view = commands
        self.assertEqual(deploy[:6], ["near", "contract", "deploy", "registry.testnet",
                                     "use-file", str(ROOT / "res/attest.wasm")])
        self.assertEqual(deploy[6:9], ["with-init-call", "new", "json-args"])
        self.assertEqual(json.loads(deploy[9]), self.config)
        self.assertEqual(deploy[10:], [
            "prepaid-gas", "100.0 Tgas", "attached-deposit", "0 NEAR",
            "network-config", "testnet", "sign-with-access-key-file",
            "<external-key-directory>/registry.testnet.json", "send",
        ])
        self.assertEqual(view, [
            "near", "contract", "call-function", "as-read-only", "registry.testnet",
            "get_config", "json-args", "{}", "network-config", "testnet", "now",
        ])
        self.assertIn("access keys unchanged", result.stdout)

    def test_explicit_dry_run_prints_exact_commands(self):
        self.assert_commands(self.run_script("--dry-run"))

    def test_default_is_dry_run_even_when_environment_requests_execution(self):
        self.assert_commands(self.run_script())

    def test_mainnet_is_refused(self):
        self.assert_refusal(self.run_script("--dry-run", env={"NETWORK": "mainnet"}),
                            2, "NETWORK must be testnet")

    def test_conflicting_flags_are_refused(self):
        self.assert_refusal(self.run_script("--dry-run", "--execute"),
                            2, "choose either --dry-run or --execute")

    def test_unknown_option_is_refused(self):
        self.assert_refusal(self.run_script("--unknown"), 2, "unknown option")

    def test_execute_without_external_key_directory_is_refused_before_near(self):
        self.assert_refusal(self.run_script("--execute"),
                            2, "missing required environment variable: NEAR_KEYS_DIR")

    def test_registry_outside_testnet_is_refused(self):
        self.assert_refusal(self.run_script("--dry-run", registry="registry.near"),
                            2, "registry account must end in .testnet")

    def test_extra_config_field_is_refused(self):
        self.assert_refusal(self.run_script("--dry-run", config={
            **self.config, "owner_id": "owner.testnet"}), 1, "exactly the five")

    def test_missing_config_field_is_refused(self):
        del self.config["signer_account_id"]
        self.assert_refusal(self.run_script("--dry-run"), 1, "exactly the five")

    def test_zero_lease_is_refused(self):
        self.config["max_lease_ms"] = 0
        self.assert_refusal(self.run_script("--dry-run"), 1, "must be nonzero")

    def test_boolean_domain_is_refused(self):
        self.config["ed25519_domain_id"] = True
        self.assert_refusal(self.run_script("--dry-run"), 1, "invalid u64")

    def test_domain_overflow_is_refused(self):
        self.config["ed25519_domain_id"] = 1 << 64
        self.assert_refusal(self.run_script("--dry-run"), 1, "invalid u64")

    def test_unsorted_recipients_are_refused(self):
        self.config["vote_recipients"] = ["z.testnet", "a.testnet"]
        self.assert_refusal(self.run_script("--dry-run"), 1, "sorted unique")

    def test_duplicate_recipients_are_refused(self):
        self.config["vote_recipients"] = ["a.testnet", "a.testnet"]
        self.assert_refusal(self.run_script("--dry-run"), 1, "sorted unique")

    def test_noncanonical_signer_account_is_refused(self):
        self.config["signer_account_id"] = "invalid..testnet"
        self.assert_refusal(self.run_script("--dry-run"), 1, "invalid signer_account_id")

    def test_empty_recipients_are_refused(self):
        self.config["vote_recipients"] = []
        self.assert_refusal(self.run_script("--dry-run"), 1, "1 through 8")


if __name__ == "__main__":
    unittest.main()
