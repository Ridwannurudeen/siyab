import json
import tempfile
import unittest
from email.message import Message
from pathlib import Path

from intel.delivery import TelegramDelivery
from intel.workspace import SQLiteWorkspaceStore
from intel.workspace_api import WorkspaceAPI
from tests.test_delivery import FakeClient


class DeliveryAPITest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.now = 1700000000.0
        self.store = SQLiteWorkspaceStore(Path(self.directory.name) / "workspace.sqlite3", clock=lambda: self.now)
        self.owner = self.store.register("owner", "test-password-123")
        self.viewer = self.store.register("viewer", "test-password-123")
        self.outsider = self.store.register("outsider", "test-password-123")
        self.team = self.store.list_teams(self.owner["user"]["id"])[0]["id"]
        invite = self.store.invite_member(self.owner["user"]["id"], self.team, "viewer", "viewer")
        self.store.join_team(self.viewer["user"]["id"], invite["token"])
        self.client = FakeClient()
        self.delivery = TelegramDelivery(self.store, self.client, clock=lambda: self.now)
        self.api = WorkspaceAPI(self.store, delivery=self.delivery)

    def request(self, suffix="", *, actor=None, method="GET", body=None, csrf=True, origin="https://siyab.xyz"):
        actor = actor or self.owner
        headers = Message()
        headers["Cookie"] = "__Host-siyab-session=" + actor["session_token"]
        headers["Content-Type"] = "application/json"
        headers["Origin"] = origin
        if csrf:
            headers["X-CSRF-Token"] = actor["csrf_token"]
        return self.api.handle(method, "/workspace-api/teams/" + self.team + "/delivery" + suffix,
                               "203.0.113.1", headers, json.dumps(body or {}).encode())

    def test_authenticated_csrf_origin_owner_and_tenant_boundaries(self):
        self.assertEqual(self.request(actor=self.outsider)[0], 403)
        self.assertEqual(self.request("/link", method="POST", csrf=False)[0], 403)
        self.assertEqual(self.request("/link", method="POST", origin="https://evil.test")[0], 403)
        self.assertEqual(self.request("/link", method="POST", actor=self.viewer)[0], 403)
        self.assertEqual(self.request("/link", method="POST", body={"chat_id": 999})[0], 400)
        status, link, _ = self.request("/link", method="POST")
        self.assertEqual(status, 201)
        self.assertTrue(link["link"].startswith("https://t.me/siyabonnearbot?start="))
        public = self.request(actor=self.viewer)[1]
        self.assertNotIn("generation", public)
        self.assertNotIn("observed_chat_label", public)
        self.assertEqual(self.request("/confirm", method="POST", body={"generation": link["generation"]})[0], 409)
        nonce = link["link"].split("start=")[1]
        self.client.updates = [{"update_id": 1, "message": {"text": "/start " + nonce,
            "chat": {"id": 555, "type": "private"}, "from": {"id": 555, "is_bot": False}}}]
        self.delivery.run_once()
        self.assertEqual(self.request("/confirm", method="POST", body={"generation": link["generation"]})[0], 200)
        self.assertEqual(self.request("/revoke", method="POST", actor=self.viewer)[0], 403)
        self.assertEqual(self.request("/revoke", method="POST")[1]["state"], "disconnected")

    def test_log_validation_and_retry_cannot_accept_foreign_identifiers(self):
        self.assertEqual(self.request("/log?limit=0")[0], 400)
        self.assertEqual(self.request("/log?before=1&limit=10")[0], 200)
        self.assertEqual(self.request("/1/retry", method="POST", actor=self.outsider)[0], 403)
        self.assertEqual(self.request("/1/retry", method="POST", actor=self.viewer)[0], 403)
        self.assertEqual(self.request("/1/retry", method="POST")[0], 409)
        self.assertEqual(self.request("/unknown", method="POST")[0], 404)

    def test_optional_backend_returns_honest_unavailable(self):
        self.api = WorkspaceAPI(self.store)
        self.assertEqual(self.request()[0], 503)
