import copy
import json
import tempfile
import unittest
import urllib.parse
from contextlib import closing
from email.message import Message
from pathlib import Path
from unittest.mock import Mock, patch

from intel.live import LiveAPI
from intel.workspace import SQLiteWorkspaceStore
from intel.workspace_api import WorkspaceAPI


VALUES = {"receiver": "intents.near", "amount": "1", "token": "near", "question_id": "why_verdict"}
VERDICT = {"outcome": "ASK", "action": "ask", "reasons": ["The read could not run."],
           "checks": [{"name": "receiver_exists", "status": "unknown", "detail": "The read could not run."}],
           "evidence_hash": "a" * 64, "freshness": {}, "verdict": None, "signing": "never", "latency_ms": 0}


class FakeAsk:
    enabled = True
    configuration_reason = None

    def __init__(self):
        self.rows = {}
        self.calls = []
        self.factory = Mock(side_effect=AssertionError("No client should be built by the route."))

    def cached(self, subject, verdict, question_id):
        raise AssertionError("Public reads must not read the answer cache.")

    def answer(self, values, **context):
        self.calls.append((dict(values), context))
        question_id = values.get("question_id", "free_text")
        cached = question_id in self.rows
        body = {"status": "explained", "question_id": question_id, "cached": cached,
                "statements": [{"text": "The read could not run.", "evidence_ids": ["check.receiver_exists"]}]}
        if question_id != "free_text":
            self.rows[question_id] = copy.deepcopy(body)
        return 200, body, {}


class AskAPITests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.store = SQLiteWorkspaceStore(Path(self.directory.name) / "workspace.sqlite3")
        self.session = self.store.register("alice", "test-password-123")
        self.actor = self.session["user"]["id"]
        self.team = self.store.create_team(self.actor, "Research")["id"]
        self.api = WorkspaceAPI(self.store, clock=lambda: 100)
        self.live = LiveAPI(clock=lambda: 100)
        self.ask = FakeAsk()
        self.api.ask = self.live.ask = self.ask
        self.read = patch.object(self.live, "_verdict", return_value=copy.deepcopy(VERDICT)).start()
        self.addCleanup(patch.stopall)
        self.socket = patch("socket.socket.connect", side_effect=AssertionError("Network is forbidden")).start()

    def get(self, question="why_verdict", extra=None):
        values = {key: VALUES[key] for key in ("receiver", "amount", "token")}
        values["question"] = question
        values.update(extra or {})
        return self.live.handle("/live/ask?" + urllib.parse.urlencode(values), "203.0.113.1", Message())

    def post(self, values=None, *, session=True, csrf=None, origin="https://siyab.xyz", team=None):
        headers = Message()
        headers["Origin"] = origin
        headers["Content-Type"] = "application/json"
        if session:
            headers["Cookie"] = "__Host-siyab-session=" + self.session["session_token"]
            headers["X-CSRF-Token"] = csrf or self.session["csrf_token"]
        return self.api.handle("POST", "/workspace-api/teams/" + (team or self.team) + "/ask",
                               "203.0.113.1", headers, json.dumps(VALUES if values is None else values).encode())

    def test_anonymous_get_cached_answer_requires_sign_in(self):
        self.ask.rows["why_verdict"] = {"status": "explained", "statements": []}
        answer = self.get()
        self.assertEqual(answer[0], 200)
        self.assertEqual(answer[1]["reason"], "sign_in_required")
        self.assertFalse(answer[1]["cached"])
        self.assertEqual(answer[1]["statements"], [])
        self.assertEqual(answer[1]["outcome"], "ASK")
        self.assertEqual(answer[1]["unknown_checks"], ["receiver_exists"])
        self.assertIsNotNone(answer[1]["unknown_notice"])
        self.assertIsNotNone(answer[1]["projection_sha256"])
        self.ask.factory.assert_not_called()

    def test_anonymous_get_miss_is_not_cached_and_never_calls_the_factory(self):
        answer = self.get()
        self.assertEqual((answer[0], answer[1]["reason"]), (200, "sign_in_required"))
        self.assertFalse(answer[1]["cached"])
        self.assertEqual(self.ask.rows, {})
        self.ask.factory.assert_not_called()

    def test_get_refuses_free_text_and_unknown_question_ids(self):
        for question in ("Tell me about it", "free_text", "", "other"):
            with self.subTest(question=question):
                answer = self.get(question)
                self.assertEqual((answer[0], answer[1]["error"]), (400, "invalid_input"))
        self.read.assert_not_called()

    def test_get_passes_through_verdict_errors(self):
        for status in (429, 503, 504):
            expected = (status, {"error": "read_failed"}, {"Retry-After": "2"})
            with patch.object(self.live, "_read", return_value=expected):
                self.assertEqual(self.get(), expected)

    def test_get_charges_one_unit_and_reuses_the_verdict_process_cache(self):
        self.get()
        self.get()
        self.assertEqual(self.read.call_count, 1)
        self.assertEqual(self.live._rates["203.0.113.1"][1:], (4, 13))

    def test_live_verdict_body_is_unchanged(self):
        target = "/live/verdict?receiver=intents.near&amount=1&token=near"
        expected = ('{"outcome": "ASK", "action": "ask", "reasons": ["The read could not run."], '
                    '"checks": [{"name": "receiver_exists", "status": "unknown", "detail": "The read could not run."}], '
                    '"evidence_hash": "' + "a" * 64 + '", "freshness": {}, "verdict": null, "signing": "never", "latency_ms": 0}')
        for service in (None, self.ask):
            self.live.ask = service
            response = self.live.handle(target, "203.0.113.1", Message())
            self.assertEqual(response[0], 200)
            self.assertEqual(json.dumps(response[1], ensure_ascii=False).encode(), expected.encode())

    def test_get_disabled_still_reads_verdict_and_requires_sign_in(self):
        self.live.ask = None
        self.assertEqual(self.get()[1]["reason"], "sign_in_required")
        self.read.assert_called_once()

    def test_get_misconfigured_still_reads_verdict_and_requires_sign_in(self):
        self.ask.configuration_reason = "misconfigured"
        self.ask.enabled = False
        self.assertEqual(self.get()[1]["reason"], "sign_in_required")
        self.read.assert_called_once()

    def test_post_without_session_is_401(self):
        answer = self.post(session=False)
        self.assertEqual((answer[0], answer[1]["error"]), (401, "authentication_required"))

    def test_post_with_bad_csrf_is_403(self):
        answer = self.post(csrf="b" * 43)
        self.assertEqual((answer[0], answer[1]["error"]), (403, "csrf_denied"))

    def test_post_with_foreign_origin_is_403(self):
        answer = self.post(origin="https://other.example")
        self.assertEqual((answer[0], answer[1]["error"]), (403, "origin_denied"))

    def test_post_by_non_member_is_403(self):
        answer = self.post(team="missing")
        self.assertEqual((answer[0], answer[1]["error"]), (403, "forbidden"))

    def test_post_by_viewer_is_allowed(self):
        with self.store._connection(write=True) as connection:
            connection.execute("UPDATE memberships SET role='viewer' WHERE user_id=?", (self.actor,))
        self.assertEqual(self.post()[0], 200)
        self.assertEqual(self.ask.calls[0][1]["user_id"], self.actor)

    def test_post_with_both_question_fields_is_400(self):
        answer = self.post({**VALUES, "question": "What was read?"})
        self.assertEqual((answer[0], answer[1]["error"]), (400, "invalid_fields"))

    def test_post_question_over_300_chars_is_400(self):
        values = {key: value for key, value in VALUES.items() if key != "question_id"}
        answer = self.post({**values, "question": "a" * 301})
        self.assertEqual((answer[0], answer[1]["error"]), (400, "invalid_question"))

    def test_post_invalid_question_and_transfer_fields(self):
        for update in ({"question_id": "other"}, {"question_id": []}, {"amount": "0"},
                       {"receiver": "receiver.testnet"}, {"token": "token.testnet"}, {"receiver": 1}):
            with self.subTest(update=update):
                answer = self.post({**VALUES, **update})
                self.assertEqual((answer[0], answer[1]["error"]), (400, "invalid_question"))

    def test_post_non_utf8_question_is_invalid(self):
        values = {key: value for key, value in VALUES.items() if key != "question_id"}
        answer = self.post({**values, "question": "What " + chr(0xd800) + " was read?"})
        self.assertEqual((answer[0], answer[1].get("error")), (400, "invalid_question"))
        self.assertEqual(self.ask.calls, [])

    def test_post_question_controls_are_invalid(self):
        values = {key: value for key, value in VALUES.items() if key != "question_id"}
        for question in ("", "   ", "What\nwas read?", "What\twas read?", "What" + chr(127) + "was read?", None):
            with self.subTest(question=question):
                answer = self.post({**values, "question": question})
                self.assertEqual((answer[0], answer[1]["error"]), (400, "invalid_question"))
        self.assertEqual(self.ask.calls, [])

    def test_post_unicode_question_controls_are_invalid(self):
        values = {key: value for key, value in VALUES.items() if key != "question_id"}
        for question in ("What\u0085was read?", "What\u009fwas read?", "What\u202ewas read?", "What\u200bwas read?"):
            with self.subTest(question=question):
                answer = self.post({**values, "question": question})
                self.assertEqual((answer[0], answer[1].get("error")), (400, "invalid_question"))
                self.assertEqual(answer[1]["message"],
                                 "Use a question of 1 to 300 characters without control characters.")
                self.assertEqual(self.ask.calls, [])

    def test_post_eleventh_ask_in_a_minute_is_429(self):
        for _ in range(10):
            self.assertEqual(self.post()[0], 200)
        self.assertEqual(self.post()[0], 429)
        self.assertEqual(len(self.ask.calls), 10)

    def real_service(self):
        from intel.ask import AskService

        settings = patch.dict("os.environ", {"SIYAB_ASK": "1"})
        settings.start()
        self.addCleanup(settings.stop)
        key = patch("intel.ask._read_key", return_value="test-sentinel")
        key.start()
        self.addCleanup(key.stop)
        client = Mock()
        client.create.return_value = {
            "stop_reason": "end_turn",
            "text": json.dumps({"answerable": True, "points": [
                {"item_id": "check.receiver_exists", "aspect": "unknown"}], "gaps": []}),
            "usage": {"input_tokens": 100, "output_tokens": 20,
                      "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0}}
        factory = Mock(return_value=client)
        service = AskService(self.directory.name, self.live.store,
                             lambda query, peer, headers: self.live.handle(
                                 "/live/verdict?" + urllib.parse.urlencode(sorted(query.items())), peer, headers),
                             client_factory=factory)
        self.api.ask = self.live.ask = service
        return client, factory

    def test_post_fixed_question_cache_stays_private(self):
        client, factory = self.real_service()
        answer = self.post()
        self.assertEqual((answer[0], answer[1]["status"]), (200, "explained"))
        with patch.object(self.api.ask.cache, "get", side_effect=AssertionError("No public cache read")):
            public = self.get()[1]
        self.assertEqual((public["reason"], public["cached"], public["statements"]),
                         ("sign_in_required", False, []))
        self.assertEqual(client.create.call_count, 1)
        self.assertEqual(factory.call_count, 1)

    def test_post_cache_hit_charges_no_tokens(self):
        import sqlite3

        client, factory = self.real_service()
        self.post()
        with closing(sqlite3.connect(Path(self.directory.name) / "ask.sqlite3")) as connection:
            before = connection.execute("SELECT * FROM counters ORDER BY counter,day").fetchall()
        self.assertTrue(self.post()[1]["cached"])
        with closing(sqlite3.connect(Path(self.directory.name) / "ask.sqlite3")) as connection:
            self.assertEqual(connection.execute("SELECT * FROM counters ORDER BY counter,day").fetchall(), before)
        self.assertEqual(client.create.call_count, 1)
        self.assertEqual(factory.call_count, 1)

    def test_post_free_text_is_never_cached(self):
        import sqlite3

        client, factory = self.real_service()
        values = {key: value for key, value in VALUES.items() if key != "question_id"}
        values["question"] = "  What was read?  "
        for _ in range(2):
            answer = self.post(values)
            self.assertEqual((answer[0], answer[1]["status"]), (200, "explained"))
            self.assertEqual(answer[1]["question"], "What was read?")
            self.assertFalse(answer[1]["cached"])
        with closing(sqlite3.connect(Path(self.directory.name) / "ask.sqlite3")) as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM answers").fetchone()[0], 0)
        self.assertEqual(client.create.call_count, 2)
        self.assertEqual(factory.call_count, 2)

    def test_free_prose_fields_are_invalid_and_never_cached(self):
        client, _ = self.real_service()
        self.refused_output(client, {"answerable": True, "statements": [
            {"text": "The read could not run.", "evidence_ids": ["check.receiver_exists"]}], "not_in_evidence": []})

    def refused_output(self, client, value):
        client.create.return_value["text"] = json.dumps(value)
        with patch.object(self.api, "_allowed", return_value=(True, 100)), patch.object(
                self.live, "_rate_limit", return_value=None):
            response = self.post()
            self.assertEqual(response[0], 200)
            body = response[1]
            self.assertEqual((body["status"], body["reason"], body["statements"], body["not_in_evidence"],
                              body["answerable"], body["cached"], body["model"], body["generated_at"]),
                             ("unavailable", "invalid_output", [], [], None, False, None, None))
            public = self.get()[1]
            self.assertEqual((public["status"], public["reason"], public["statements"], public["cached"]),
                             ("unavailable", "sign_in_required", [], False))
        import sqlite3

        with closing(sqlite3.connect(Path(self.directory.name) / "ask.sqlite3")) as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM answers").fetchone()[0], 0)

    def test_malformed_cached_rows_are_refetched_and_stay_private(self):
        from intel import ask

        client, _ = self.real_service()
        subject = {"network": "mainnet", **{key: VALUES[key] for key in ("receiver", "amount", "token")}}
        projection = ask.project(subject, VERDICT, self.live.store)
        key = ask.cache_key(self.api.ask.model, self.api.ask.effort, "why_verdict", projection)
        expected = json.loads(client.create.return_value["text"])
        for body in ({}, [], {"answerable": True, "statements": [
                {"text": "The read could not run.", "evidence_ids": ["check.receiver_exists"]}]}):
            with self.subTest(body=body):
                self.api.ask.cache.put(key, body, 100)
                before = client.create.call_count
                public = self.get()[1]
                self.assertEqual(public["reason"], "sign_in_required")
                self.assertEqual(public["statements"], [])
                self.assertEqual(client.create.call_count, before)
                response = self.post()
                self.assertEqual(response[0], 200)
                result = response[1]
                self.assertEqual((result["status"], result["reason"], result["cached"]),
                                 ("explained", None, False))
                self.assertEqual(result["statements"], ask.render_answer(expected, projection)["statements"])
                self.assertEqual(client.create.call_count, before + 1)
                cached = self.api.ask.cache.get(key, 100)
                self.assertEqual(cached["selection"], expected)
                self.assertTrue(self.post()[1]["cached"])
                self.assertEqual(client.create.call_count, before + 1)

    def test_semantically_invalid_cached_rows_are_unavailable_and_stay_private(self):
        from intel import ask

        client, _ = self.real_service()
        subject = {"network": "mainnet", **{key: VALUES[key] for key in ("receiver", "amount", "token")}}
        projection = ask.project(subject, VERDICT, self.live.store)
        key = ask.cache_key(self.api.ask.model, self.api.ask.effort, "why_verdict", projection)
        body = {"selection": {"answerable": True, "points": [
            {"item_id": "check.missing", "aspect": "read"}], "gaps": []}, "generated_at": "1970-01-01T00:01:40Z"}
        self.api.ask.cache.put(key, body, 100)
        public = self.get()[1]
        self.assertEqual((public["reason"], public["statements"]), ("sign_in_required", []))
        response = self.post()
        self.assertEqual(response[0], 200)
        result = response[1]
        self.assertEqual((result["status"], result["reason"], result["statements"],
                          result["not_in_evidence"], result["answerable"], result["cached"]),
                         ("unavailable", "invalid_output", [], [], None, False))
        client.create.assert_not_called()

    def test_projected_account_fields_stay_in_signed_in_evidence_chips(self):
        from intel import ask

        client, _ = self.real_service()
        for outcome, status in (("ALLOW", "pass"), ("DENY", "signal"), ("ASK", "unknown")):
            for account in ("intents.near", "reader.tg", "reader.aurora", "reader.sweat", "ab" * 32,
                            "reader.com", "good-wallet.near"):
                with self.subTest(outcome=outcome, account=account):
                    verdict = copy.deepcopy(VERDICT)
                    verdict["outcome"] = outcome
                    verdict["checks"] = [{"name": "account_age", "status": status, "detail": "Recorded creation date."}]
                    self.read.return_value = verdict
                    self.live._cache.clear()
                    selection = {"answerable": True, "points": [
                        {"item_id": "check.account_age", "aspect": "read"}, {"item_id": "subject", "aspect": "read"}], "gaps": []}
                    client.create.return_value["text"] = json.dumps(selection)
                    values = {**VALUES, "receiver": account}
                    with patch.object(self.api, "_allowed", return_value=(True, 100)), patch.object(
                            self.live, "_rate_limit", return_value=None):
                        body = self.post(values)[1]
                        self.assertEqual((body["status"], body["outcome"]), ("explained", outcome))
                        projection = ask.project({"network": "mainnet", **{
                            key: values[key] for key in ("receiver", "token", "amount")}}, verdict, self.live.store)
                        self.assertEqual(body["statements"], ask.render_answer(selection, projection)["statements"])
                        self.assertEqual(body["statements"][1]["evidence_chip"]["fields"]["receiver"], account)
                        public = self.get(extra={"receiver": account})[1]
                        self.assertEqual((public["reason"], public["cached"], public["statements"]),
                                         ("sign_in_required", False, []))

    def test_cited_pass_keeps_uncited_unknown_notice(self):
        from intel import ask

        client, _ = self.real_service()
        verdict = copy.deepcopy(VERDICT)
        verdict["checks"] = [{"name": "receiver_exists", "status": "pass", "detail": "The receiver exists."},
                             {"name": "account_age", "status": "unknown", "detail": "Read unavailable."}]
        self.read.return_value = verdict
        client.create.return_value["text"] = json.dumps({"answerable": True, "points": [
            {"item_id": "check.receiver_exists", "aspect": "read"}], "gaps": []})
        response = self.post()[1]
        self.assertEqual(response["status"], "explained")
        self.assertEqual(response["unknown_checks"], ["account_age"])
        self.assertEqual(response["unknown_notice"], ask.UNKNOWN_NOTICE)
        self.assertEqual(client.create.call_count, 1)

    def test_selected_fixture_evidence_is_cached_only_for_signed_in_users(self):
        from intel import ask
        from scripts import ask_eval

        items = ask_eval.corpus()[:2]
        client, _ = self.real_service()
        for item in items:
            with self.subTest(item=item["item"]):
                self.read.return_value = item["verdict"]
                self.live._cache.clear()
                selection = {"answerable": True, "points": [{"item_id": "subject", "aspect": "read"}], "gaps": []}
                client.create.return_value["text"] = json.dumps(selection)
                values = {key: item["subject"][key] for key in ("receiver", "amount", "token")}
                values["question_id"] = item["question_id"]
                before = client.create.call_count
                answer = self.post(values)[1]
                self.assertEqual((answer["status"], answer["cached"]), ("explained", False))
                self.assertEqual(answer["statements"], ask.render_answer(selection, item["projection"])["statements"])
                self.assertEqual(client.create.call_count, before + 1)
                cached = self.post(values)[1]
                self.assertEqual((cached["status"], cached["cached"]), ("explained", True))
                self.assertEqual(cached["generated_at"], answer["generated_at"])
                self.assertIsNotNone(cached["generated_at"])
                self.assertEqual(client.create.call_count, before + 1)
                public = self.get(item["question_id"], extra={
                    key: values[key] for key in ("receiver", "amount", "token")})[1]
                self.assertEqual((public["reason"], public["statements"], public["cached"]),
                                 ("sign_in_required", [], False))

    def test_post_body_holds_no_user_or_team_id(self):
        answer = self.post()
        self.assertNotIn(self.actor, json.dumps(answer[1]))
        self.assertNotIn(self.team, json.dumps(answer[1]))
        self.assertEqual(set(self.ask.calls[0][0]), set(VALUES))

    def test_reserved_membership_is_passed_to_service(self):
        self.assertFalse(self.store.is_reserved(self.actor))
        self.store.grant_reserved("alice")
        self.assertTrue(self.store.is_reserved(self.actor))
        self.post()
        self.assertTrue(self.ask.calls[0][1]["reserved"])
