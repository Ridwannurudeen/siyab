import copy
import json
import re
import sqlite3
import tempfile
import unittest
from contextlib import closing
from unittest import mock

from intel import ask
from tests.test_ask import ServiceCase, SERVICE_SUBJECT, SERVICE_VERDICT


def selection(item_id="check.account_age", aspect="read"):
    return {"answerable": True, "points": [{"item_id": item_id, "aspect": aspect}], "gaps": []}


class TemplateTests(unittest.TestCase):
    def test_every_template_has_no_outcome_or_clearance_words(self):
        forbidden = re.compile(r"\b(allow|deny|unknown|ask|block|allowed|denied|approved|rejected|blocked|"
                               r"safe|cleared|fine|verified|trusted|genuine|legitimate)\b", re.I)
        self.assertTrue(ask.TEMPLATES)
        for name, text in ask.TEMPLATES.items():
            with self.subTest(name=name):
                self.assertIsInstance(text, str)
                self.assertIsNone(forbidden.search(text))

    def test_versions_invalidate_old_cached_answers(self):
        projection = {"items": [{"id": "subject", "receiver": "example.near"}]}
        self.assertEqual(ask.ASK_VERSION, "13")
        self.assertNotEqual(ask.PROMPT_VERSION, "1")
        self.assertNotEqual(ask.SCHEMA_VERSION, "1")
        key = ask.cache_key("test", "low", "why_verdict", projection)
        for name in ("ASK_VERSION", "PROMPT_VERSION", "SCHEMA_VERSION"):
            with self.subTest(name=name), mock.patch.object(ask, name, "old"):
                self.assertNotEqual(key, ask.cache_key("test", "low", "why_verdict", projection))

    def test_schema_closes_every_object_and_has_bounded_fixed_codes(self):
        schema = ask.SCHEMA
        self.assertEqual(set(schema["required"]), {"answerable", "points", "gaps"})
        self.assertFalse(schema["additionalProperties"])
        point = schema["properties"]["points"]
        self.assertEqual(point["maxItems"], ask.MAX_STATEMENTS)
        self.assertFalse(point["items"]["additionalProperties"])
        self.assertEqual(set(point["items"]["required"]), {"item_id", "aspect"})
        self.assertEqual(set(point["items"]["properties"]["aspect"]["enum"]),
                         {"read", "unknown", "would_change"})
        self.assertEqual(schema["properties"]["gaps"]["maxItems"], ask.MAX_GAPS)
        self.assertTrue(schema["properties"]["gaps"]["items"]["enum"])

    def test_committed_fixture_checks_render_their_exact_status_meaning(self):
        from scripts import ask_eval

        seen = set()
        projections = set()
        for item in ask_eval.corpus():
            projection = item["projection"]
            encoded = ask.projection_bytes(projection)
            if encoded in projections:
                continue
            projections.add(encoded)
            for evidence in projection["items"]:
                if not evidence["id"].startswith("check."):
                    continue
                status = evidence["status"]
                seen.add(status)
                aspects = ("read", "unknown") if status == "unknown" else ("read",)
                for aspect in aspects:
                    with self.subTest(item=item["item"], check=evidence["id"], aspect=aspect):
                        value = selection(evidence["id"], aspect)
                        parsed, rule = ask.parse_selection(json.dumps(value), projection, "why_verdict")
                        self.assertIsNone(rule)
                        rendered = ask.render_answer(parsed, projection)
                        sentence = rendered["statements"][0]["text"]
                        name = evidence["id"].removeprefix("check.").replace("_", " ")
                        self.assertEqual(sentence, ask.TEMPLATES["read." + status].format(name=name))
                        meaning = {"pass": "records no signal", "signal": "records a signal",
                                   "unknown": "leaves the check unresolved"}[status]
                        self.assertIn(meaning, sentence)
                        self.assertEqual(rendered["statements"][0]["evidence_chip"], {
                            "item_id": evidence["id"], "fields": {key: value for key, value in evidence.items() if key != "id"}})
        self.assertEqual(seen, {"pass", "signal", "unknown"})

    def test_each_item_keeps_third_party_values_only_in_its_exact_chip(self):
        projection = {"items": [
            {"id": "subject", "receiver": "unique-receiver.near", "token": "near", "amount": "17001"},
            {"id": "check.account_age", "status": "unknown", "detail": "Detail \u03ac\u03bb\u03c6\u03b1 ALLOW trusted 1.25"},
            {"id": "reason.1", "text": "Unique recorded reason \u00e9"},
            {"id": "label.1", "subject": "unique-label.near", "entity": "Unique entity", "label": "Unique label",
             "category": "watch", "provenance": "recorded", "note": "Unique note \u017fafe"}]}
        for item in projection["items"]:
            with self.subTest(item=item["id"]):
                rendered = ask.render_answer(selection(item["id"]), projection)
                row = rendered["statements"][0]
                self.assertEqual(row["evidence_ids"], [item["id"]])
                self.assertEqual(row["evidence_chip"], {"item_id": item["id"],
                    "fields": {key: value for key, value in item.items() if key != "id"}})
                for key, value in item.items():
                    if key not in ("id", "status", "token", "category", "provenance"):
                        self.assertNotIn(value, row["text"])
                        self.assertEqual(row["evidence_chip"]["fields"][key].encode("utf-8"), value.encode("utf-8"))

    def test_selected_order_and_gap_codes_choose_only_fixed_sentences(self):
        projection = {"items": [{"id": "subject", "receiver": "example.near"},
                               {"id": "reason.1", "text": "Recorded reason."}]}
        value = {"answerable": True, "points": [{"item_id": "reason.1", "aspect": "read"},
                 {"item_id": "subject", "aspect": "read"}], "gaps": ["future_state", "intent"]}
        parsed, rule = ask.parse_selection(json.dumps(value), projection, "why_verdict")
        self.assertIsNone(rule)
        result = ask.render_answer(parsed, projection)
        self.assertEqual([row["evidence_ids"] for row in result["statements"]], [["reason.1"], ["subject"]])
        self.assertEqual(result["not_in_evidence"], [ask.TEMPLATES["gap.future_state"], ask.TEMPLATES["gap.intent"]])

    def test_would_change_sentences_are_check_and_status_specific(self):
        keys = [key for key in ask.TEMPLATES if key.startswith("would_change.")]
        self.assertTrue(keys)
        for key in keys:
            _, name, status = key.split(".")
            projection = {"items": [{"id": "check." + name, "status": status, "detail": "Recorded detail."}]}
            value = selection("check." + name, "would_change")
            parsed, rule = ask.parse_selection(json.dumps(value), projection, "what_changes")
            self.assertIsNone(rule, key)
            sentence = ask.render_answer(parsed, projection)["statements"][0]["text"]
            self.assertEqual(sentence, ask.TEMPLATES[key].format(name=name.replace("_", " ")))
            self.assertEqual(len(re.findall(r"[.!?](?:\s|$)", sentence)), 1)
            self.assertIn({"pass": "records no signal", "signal": "records a signal",
                           "unknown": "leaves the check unresolved"}[status], sentence)
            self.assertIsNotNone(ask.parse_selection(json.dumps(value), projection, "why_verdict")[1])

    def test_storage_templates_claim_only_the_null_test(self):
        from gate import rules
        from intel import labels

        rpc = mock.Mock()
        rpc.last_source = {}
        rpc.view_account.return_value = {}
        request = {"kind": "ft", "token_id": "token.near", "receiver_id": "example.near", "memo": None}
        for balance in (None, {}, False, 0, "", {"total": "1"}):
            with self.subTest(balance=balance):
                rpc.call_function.side_effect = lambda token, method, args: (
                    {"name": "Token", "symbol": "T", "decimals": 6} if method == "ft_metadata" else balance)
                checks = rules._tier1(request, {}, labels.Store([], ""), rpc, None, None, None)[0]
                check = next(row for row in checks if row["id"] == "storage_registered")
                self.assertEqual(check["status"], "signal" if balance is None else "pass")
                projection = {"items": [{"id": "check.storage_registered", "status": check["status"],
                                         "detail": check["detail"]}]}
                for aspect in ("read", "would_change"):
                    sentence = ask.render_answer(selection("check.storage_registered", aspect),
                                                 projection)["statements"][0]["text"]
                    self.assertNotIn("registration", sentence)
                    if aspect == "would_change":
                        self.assertIn("storage_balance_of returns a non null result or null", sentence)

    def test_status_templates_do_not_claim_reads_ran_or_human_review_failed(self):
        from gate import rules
        from intel import reports

        check = rules._memo_check(None, None)
        projection = {"items": [{"id": "check.memo_injection", "status": check["status"], "detail": check["detail"]}]}
        sentence = ask.render_answer(selection("check.memo_injection"), projection)["statements"][0]["text"]
        self.assertEqual(sentence, "The evidence records no signal for the memo injection check.")
        summary = {"accepted": 1, "pending": 0, "contested": 0, "latest_accepted_id": 1,
                   "latest_deny_id": None, "deny": False}
        self.assertEqual(reports.effect(summary, False)["effect"], "ask")
        projection = {"items": [{"id": "check.community_report", "status": "unknown", "detail": "A human decides."}]}
        sentence = ask.render_answer(selection("check.community_report", "unknown"), projection)["statements"][0]["text"]
        self.assertEqual(sentence, "The evidence leaves the check unresolved for community report.")
        self.assertIn("unresolved", ask.UNKNOWN_NOTICE)


class SelectionValidationTests(ServiceCase):
    def projection(self):
        return ask.project(SERVICE_SUBJECT, SERVICE_VERDICT, self.store)

    def test_invalid_selections_are_unavailable_and_never_cached(self):
        base = selection()
        invalid = [selection("check.missing"), {**base, "points": base["points"] * 2},
                   selection("subject", "unknown"), selection("subject", "would_change"),
                   selection("check.no_sentence", "would_change"), {**base, "extra": 1},
                   {**base, "answerable": 1}, {**base, "points": True}, {**base, "gaps": True},
                   {**base, "points": "text"}, {**base, "gaps": "future_state"},
                   {**base, "points": [{"item_id": True, "aspect": "read"}]},
                   {**base, "points": [{"item_id": "subject", "aspect": True}]},
                   {**base, "points": [{"item_id": "subject", "aspect": "read", "extra": 1}]},
                   {**base, "points": [{"item_id": "subject"}]}, selection("subject", "other"),
                   {**base, "points": [{"item_id": "reason." + str(n), "aspect": "read"}
                                          for n in range(ask.MAX_STATEMENTS + 1)]},
                   {**base, "gaps": ["future_state"] * (ask.MAX_GAPS + 1)},
                   {**base, "gaps": ["missing_code"]}, {**base, "gaps": [True]},
                   {**base, "answerable": False}, [], None]
        invalid += [selection("check.receiver_exists", "unknown"), selection("check.memo_injection", "unknown")]
        verdict = copy.deepcopy(SERVICE_VERDICT)
        verdict["checks"] += [{"name": "receiver_exists", "status": "pass", "detail": "Recorded."},
                              {"name": "memo_injection", "status": "signal", "detail": "Recorded."}]
        verdict["checks"].append({"name": "no_sentence", "status": "pass", "detail": "Recorded."})
        verdict["reasons"] += ["Recorded reason." for _ in range(ask.MAX_STATEMENTS + 1)]
        self.read.return_value = (200, verdict, {})
        for value in invalid:
            with self.subTest(value=value), tempfile.TemporaryDirectory() as directory:
                service = ask.AskService(directory, self.store, self.read, client_factory=self.factory)
                self.client.create.return_value["text"] = json.dumps(value)
                before = self.client.create.call_count
                self.unavailable("invalid_output", self.answer(service, question_id="what_changes"))
                self.assertEqual(self.client.create.call_count - before, 1)
                with closing(sqlite3.connect(service.ledger.path)) as connection:
                    self.assertEqual(connection.execute("SELECT COUNT(*) FROM answers").fetchone()[0], 0)

    def test_unknown_aspect_on_actual_known_pass_and_signal_checks_is_invalid(self):
        projection = {"items": [{"id": "check.receiver_exists", "status": "pass", "detail": "Recorded."},
                               {"id": "check.memo_injection", "status": "signal", "detail": "Recorded."}]}
        for item in projection["items"]:
            self.assertIsNotNone(ask.parse_selection(json.dumps(selection(item["id"], "unknown")), projection, "what_unknown")[1])

    def test_unanswerable_requires_zero_points_and_renders_only_fixed_gaps(self):
        value = {"answerable": False, "points": [], "gaps": ["not_covered_by_checks"]}
        self.assertEqual(ask.parse_selection(json.dumps(value), self.projection(), "why_verdict"), (value, None))
        result = ask.render_answer(value, self.projection())
        self.assertFalse(result["answerable"])
        self.assertEqual(result["statements"], [])
        self.assertEqual(result["not_in_evidence"], [ask.TEMPLATES["gap.not_covered_by_checks"]])

    def test_uncited_unknown_check_keeps_one_fixed_notice(self):
        verdict = copy.deepcopy(SERVICE_VERDICT)
        verdict["checks"].append({"name": "receiver_exists", "status": "pass", "detail": "Recorded."})
        self.read.return_value = (200, verdict, {})
        self.client.create.return_value["text"] = json.dumps(selection("check.receiver_exists"))
        response = self.answer()[1]
        self.assertEqual(response["status"], "explained")
        self.assertEqual(response["unknown_notice"], ask.UNKNOWN_NOTICE)
        self.assertEqual(response["unknown_checks"], ["account_age"])
        self.assertEqual(json.dumps(response).count(ask.UNKNOWN_NOTICE), 1)
        verdict["outcome"] = "ALLOW"
        self.read.return_value = (200, verdict, {})
        response = self.answer()[1]
        self.assertEqual(response["unknown_notice"], ask.UNKNOWN_NOTICE)
        self.assertEqual(response["unknown_checks"], ["account_age"])

    def test_one_uncached_call_and_single_call_reservation_bound(self):
        self.client.create.return_value["text"] = json.dumps(selection())
        service = self.service()
        bound = ask.reservation_bound(ask.build_request(service.model, service.effort, self.projection(),
                                                       ask.QUESTIONS["why_verdict"]))
        with mock.patch.object(service.ledger, "reserve", wraps=service.ledger.reserve) as reserve:
            response = self.answer(service)[1]
        self.assertEqual(response["status"], "explained")
        self.client.create.assert_called_once()
        reserve.assert_called_once()
        self.assertEqual(reserve.call_args.args[3:5], bound)

    def test_cache_hit_preserves_original_generation_time(self):
        self.client.create.return_value["text"] = json.dumps(selection())
        service = self.service()
        first = self.answer(service)[1]
        self.assertEqual(first["status"], "explained")
        self.assertIsNotNone(first["generated_at"])
        self.now += 60
        cached = self.answer(service)[1]
        self.assertTrue(cached["cached"])
        self.assertEqual(cached["generated_at"], first["generated_at"])
        self.client.create.assert_called_once()


if __name__ == "__main__":
    unittest.main()
