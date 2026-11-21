import shutil
import subprocess
import unittest
from pathlib import Path


@unittest.skipUnless(shutil.which("node"), "Node is unavailable")
class WorkspaceVaultTest(unittest.TestCase):
    def run_scenario(self, scenario):
        result = subprocess.run(
            [shutil.which("node"), "tests/workspace_vault_runtime.cjs", scenario],
            cwd=Path(__file__).resolve().parent.parent,
            capture_output=True, text=True, timeout=20,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_nep413_spec_serialization(self):
        self.run_scenario("spec-vector")

    def test_unlock_request_stable_across_devices(self):
        self.run_scenario("deterministic-request")

    def test_ciphertext_round_trip_across_devices(self):
        self.run_scenario("round-trip")

    def test_ciphertext_and_tag_tampering_fail(self):
        self.run_scenario("tamper")

    def test_origin_account_key_and_vault_binding(self):
        self.run_scenario("context")

    def test_ciphertext_envelope_validation(self):
        self.run_scenario("strict-envelope")

    def test_nep413_payload_changes_fail(self):
        self.run_scenario("payload-binding")

    def test_wallet_identity_and_signature_validation(self):
        self.run_scenario("wallet-identity")

    def test_nondeterministic_wallet_fails_before_derivation(self):
        self.run_scenario("nondeterministic")

    def test_plaintext_byte_bounds(self):
        self.run_scenario("limits")

    def test_lock_cancels_reads_and_writes(self):
        self.run_scenario("lock")

    def test_missing_crypto_and_invalid_context_fail(self):
        self.run_scenario("crypto-unavailable")

    def test_independent_crypto_interoperability(self):
        self.run_scenario("interoperability")

    def test_server_boundary_has_only_ciphertext(self):
        self.run_scenario("workspace-boundary")


if __name__ == "__main__":
    unittest.main()
