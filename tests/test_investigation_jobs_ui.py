import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from intel import history, investigation_jobs
from intel.workspace import SQLiteWorkspaceStore
from tests.test_history import page, transfer


class InvestigationJobsUITest(unittest.TestCase):
    @unittest.skipUnless(shutil.which("node"), "Node is required for browser boundary tests")
    def test_real_job_responses_auth_races_limits_and_retained_evidence(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = SQLiteWorkspaceStore(root / "workspace.sqlite3")
            registered = store.register("alice", "test-password-123")
            actor = registered["user"]["id"]
            team = store.list_teams(actor)[0]
            team2 = store.create_team(actor, "Other team")
            archive = history.AccountHistory(root / "history.sqlite3", clock=lambda: 2000)
            jobs = investigation_jobs.InvestigationJobs(root / "investigation-jobs.sqlite3", archive, clock=lambda: 2000)
            queued = jobs.create(team["id"], actor, "alice.near")
            completed = jobs.create(team["id"], actor, "alice.near")
            other = jobs.create(team2["id"], actor, "bob.near")

            class Reader:
                def history_page(self, account, kind, **kwargs):
                    return page([transfer(1, outgoing=False, other=None)], account=account)

            jobs.cancel(team["id"], queued["id"])
            jobs.cancel(team2["id"], other["id"])
            jobs.tick(Reader())
            session = {**store.authenticate(registered["session_token"]), "authenticated": True, "teams": [team, team2]}
            fixtures = {"session": session, "team": team, "team2": team2, "queued": queued, "other": other,
                        "completed": jobs.get(team["id"], completed["id"]), "result": jobs.result(team["id"], completed["id"])}
            for scenario in ("auth-team-race", "viewer-and-visibility", "timeout-and-permission", "results-and-stale-auth", "result-selection-race", "unknown-and-bounds", "cloud-context-hook"):
                with self.subTest(scenario=scenario):
                    result = subprocess.run([shutil.which("node"), "tests/investigation_jobs_runtime.cjs", scenario], input=json.dumps(fixtures), text=True, capture_output=True, timeout=20, env={**os.environ, "TZ": "America/New_York"})
                    self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_semantics_reflow_and_explicit_collection_bounds(self):
        fragment = Path("intel/ui/investigation-jobs-panel.html").read_text(encoding="utf-8")
        script = Path("intel/ui/investigation-jobs.js").read_text(encoding="utf-8")
        css = Path("intel/ui/investigation-jobs.css").read_text(encoding="utf-8")
        self.assertIn('aria-live="polite"', fragment)
        self.assertIn("From (UTC)", fragment)
        self.assertIn("200 transfer records", fragment)
        self.assertIn("@media(max-width:480px)", css)
        self.assertIn("minmax(0,1fr)", css)
        self.assertIn("prefers-reduced-motion", css)
        self.assertNotIn("innerHTML", script)
        self.assertNotIn("localStorage", script)
