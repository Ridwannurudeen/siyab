import hashlib
import http.client
import json
import tempfile
import threading
import unittest
from contextlib import closing
from email.message import Message
from pathlib import Path
from unittest import mock

from intel import accounts, api, enrichment, history, investigation_jobs, live, portfolio
from intel.workspace import SQLiteWorkspaceStore
from intel.workspace_api import WorkspaceAPI


class AnalyticsLiveAPITest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.directory = enrichment.EntityDirectory()
        self.data = mock.Mock(spec=portfolio.PortfolioData)
        self.portfolios = mock.Mock(spec=portfolio.PortfolioHistory)
        self.service = live.LiveAPI(entity_directory=self.directory, portfolio_data=self.data, portfolio_history=self.portfolios)

    def request(self, path):
        return self.service.handle(path, "203.0.113.8", {})

    def test_entity_directory_lookup_and_profile_overlay_preserve_provenance(self):
        status, body, _ = self.request("/live/entity-directory?query=wrap&limit=2")
        self.assertEqual(status, 200)
        self.assertLessEqual(len(body["items"]), 2)
        expected = self.directory.lookup("wrap.near")
        self.assertEqual(self.request("/live/accounts/wrap.near/enrichment")[:2], (200, expected))
        with mock.patch.object(accounts.AccountData, "profile", return_value={"account_id": "wrap.near"}):
            status, body, _ = self.request("/live/accounts/wrap.near/profile")
        self.assertEqual(status, 200)
        self.assertEqual(body["enrichment"], expected)

    def test_portfolio_routes_save_snapshots_and_history_bypasses_cache(self):
        observation = {"account_id": "wrap.near", "coverage": {"history_complete": False}}
        self.data.inspect.return_value = observation
        self.data.historical.return_value = observation
        self.portfolios.save.return_value = {"id": "a" * 64, "url": "/live/portfolio-data/" + "a" * 64 + ".json"}
        status, body, _ = self.request("/live/accounts/wrap.near/portfolio?max_tokens=40")
        self.assertEqual(status, 200)
        self.assertIn("snapshot", body)
        self.data.inspect.assert_called_once_with("wrap.near", max_tokens=40)
        self.portfolios.save.assert_called_with(observation)
        self.assertEqual(self.request("/live/accounts/wrap.near/portfolio-at-block?block_height=123&contracts=wrap.near,token.near")[0], 200)
        self.data.historical.assert_called_once_with("wrap.near", 123, contracts=["wrap.near", "token.near"])
        self.portfolios.list_records.side_effect = [{"items": []}, {"items": ["fresh"]}]
        path = "/live/accounts/wrap.near/portfolio-history?before=0&limit=100&asset_id=native%3Anear"
        self.assertEqual(self.request(path)[1], {"items": []})
        self.assertEqual(self.request(path)[1], {"items": ["fresh"]})
        self.portfolios.list_records.assert_called_with("wrap.near", before=0, limit=100, asset_id="native:near")

    def test_portfolio_snapshot_bytes_and_invalid_inputs(self):
        raw = b'{"assets":[]}\n'
        identifier = hashlib.sha256(raw).hexdigest()
        self.portfolios.get_bytes.return_value = raw
        status, body, headers = self.request("/live/portfolio-data/" + identifier + ".json")
        self.assertEqual((status, body), (200, raw))
        self.assertIn("immutable", headers["Cache-Control"])
        for path in ("/live/entity-directory?limit=101", "/live/entity-directory?query=x&query=y",
                     "/live/accounts/foo.testnet/portfolio", "/live/accounts/wrap.near/portfolio?max_tokens=101",
                     "/live/accounts/wrap.near/portfolio-at-block?contracts=wrap.near", "/live/accounts/wrap.near/portfolio-at-block?block_height=1&contracts=wrap.near,wrap.near",
                     "/live/accounts/wrap.near/portfolio-history?before=-1", "/live/portfolio-data/" + identifier + ".json?x=1"):
            self.assertEqual(self.request(path)[0], 400, path)
        self.assertEqual(live.LiveAPI().handle("/live/accounts/wrap.near/portfolio", "203.0.113.8", {})[0], 503)

    def test_current_portfolio_is_not_cached_and_partial_values_remain_unknown(self):
        self.data.inspect.side_effect = [{"summary": {"total_value_usd": None, "priced_subtotal_usd": "2", "unpriced_assets": 1}},
                                         {"summary": {"total_value_usd": None, "priced_subtotal_usd": "3", "unpriced_assets": 2}}]
        self.portfolios.save.return_value = {"id": "a" * 64}
        first = self.request("/live/accounts/wrap.near/portfolio")[1]
        second = self.request("/live/accounts/wrap.near/portfolio")[1]
        self.assertEqual(first["summary"]["priced_subtotal_usd"], "2")
        self.assertEqual(second["summary"]["priced_subtotal_usd"], "3")
        self.assertIsNone(second["summary"]["total_value_usd"])

    def test_actual_portfolio_observation_round_trips_through_retained_history_and_exact_bytes(self):
        from tests.test_portfolio import NOW, Providers, Reader

        observation_reader = portfolio.PortfolioData(Reader(), client_factory=Providers().factory, clock=lambda: NOW)
        archive = portfolio.PortfolioHistory(self.root / "portfolio.sqlite3", clock=lambda: NOW)
        self.service = live.LiveAPI(portfolio_data=observation_reader, portfolio_history=archive)
        status, document, _ = self.request("/live/accounts/alice.near/portfolio?max_tokens=1")
        self.assertEqual(status, 200)
        snapshot = document.pop("snapshot")
        status, raw, _ = self.request(snapshot["url"])
        self.assertEqual(status, 200)
        self.assertEqual(hashlib.sha256(raw).hexdigest(), snapshot["id"])
        self.assertEqual(json.loads(raw), document)
        status, chart, _ = self.request("/live/accounts/alice.near/portfolio-history?asset_id=native%3Anear")
        self.assertEqual(status, 200)
        self.assertEqual(chart["items"][0]["effective_at"], document["effective_at"])
        self.assertFalse(chart["coverage"]["history_complete"])

    def test_portfolio_history_answers_503_for_a_retained_asset_with_a_missing_or_mistyped_balance_field(self):
        from tests.test_portfolio import NOW, Providers, Reader

        providers = Providers()
        del providers.prices["near:wrap.near"]
        observation = portfolio.PortfolioData(Reader(), client_factory=providers.factory, clock=lambda: NOW).inspect("alice.near")
        route = "/live/accounts/alice.near/portfolio-history?asset_id=nep141%3Awrap.near"
        for name, edit in (("missing balance_base_units", lambda asset: asset.pop("balance_base_units")),
                           ("missing decimals", lambda asset: asset.pop("decimals")),
                           ("missing balance", lambda asset: asset.pop("balance")),
                           ("numeric balance_base_units", lambda asset: asset.update(balance_base_units=1)),
                           ("fractional balance_base_units", lambda asset: asset.update(balance_base_units="1.5")),
                           ("boolean decimals", lambda asset: asset.update(decimals=True)),
                           ("string decimals", lambda asset: asset.update(decimals="24")),
                           ("out of range decimals", lambda asset: asset.update(decimals=256)),
                           ("numeric balance", lambda asset: asset.update(balance=1)),
                           ("inconsistent balance", lambda asset: asset.update(balance="2")),
                           ("balance without base units", lambda asset: asset.update(balance_base_units=None))):
            with self.subTest(name=name):
                archive = portfolio.PortfolioHistory(self.root / (name.replace(" ", "-") + ".sqlite3"), clock=lambda: NOW)
                self.service = live.LiveAPI(portfolio_history=archive)
                saved = archive.save(observation)
                status, chart, _ = self.request(route)
                self.assertEqual(status, 200)
                self.assertEqual((chart["items"][0]["balance"], chart["items"][0]["asset_value_usd"]), ("1", None))
                document = json.loads(archive.get_bytes(saved["id"]))
                edit(next(asset for asset in document["assets"] if asset["asset_id"] == "nep141:wrap.near"))
                body = portfolio._body(document)
                with archive._connection(write=True) as connection:
                    connection.execute("UPDATE observations SET body=?,id=? WHERE id=?", (body, hashlib.sha256(body).hexdigest(), saved["id"]))
                status, answer, _ = self.request(route)
                self.assertEqual((status, answer["error"]), (503, "read_failed"))
                self.assertIsNotNone(archive.get_bytes(archive.save(observation)["id"]))
                self.assertEqual(self.request(route)[0], 503)

    def test_http_transport_independently_checks_portfolio_snapshot_hash_before_immutable_cache(self):
        raw = b'{"assets":[]}\n'
        identifier = hashlib.sha256(raw).hexdigest()
        service = live.LiveAPI()
        server = api.make_server("127.0.0.1", 0, live_routes=service)
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        try:
            with mock.patch.object(service, "handle", return_value=(200, raw, {"Cache-Control": "public, max-age=31536000, immutable"})):
                for target, immutable in (("/live/portfolio-data/" + identifier + ".json", True),
                                          ("/live/portfolio-data/" + "a" * 64 + ".json", False),
                                          ("/live/portfolio-data/" + identifier + ".json?x=1", False)):
                    with closing(http.client.HTTPConnection(*server.server_address, timeout=5)) as connection:
                        connection.request("GET", target)
                        response = connection.getresponse()
                        self.assertEqual(response.read(), raw)
                        self.assertEqual("immutable" in response.getheader("Cache-Control"), immutable)
        finally:
            server.shutdown()
            server.server_close()
            worker.join(timeout=5)

    def test_serve_wires_optional_analytics_to_external_state_only(self):
        server = mock.Mock()
        server.server_address = ("127.0.0.1", 8792)
        with mock.patch.dict("os.environ", {}, clear=True), mock.patch.object(api, "make_server", return_value=server) as make_server:
            self.assertEqual(api.serve("127.0.0.1", 8792, live=True, state_dir=self.root), 0)
        live_service = make_server.call_args.kwargs["live_routes"]
        private_service = make_server.call_args.kwargs["workspace_routes"]
        self.assertEqual(live_service.portfolio_history.path, self.root / "portfolio.sqlite3")
        self.assertEqual(private_service.investigation_jobs.path, self.root / "investigation-jobs.sqlite3")
        self.assertEqual(live_service.entity_directory.path, self.root / "entity-sources.sqlite3")
        self.assertIn("investigations", private_service.monitor_status())
        server.server_close.assert_called_once()


class JobsWorkspaceAPITest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        self.store = SQLiteWorkspaceStore(root / "workspace.sqlite3")
        self.jobs = investigation_jobs.InvestigationJobs(root / "investigation-jobs.sqlite3", history.AccountHistory(root / "history.sqlite3"))
        self.api = WorkspaceAPI(self.store, investigation_jobs=self.jobs)
        self.owner = self.store.register("owner", "test-password-123")
        self.viewer = self.store.register("viewer", "test-password-123")
        self.team = self.store.list_teams(self.owner["user"]["id"])[0]["id"]
        invite = self.store.invite_member(self.owner["user"]["id"], self.team, "viewer", "viewer")
        self.store.join_team(self.viewer["user"]["id"], invite["token"])
        self.route = "/workspace-api/teams/" + self.team + "/investigation-jobs"

    def request(self, path, *, session=None, method="GET", values=None, csrf=True):
        session = self.owner if session is None else session
        headers = Message()
        headers["Cookie"] = "__Host-siyab-session=" + session["session_token"]
        headers["Origin"] = "https://siyab.xyz"
        headers["Content-Type"] = "application/json"
        if csrf:
            headers["X-CSRF-Token"] = session["csrf_token"]
        return self.api.handle(method, path, "203.0.113.8", headers, json.dumps(values or {}).encode())

    def test_jobs_lifecycle_role_acl_and_csrf(self):
        self.assertEqual(self.request(self.route, method="POST", values={"account": "wrap.near"}, csrf=False)[0], 403)
        self.assertEqual(self.request(self.route, session=self.viewer, method="POST", values={"account": "wrap.near"})[0], 403)
        status, job, _ = self.request(self.route, method="POST", values={"account": "wrap.near", "max_pages": 2})
        self.assertEqual(status, 201)
        route = self.route + "/" + job["id"]
        self.assertEqual(self.request(route, session=self.viewer)[1]["id"], job["id"])
        self.assertEqual(self.request(route + "/result?before=0&limit=25", session=self.viewer)[0], 200)
        self.assertEqual(self.request(route + "/cancel", session=self.viewer, method="POST")[0], 403)
        self.assertEqual(self.request(route + "/cancel", method="POST")[1]["state"], "cancelled")
        self.assertEqual(self.request(route + "/retry", method="POST")[1]["state"], "queued")
        self.assertEqual(len(self.request(self.route)[1]["items"]), 1)

    def test_team_isolation_unknown_fields_and_invalid_bounds(self):
        stranger = self.store.register("stranger", "test-password-123")
        self.assertEqual(self.request(self.route, session=stranger)[0], 403)
        for values in ({"account": "wrap.near", "team_id": self.team}, {"account": "wrap.near", "max_pages": 101}, {"account": "foo.testnet"}):
            self.assertEqual(self.request(self.route, method="POST", values=values)[0], 400)
        self.assertEqual(self.request(self.route + "?limit=101")[0], 400)
        self.assertEqual(self.request(self.route + "/" + "a" * 32)[0], 404)

    def test_active_job_capacity_is_shared_by_every_session_in_the_same_team(self):
        self.store.set_role(self.owner["user"]["id"], self.team, self.viewer["user"]["id"], "editor")
        for number in range(10):
            session = self.owner if number % 2 else self.viewer
            status, body, _ = self.request(self.route, method="POST", session=session, values={"account": "wrap.near"})
            self.assertEqual(status, 201, body)
        self.assertEqual(self.request(self.route, method="POST", session=self.viewer, values={"account": "wrap.near"})[0], 503)
        self.assertEqual(len(self.request(self.route, session=self.owner)[1]["items"]), 10)

    def test_unconfigured_jobs_fail_safely_after_membership_check(self):
        self.api = WorkspaceAPI(self.store)
        self.assertEqual(self.request(self.route)[0], 503)
        stranger = self.store.register("stranger", "test-password-123")
        self.assertEqual(self.request(self.route, session=stranger)[0], 403)
