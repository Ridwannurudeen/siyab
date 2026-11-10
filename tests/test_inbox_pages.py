import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from intel.workspace import SQLiteWorkspaceStore, WorkspaceError


class InboxPagesTest(unittest.TestCase):
    def test_older_and_newer_pages_preserve_every_alert_without_replay(self):
        with tempfile.TemporaryDirectory() as directory:
            store = SQLiteWorkspaceStore(Path(directory) / "workspace.sqlite3", clock=lambda: 1700000000)
            session = store.register("alice", "test-password-123")
            actor = session["user"]["id"]
            team = store.list_teams(actor)[0]["id"]
            store.add_watch(actor, team, "wrap.near")
            for index in range(75):
                event = {"kind": "feed_event", "signal": "account_transfer_observed", "subject": "wrap.near",
                         "detail": f"Observed transfer {index}", "source": {},
                         "observed": {"block_height": index + 1, "timestamp_ms": 1700000000001 + index}}
                identity = hashlib.sha256(json.dumps(event).encode()).hexdigest()
                store.ingest_event(identity, event, notify=True)
            latest = store.list_inbox(actor, team, limit=50)
            self.assertEqual(len(latest["items"]), 50)
            self.assertTrue(latest["has_older"])
            older = store.list_inbox(actor, team, limit=50, before=latest["next_before"])
            self.assertEqual(len(older["items"]), 25)
            self.assertFalse(older["has_older"])
            identities = [item["id"] for item in older["items"] + latest["items"]]
            self.assertEqual(identities, list(range(1, 76)))
            self.assertEqual(store.list_inbox(actor, team, after=latest["next_after"])["items"], [])
            with self.assertRaises(WorkspaceError):
                store.list_inbox(actor, team, after=1, before=50)
