import json
import shutil
import subprocess
import unittest
from pathlib import Path


@unittest.skipUnless(shutil.which("node"), "Node is unavailable")
class NearWorkspaceBrowserTest(unittest.TestCase):
    def run_scenario(self, scenario, *arguments):
        result = subprocess.run(
            [shutil.which("node"), "tests/near_workspace_runtime.cjs", scenario, *arguments],
            cwd=Path(__file__).resolve().parent.parent,
            capture_output=True, text=True, timeout=20,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return result.stdout

    def test_sign_in_reads_no_browser_workspace(self):
        self.run_scenario("sign-in-privacy")

    def test_okx_sign_in_accepts_a_direct_signed_response(self):
        self.run_scenario("okx-sign-in")

    def test_sign_in_and_vault_accept_a_client_two_seconds_behind(self):
        self.run_scenario("clock-skew", "-2000")

    def test_sign_in_and_vault_accept_a_client_two_seconds_ahead(self):
        self.run_scenario("clock-skew", "2000")

    def test_sign_in_and_vault_accept_a_client_ten_minutes_behind(self):
        self.run_scenario("clock-skew", "-600000")

    def test_sign_in_and_vault_accept_a_client_ten_minutes_ahead(self):
        self.run_scenario("clock-skew", "600000")

    def test_server_expired_challenge_refuses_every_client_clock_skew(self):
        self.run_scenario("server-challenge-expiry")

    def test_server_session_expiry_locks_every_client_clock_skew(self):
        self.run_scenario("server-session-expiry")

    def test_okx_unlock_reads_sender_ciphertext_and_sender_reads_its_saved_copy(self):
        self.run_scenario("okx-unlock")

    def test_okx_connects_without_key_permissions_before_sign_in_or_unlock(self):
        self.run_scenario("okx-connect")

    def test_sender_keeps_its_wrapped_response_and_never_connects(self):
        self.run_scenario("sender-shape")

    def test_unknown_providers_are_refused_before_issuing_a_challenge(self):
        self.run_scenario("unknown-provider")

    def test_okx_bad_response_shapes_and_bindings_refuse_sign_in_and_unlock(self):
        self.run_scenario("okx-validation")

    def test_double_submit_is_ignored_during_challenge_connect_wallet_and_verify(self):
        self.run_scenario("double-submit")

    def test_double_unlock_is_ignored_during_both_wallet_approvals(self):
        self.run_scenario("double-unlock")

    def test_explicit_save_sends_only_ciphertext(self):
        self.run_scenario("save-privacy")

    def test_restore_requires_explicit_confirmation(self):
        self.run_scenario("restore-explicit")

    def test_cancelled_save_changes_nothing(self):
        self.run_scenario("save-cancelled")

    def test_bad_server_challenges_never_reach_wallet(self):
        self.run_scenario("challenge-validation")

    def test_auth_challenge_cannot_request_vault_unlock_signature(self):
        self.run_scenario("auth-domain-substitution")

    def test_auth_challenge_message_has_exact_domain_and_lifetime(self):
        self.run_scenario("auth-message-validation")

    def test_unsupported_wallet_and_bad_responses_fail(self):
        self.run_scenario("wallet-validation")

    def test_unlock_signatures_bind_the_exact_request(self):
        self.run_scenario("unlock-binding")

    def test_nondeterministic_wallet_cannot_save(self):
        self.run_scenario("unlock-nondeterministic")

    def test_navigation_cancels_stale_challenge(self):
        self.run_scenario("navigation-challenge")

    def test_navigation_cancels_stale_wallet_reply(self):
        self.run_scenario("navigation-wallet")

    def test_navigation_cancels_stale_sign_in_reply(self):
        self.run_scenario("navigation-verify")

    def test_bfcache_cancels_unlock_wallet_race(self):
        self.run_scenario("navigation-unlock")

    def test_bfcache_cancels_unlock_decrypt_race(self):
        self.run_scenario("navigation-decrypt")

    def test_navigation_cancels_encrypted_save(self):
        self.run_scenario("navigation-save")

    def test_key_changes_and_corruption_preserve_saved_copy(self):
        self.run_scenario("unreadable-record")

    def test_auth_and_revision_errors_lock_writes(self):
        self.run_scenario("http-session-conflict")

    def test_lower_vault_revision_is_refused_after_reload(self):
        self.run_scenario("rollback-after-reload")

    def test_successful_saves_record_the_highest_revision(self):
        self.run_scenario("rollback-after-save")

    def test_revision_history_is_scoped_to_origin_account_and_public_key(self):
        self.run_scenario("rollback-identity-scope")

    def test_unavailable_storage_keeps_encryption_and_page_rollback_checks_working(self):
        self.run_scenario("rollback-storage-unavailable")

    def test_reset_requires_typed_account_and_does_not_upload_browser_data(self):
        self.run_scenario("reset-explicit")

    def test_reset_recovers_a_changed_key_and_preserves_revision_history(self):
        self.run_scenario("reset-changed-key")

    def test_reset_and_replacement_are_readable_on_a_second_device(self):
        self.run_scenario("reset-two-devices")

    def test_reset_is_remembered_on_this_device_after_reload_and_sign_in(self):
        self.run_scenario("reset-same-device")

    def test_reset_marker_is_scoped_to_origin_account_and_public_key(self):
        self.run_scenario("reset-identity-scope")

    def test_later_reset_on_another_device_does_not_reuse_local_marker(self):
        self.run_scenario("reset-later-elsewhere")

    def test_reset_survives_storage_failures_without_false_device_attribution(self):
        self.run_scenario("reset-storage-unavailable")

    def test_tombstones_preserve_rollback_checks_at_the_high_water_mark(self):
        self.run_scenario("reset-tombstone-revisions")

    def test_failed_reset_preserves_session_ciphertext_and_revision_history(self):
        self.run_scenario("reset-refused")

    def test_reset_still_recovers_when_storage_cannot_replace_old_revision(self):
        self.run_scenario("reset-storage-write-denied")

    def test_navigation_cannot_apply_a_stale_reset_reply(self):
        self.run_scenario("navigation-reset")

    def test_eth_implicit_account_never_reaches_wallet(self):
        self.run_scenario("eth-implicit")

    def test_panel_discloses_reset_loss_ip_and_revision_timing(self):
        panel = (Path(__file__).resolve().parent.parent / "intel/ui/near-workspace-panel.html").read_text(encoding="utf-8")
        for text in ("near-reset-form", "near-reset-confirmation", "near-reset", "permanently", "lost",
                     "IP address", "revision", "timing", "0x", "five minutes", "Sender", "OKX Wallet"):
            with self.subTest(text=text):
                self.assertIn(text, panel)

    def test_initial_vault_timeout_blocks_writes(self):
        self.run_scenario("timeout-after-login")

    def test_initial_vault_errors_block_writes(self):
        self.run_scenario("error-after-login")

    def test_logout_and_bfcache_lock_key_material(self):
        self.run_scenario("logout-and-bfcache")

    def test_emit_real_browser_ciphertext_without_secrets(self):
        envelope = json.loads(self.run_scenario("emit-envelope"))
        self.assertEqual(envelope["vault_id"], "workspace:1")
        self.assertEqual(envelope["algorithm"], "AES-256-GCM")
        self.assertNotIn("signature", envelope)
        self.assertNotIn("Confidential notes", json.dumps(envelope))


if __name__ == "__main__":
    unittest.main()
