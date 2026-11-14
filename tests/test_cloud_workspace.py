import shutil
import subprocess
import unittest
from pathlib import Path


class CloudWorkspaceTest(unittest.TestCase):
    @unittest.skipUnless(shutil.which("node"), "Node is unavailable")
    def test_cloud_boundaries(self):
        for scenario in ("session-unavailable", "session-temporary-failure", "session-reload-failure", "destination-description", "privacy-validation", "stale-export", "cancelled-import", "chunk-import", "body-timeout", "session-race", "version-conflict", "viewer-controls", "private-editor-transition", "global-save", "recovery-identity", "monitor-evidence", "registration-access-code", "member-identifier"):
            with self.subTest(scenario=scenario):
                result = subprocess.run([shutil.which("node"), "tests/cloud_workspace_runtime.cjs", scenario],
                                        cwd=Path(__file__).resolve().parent.parent, capture_output=True,
                                        text=True, timeout=20)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_workspace_preserves_browser_controls(self):
        from intel import render

        html = render.workspace_page()
        for identifier in ("cloud-workspace", "cloud-shell", "browser-workspace", "case-search", "saved-cases", "watch-form", "watched-accounts", "alerts-enabled", "alert-state", "desktop-alerts", "watch-notifications", "clear-notifications", "export-workspace", "import-workspace", "reset-workspace"):
            with self.subTest(identifier=identifier):
                self.assertEqual(html.count(f'id="{identifier}"'), 1)
        self.assertIn("Monitoring runs while a Siyab page stays open.", html)
        self.assertIn("Import merges investigations and accounts without replacing existing notes or enabling monitoring.", html)


if __name__ == "__main__":
    unittest.main()
