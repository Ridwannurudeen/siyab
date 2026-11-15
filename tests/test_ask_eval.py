import ast
import contextlib
import io
import json
import os
import socket
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from scripts import ask_eval
from tests.test_ask import SERVICE_VALUES, ServiceCase

ROOT = Path(__file__).resolve().parent.parent


def current_row(index=0):
    sample = ROOT / "fixtures/ask/replay-sample.jsonl"
    row = json.loads(sample.read_text(encoding="utf-8").splitlines()[index])
    row.update(prompt_version=ask_eval.ask.PROMPT_VERSION, schema_version=ask_eval.ask.SCHEMA_VERSION,
               text=json.dumps({"answerable": True, "points": [{"item_id": "subject", "aspect": "read"}], "gaps": []}))
    return row


class AskEvalTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.items = ask_eval.corpus()

    def setUp(self):
        self.connect = mock.patch.object(socket.socket, "connect", side_effect=AssertionError("network"))
        self.connect.start()
        self.addCleanup(self.connect.stop)

    def test_corpus_counts(self):
        self.assertEqual(ask_eval.counts(self.items),
                         {"judgement": 336, "evidence": 48, "unknown": 48, "adversarial": 22, "free": 8})
        self.assertEqual(len(self.items), 462)

    def test_unknown_items_are_committed_unknown_judgements(self):
        files = sorted(p for p in (ROOT / "fixtures/receivers/top/judgements").glob("*.json")
                       if json.loads(p.read_text(encoding="utf-8"))["verdict"] == "UNKNOWN")[:12]
        items = [i for i in self.items if i["item"].startswith("unknown:")]
        self.assertEqual({i["item"].split(":")[1] for i in items}, {p.stem for p in files})
        self.assertTrue(all(i["projection"]["outcome"] == "ASK" for i in items))

    def test_zero_check_evidence_document_answers_evidence_empty(self):
        path = next(p for p in sorted((ROOT / "fixtures/evidence").glob("*.json"))
                    if not json.loads(p.read_text(encoding="utf-8"))["checks"])
        item = ask_eval.from_document(path, "evidence", None)
        self.assertEqual(item["expect"]["reason"], "evidence_empty")
        factory = mock.Mock(side_effect=AssertionError("model call"))
        with tempfile.TemporaryDirectory() as directory, mock.patch.dict(os.environ, {"SIYAB_ASK": "1"}):
            service = ask_eval.ask.AskService(directory, ask_eval.labels.Store([], ""),
                mock.Mock(return_value=(200, item["verdict"], {})), client_factory=factory)
            _, result, _ = service.answer({**item["subject"], "question_id": "why_verdict"},
                user_id="reader", team_id="team", reserved=False, peer="local", headers={})
        self.assertEqual(result["reason"], "evidence_empty")
        self.assertEqual(result["statements"], [])
        factory.assert_not_called()
        self.assertFalse(any(i["item"].split(":")[1] == path.stem for i in self.items))

    def test_adversarial_planted_fields_are_withheld(self):
        for item in self.items:
            if not item["item"].startswith("adversarial:"):
                continue
            with self.subTest(item=item["item"]):
                self.assertTrue(ask_eval.injection_projection_passes(item))

    def test_adversarial_note_cases_preserve_existing_labels(self):
        store = ask_eval.labels.load()
        for name in ("label_note", "tag_break"):
            item = next(item for item in self.items if item["item"] == f"adversarial:{name}:why_verdict")
            baseline = ask_eval.from_document(ROOT / item["expect"]["source"], "judgement", store)
            original = [{key: value for key, value in row.items() if key != "id"}
                        for row in baseline["projection"]["items"] if row["id"].startswith("label.")]
            projected = [row for row in item["projection"]["items"] if row["id"].startswith("label.")]
            with self.subTest(case=name):
                self.assertEqual(len(projected), len(original) + 1)
                for row in original:
                    self.assertIn(row, [{key: value for key, value in row.items() if key != "id"}
                                        for row in projected])
                planted = next(row for row in projected if row["entity"] == "Recorded assertion"
                               and row["label"] == "Recorded label")
                if name == "label_note":
                    self.assertTrue(planted["note"].startswith("[withheld:"))
                else:
                    self.assertEqual(planted["note"], item["expect"]["edits"]["note"])
                self.assertTrue(ask_eval.injection_projection_passes(item))

    def test_replay_sample_requires_current_selection_versions(self):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            result = ask_eval.replay(ROOT / "fixtures/ask/replay-sample.jsonl", self.items)
        self.assertEqual(result, 1)
        self.assertIn("stale_version", output.getvalue())
        self.assertIn("0 passed; 8 failed", output.getvalue())

    def test_replay_refuses_a_stale_projection_hash(self):
        line = current_row()
        line["projection_sha256"] = "00" * 32
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "run.jsonl"
            path.write_text(json.dumps(line) + "\n", encoding="utf-8")
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                self.assertEqual(ask_eval.replay(path, self.items), 1)
            self.assertIn("stale_projection", output.getvalue())

    def test_replay_requires_exact_corpus_coverage_per_run(self):
        row = current_row()
        item = next(item for item in self.items if item["item"] == row["item"])
        other = next(item for item in self.items if item["item"] != row["item"])
        for rows, items in (([row], [item, other]), ([row, row], [item]),
                            ([row, {**row, "run": 2}], [item, other])):
            with self.subTest(rows=len(rows), items=len(items)), tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "run.jsonl"
                path.write_text("".join(json.dumps(value) + "\n" for value in rows), encoding="utf-8")
                with contextlib.redirect_stdout(io.StringIO()):
                    self.assertEqual(ask_eval.replay(path, items), 1)

    def test_replay_accepts_complete_runs_with_declared_models(self):
        row = current_row()
        item = next(item for item in self.items if item["item"] == row["item"])
        rows = [row, {**row, "run": 2, "model": list(ask_eval.ask.PRICES)[1]}]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "run.jsonl"
            path.write_text("".join(json.dumps(value) + "\n" for value in rows), encoding="utf-8")
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(ask_eval.replay(path, [item]), 0)

    def test_replay_requires_completed_answer_stop_reason(self):
        row = current_row()
        item = next(item for item in self.items if item["item"] == row["item"])
        for stop_reason in ("end_turn", "tool_use", None, 17):
            with self.subTest(stop_reason=stop_reason), tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "run.jsonl"
                path.write_text(json.dumps({**row, "stop_reason": stop_reason}) + "\n", encoding="utf-8")
                output = io.StringIO()
                with contextlib.redirect_stdout(output):
                    self.assertEqual(ask_eval.replay(path, [item]), int(stop_reason != "end_turn"))
                self.assertIn("1 passed; 0 failed" if stop_reason == "end_turn" else "0 passed; 1 failed",
                              output.getvalue())
                if stop_reason != "end_turn":
                    self.assertIn("invalid_output", output.getvalue())

    def test_replay_preserves_refusal_and_max_tokens_behavior(self):
        row = current_row()
        item = next(item for item in self.items if item["item"] == row["item"])
        for stop_reason in ("refusal", "max_tokens"):
            with self.subTest(stop_reason=stop_reason), tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "run.jsonl"
                path.write_text(json.dumps({**row, "text": None, "stop_reason": stop_reason}) + "\n", encoding="utf-8")
                output = io.StringIO()
                with contextlib.redirect_stdout(output):
                    self.assertEqual(ask_eval.replay(path, [item]), 1)
                self.assertIn(f"line 1: {stop_reason}", output.getvalue())
                self.assertIn("0 passed; 1 failed; 0 incomplete; 1 refusal or max_tokens", output.getvalue())
                self.assertNotIn("invalid_output", output.getvalue())

    def test_replay_rejects_usage_outside_reconstructed_request(self):
        row = current_row()
        item = next(item for item in self.items if item["item"] == row["item"])
        usage = dict(row["usage"])
        row["answer_usage"] = usage
        params = ask_eval.ask.build_request(row["model"], row["effort"], item["projection"], item["question"])
        cases = [(name, {**usage, "output_tokens": 6000000}) for name in ("usage", "answer_usage")]
        cases += [(name, {**usage, "input_tokens": len(ask_eval.ask.projection_bytes(params)) + 4097})
                  for name in ("usage", "answer_usage")]
        for name, invalid in cases:
            with self.subTest(name=name), tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "run.jsonl"
                path.write_text(json.dumps({**row, name: invalid}) + "\n", encoding="utf-8")
                output = io.StringIO()
                with contextlib.redirect_stdout(output):
                    self.assertEqual(ask_eval.replay(path, [item]), 1)
                self.assertIn("invalid_usage", output.getvalue())

    def test_replay_requires_current_versions_and_consistent_model(self):
        row = current_row()
        item = next(item for item in self.items if item["item"] == row["item"])
        other = next(item for item in self.items if item["item"] != row["item"])
        second = {**row, "item": other["item"],
                  "projection_sha256": ask_eval.hashlib.sha256(ask_eval.ask.projection_bytes(other["projection"])).hexdigest()}
        cases = [[{**row, key: "stale"}] for key in ("prompt_version", "schema_version", "model")]
        cases.append([row, {**second, "model": list(ask_eval.ask.PRICES)[1]}])
        for rows in cases:
            with self.subTest(rows=rows), tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "run.jsonl"
                path.write_text("".join(json.dumps(value) + "\n" for value in rows), encoding="utf-8")
                with contextlib.redirect_stdout(io.StringIO()):
                    self.assertEqual(ask_eval.replay(path, [item] if len(rows) == 1 else [item, other]), 1)

    def test_grader_rejects_free_prose_output(self):
        for status in ("unknown", "signal"):
            item = {"item": "review", "question_id": "why_verdict", "projection": {
                "outcome": "ASK", "items": [{"id": "check.account_age", "status": status}]}}
            value = {"answerable": True, "statements": [{"text": "The check passed.",
                     "evidence_ids": ["check.account_age"]}], "not_in_evidence": []}
            result, rule = ask_eval.graders(item, json.dumps(value))
            self.assertIsNotNone(rule)
            self.assertFalse(result["validated"])

    def test_grader_rejects_every_unmapped_rule(self):
        item = {"item": "review", "question_id": "explain_labels", "projection": {
            "outcome": "DENY", "items": [{"id": "label.1", "entity": "Recorded name"}]}}
        value = {"answerable": True, "points": [{"item_id": "label.1", "aspect": "read"}], "gaps": []}
        with mock.patch.object(ask_eval.ask, "parse_selection", return_value=(None, "new_rule")):
            result, rule = ask_eval.graders(item, json.dumps(value))
        self.assertEqual(rule, "new_rule")
        self.assertFalse(all(result.values()))
        result, rule = ask_eval.graders(item, json.dumps(value))
        self.assertIsNone(rule)
        self.assertTrue(all(result.values()))

    def test_launch_records_pass_replay(self):
        path = ROOT / "fixtures/ask/launch.json"
        if not path.exists():
            self.skipTest("launch records have not been selected")
        launch = json.loads(path.read_text(encoding="utf-8"))
        for name in launch["runs"]:
            with self.subTest(name=name), contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(ask_eval.replay(ROOT / "fixtures/runs" / name, self.items), 0)

    def test_harness_imports_no_anthropic_at_module_level(self):
        tree = ast.parse((ROOT / "scripts/ask_eval.py").read_text(encoding="utf-8"))
        for node in tree.body:
            if isinstance(node, ast.Import):
                self.assertNotIn("anthropic", [alias.name for alias in node.names])
            if isinstance(node, ast.ImportFrom):
                self.assertNotEqual(node.module, "anthropic")


class FailureCapTests(ServiceCase):
    def test_provider_failure_at_spend_cap_retains_one_reservation_and_its_reason(self):
        service = self.service()
        verdict = self.read.return_value[1]
        projection = ask_eval.ask.project({"network": "mainnet", **{
            key: SERVICE_VALUES[key] for key in ("receiver", "amount", "token")}}, verdict, self.store)
        params = ask_eval.ask.build_request(service.model, service.effort, projection,
                                            ask_eval.ask.QUESTIONS["why_verdict"])
        tokens, micro = ask_eval.ask.reservation_bound(params)
        service.ledger.daily_cap = micro
        self.client.create.side_effect = ask_eval.ask.ProviderStatus(503)
        body = self.unavailable("provider_error", self.answer(service))
        self.assertEqual(self.client.create.call_count, 1)
        self.assertEqual(self.count("calls"), 1)
        self.assertEqual(self.count("spend_micro"), micro)
        self.assertEqual(self.count("tokens:user:user"), tokens)
        self.assertIsNone(body["retry_after_s"])
        self.assertEqual(body["outcome"], verdict["outcome"])
        self.assertEqual(body["evidence_hash"], verdict["evidence_hash"])
        self.assertFalse(body["cached"])


class RecordTests(unittest.TestCase):
    def test_record_selection_uses_one_call_and_single_reservation(self):
        item = ask_eval.corpus()[0]
        usage = {"input_tokens": 10, "output_tokens": 5,
                 "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0}
        selection = {"answerable": True, "points": [{"item_id": "subject", "aspect": "read"}], "gaps": []}
        client = mock.Mock()
        client.create.return_value = {"text": json.dumps(selection), "stop_reason": "end_turn", "usage": usage}
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "run.jsonl"
            path.write_text("", encoding="utf-8")
            model = next(iter(ask_eval.ask.PRICES))
            params = ask_eval.ask.build_request(model, "low", item["projection"], item["question"])
            args = ask_eval.argparse.Namespace(model=model, effort="low", runs=1,
                max_usd=ask_eval.ask.reservation_bound(params)[1] / 1000000, resume=str(path), yes=True)
            with (mock.patch.object(ask_eval, "corpus", return_value=[item]),
                  mock.patch.object(ask_eval, "_key", return_value="sentinel"),
                  contextlib.redirect_stdout(io.StringIO())):
                self.assertEqual(ask_eval.record(args, client_factory=lambda key: client), 0)
            client.create.assert_called_once()
            row = json.loads(path.read_text(encoding="utf-8"))
            self.assertNotIn("check_usage", row)
            self.assertEqual(row["usage"]["reserved_micro"], 0)

    def test_resume_rejects_invalid_reservation_before_client(self):
        item = ask_eval.corpus()[0]
        model = next(iter(ask_eval.ask.PRICES))
        for reserved in (-10000, True):
            with self.subTest(reserved=reserved), tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "run.jsonl"
                row = {"item": item["item"], "run": 1, "model": model, "effort": "low",
                    "projection_sha256": ask_eval.hashlib.sha256(ask_eval.ask.projection_bytes(item["projection"])).hexdigest(),
                    "text": None, "stop_reason": None, "usage": {"input_tokens": 0, "output_tokens": 0,
                    "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0, "reserved_micro": reserved}}
                path.write_text(json.dumps(row) + "\n", encoding="utf-8")
                args = ask_eval.argparse.Namespace(model=model, effort="low", runs=1,
                    max_usd=1, resume=str(path), yes=True)
                with (mock.patch.object(ask_eval, "corpus", return_value=[item]),
                      mock.patch.object(ask_eval, "_bound", side_effect=AssertionError("cost math")),
                      mock.patch.object(ask_eval, "_key", side_effect=AssertionError("key read"))):
                    with self.assertRaisesRegex(ValueError, "invalid_reservation"):
                        ask_eval.record(args)

    def test_resume_preserves_completed_current_selection(self):
        sample = current_row()
        item = next(item for item in ask_eval.corpus() if item["item"] == sample["item"])
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "run.jsonl"
            path.write_text(json.dumps(sample) + "\n", encoding="utf-8")
            args = ask_eval.argparse.Namespace(model=sample["model"], effort=sample["effort"], runs=1,
                max_usd=1, resume=str(path), yes=True)
            client = mock.Mock()
            with (mock.patch.object(ask_eval, "corpus", return_value=[item]),
                  mock.patch.object(ask_eval, "_key", return_value="sentinel"),
                  contextlib.redirect_stdout(io.StringIO())):
                self.assertEqual(ask_eval.record(args, client_factory=lambda key: client), 0)
            client.create.assert_not_called()
            self.assertEqual(json.loads(path.read_text(encoding="utf-8")), sample)

    def test_record_expired_call_settles_usage_and_returns_timeout(self):
        sample = current_row()
        item = next(item for item in ask_eval.corpus() if item["item"] == sample["item"])
        usage = {"input_tokens": 10, "output_tokens": 5, "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0}
        client = mock.Mock()
        client.create.return_value = {"text": sample["text"], "stop_reason": "end_turn", "usage": usage}
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "run.jsonl"
            path.write_text("", encoding="utf-8")
            args = ask_eval.argparse.Namespace(model=sample["model"], effort="low", runs=1,
                max_usd=1, resume=str(path), yes=True)
            with (mock.patch.object(ask_eval, "corpus", return_value=[item]),
                  mock.patch.object(ask_eval, "_key", return_value="sentinel"),
                  contextlib.redirect_stdout(io.StringIO())):
                self.assertEqual(ask_eval.record(args, client_factory=lambda key: client,
                    clock=mock.Mock(side_effect=[0, 0, 0, ask_eval.ask.TOTAL_DEADLINE_S + 1])), 1)
            client.create.assert_called_once()
            row = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(row["reason"], "timeout")
            self.assertEqual(row["usage"]["reserved_micro"], 0)
            self.assertEqual(row["billed_micro"], ask_eval._cost(usage, ask_eval.ask.PRICES[args.model]))

    def test_resume_preserves_actual_charges_until_cap_stop(self):
        item = ask_eval.corpus()[0]
        model = next(iter(ask_eval.ask.PRICES))
        client = mock.Mock()
        client.create.return_value = {"text": None, "stop_reason": "end_turn", "usage": {
            "input_tokens": 10000, "output_tokens": 0, "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0}}
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "run.jsonl"
            path.write_text("", encoding="utf-8")
            args = ask_eval.argparse.Namespace(model=model, effort="low", runs=1,
                max_usd=0.003012, resume=str(path), yes=True)
            with (mock.patch.object(ask_eval, "corpus", return_value=[item]),
                  mock.patch.object(ask_eval, "_bound", return_value=2012),
                  mock.patch.object(ask_eval, "_key", return_value="sentinel"),
                  contextlib.redirect_stdout(io.StringIO())):
                for _ in range(4):
                    self.assertEqual(ask_eval.record(args, client_factory=lambda key: client), 1)
            self.assertEqual(client.create.call_count, 2)
            row = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(row["billed_micro"], 2000)
            self.assertEqual(row["usage"]["input_tokens"], 20000)

    def test_record_excess_usage_is_withheld_and_keeps_attempt_reservation(self):
        sample = current_row()
        item = next(item for item in ask_eval.corpus() if item["item"] == sample["item"])
        usage = {"input_tokens": 100, "output_tokens": 5, "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0}
        params = ask_eval.ask.build_request(sample["model"], "low", item["projection"], item["question"])
        cases = [None, {}, {"input_tokens": 100}, {**usage, "output_tokens": 6000000},
                 {**usage, "output_tokens": -1}, {**usage, "output_tokens": True}]
        for invalid in cases:
            with self.subTest(usage=invalid), tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "run.jsonl"
                path.write_text("", encoding="utf-8")
                args = ask_eval.argparse.Namespace(model=sample["model"], effort="low", runs=1,
                    max_usd=3, resume=str(path), yes=True)
                client = mock.Mock()
                client.create.return_value = {"text": sample["text"], "stop_reason": "end_turn", "usage": invalid}
                with (mock.patch.object(ask_eval, "corpus", return_value=[item]),
                      mock.patch.object(ask_eval, "_key", return_value="sentinel"),
                      contextlib.redirect_stdout(io.StringIO())):
                    self.assertEqual(ask_eval.record(args, client_factory=lambda key: client), 1)
                client.create.assert_called_once()
                row = json.loads(path.read_text(encoding="utf-8"))
                self.assertIsNone(row["text"])
                self.assertEqual(row["reason"], "invalid_output")
                self.assertEqual(row["usage"]["reserved_micro"], ask_eval.ask.reservation_bound(params)[1])
                self.assertEqual(row["billed_micro"], 0)

    def test_record_bound_matches_shared_reservation(self):
        item = ask_eval.corpus()[0]
        model = next(iter(ask_eval.ask.PRICES))
        params = ask_eval.ask.build_request(model, "low", item["projection"], item["question"])
        self.assertEqual(ask_eval._bound(params, ask_eval.ask.PRICES[model]), ask_eval.ask.reservation_bound(params)[1])

    def test_record_withholds_validated_answer_without_answer_usage(self):
        sample = current_row()
        item = next(item for item in ask_eval.corpus() if item["item"] == sample["item"])
        usage = {"input_tokens": 10, "output_tokens": 5, "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0}
        client = mock.Mock()
        client.create.return_value = {"text": sample["text"], "stop_reason": "end_turn"}
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "run.jsonl"
            path.write_text("", encoding="utf-8")
            args = ask_eval.argparse.Namespace(model=sample["model"], effort="low", runs=1,
                max_usd=1, resume=str(path), yes=True)
            with (mock.patch.object(ask_eval, "corpus", return_value=[item]),
                  mock.patch.object(ask_eval, "_key", return_value="sentinel"),
                  contextlib.redirect_stdout(io.StringIO())):
                self.assertEqual(ask_eval.record(args, client_factory=lambda key: client), 1)
            self.assertEqual(client.create.call_count, 1)
            row = json.loads(path.read_text(encoding="utf-8"))
            self.assertIsNone(row["text"])
            self.assertEqual(row["reason"], "invalid_output")
            self.assertGreater(row["usage"]["reserved_micro"], 0)

    def test_record_rechecks_deadline_after_each_reservation_save(self):
        sample = current_row()
        item = next(item for item in ask_eval.corpus() if item["item"] == sample["item"])
        usage = {"input_tokens": 10, "output_tokens": 5, "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0}
        for slow_save in (1,):
            with self.subTest(slow_save=slow_save), tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "run.jsonl"
                prior = {**sample, "text": None, "stop_reason": None, "effort": "low", "billed_micro": 7,
                    "usage": {"input_tokens": 0, "output_tokens": 0, "cache_creation_input_tokens": 0,
                              "cache_read_input_tokens": 0, "reserved_micro": 11}}
                path.write_text(json.dumps(prior) + "\n", encoding="utf-8")
                args = ask_eval.argparse.Namespace(model=sample["model"], effort="low", runs=1,
                    max_usd=1, resume=str(path), yes=True)
                client = mock.Mock()
                client.create.return_value = {"text": sample["text"], "stop_reason": "end_turn", "usage": usage}
                now = [0]
                saves = []
                save_rows = ask_eval._save_rows
                def persist(destination, rows):
                    save_rows(destination, rows)
                    saves.append(1)
                    if len(saves) == slow_save:
                        now[0] = ask_eval.ask.TOTAL_DEADLINE_S - 1
                with (mock.patch.object(ask_eval, "corpus", return_value=[item]),
                      mock.patch.object(ask_eval, "_save_rows", side_effect=persist),
                      mock.patch.object(ask_eval, "_key", return_value="sentinel"),
                      contextlib.redirect_stdout(io.StringIO())):
                    self.assertEqual(ask_eval.record(args, client_factory=lambda key: client, clock=lambda: now[0]), 1)
                self.assertEqual(client.create.call_count, slow_save - 1)
                row = json.loads(path.read_text(encoding="utf-8"))
                self.assertEqual(row["reason"], "timeout")
                self.assertEqual(row["usage"]["reserved_micro"], 11)
                self.assertEqual(row["billed_micro"], 7 + (slow_save - 1) * ask_eval._cost(usage, ask_eval.ask.PRICES[args.model]))

    def test_record_requires_yes_without_key_or_client(self):
        args = ask_eval.argparse.Namespace(model=next(iter(ask_eval.ask.PRICES)), effort="low", runs=1,
                                          max_usd=1, resume=None, yes=False)
        with mock.patch.object(ask_eval, "_key", side_effect=AssertionError("key read")), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(ask_eval.record(args), 2)

    def test_judge_requires_yes_without_key_or_client(self):
        row = json.dumps(current_row())
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "run.jsonl"
            path.write_text(row + "\n", encoding="utf-8")
            args = ask_eval.argparse.Namespace(path=str(path), run=1, yes=False, max_usd=1, batch=None)
            with mock.patch.object(ask_eval, "_key", side_effect=AssertionError("key read")), contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(ask_eval.judge(args), 2)

    def test_resume_refuses_stale_projection_before_key_read(self):
        row = current_row()
        row["projection_sha256"] = "00" * 32
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "run.jsonl"
            path.write_text(json.dumps(row) + "\n", encoding="utf-8")
            args = ask_eval.argparse.Namespace(model=row["model"], effort="low", runs=1,
                                              max_usd=1, resume=str(path), yes=True)
            with mock.patch.object(ask_eval, "_key", side_effect=AssertionError("key read")):
                with self.assertRaisesRegex(ValueError, "stale_projection"):
                    ask_eval.record(args)

    def test_unknown_billing_is_preserved_in_record_cost(self):
        usage = {"input_tokens": 0, "output_tokens": 0, "reserved_micro": 123}
        self.assertEqual(ask_eval._cost(usage, (100000, 500000)), 123)

    def test_record_invalid_usage_keeps_reservation_and_spend_cap(self):
        items = ask_eval.corpus()[:3]
        valid = {"input_tokens": 0, "output_tokens": 0,
                 "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0}
        cases = [None, {}, {"input_tokens": 0},
                 *({**valid, name: value} for name in valid for value in (-1, True))]
        for usage in cases:
            with self.subTest(usage=usage), tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "run.jsonl"
                path.write_text("", encoding="utf-8")
                model = next(iter(ask_eval.ask.PRICES))
                params = ask_eval.ask.build_request(model, "low", items[0]["projection"], items[0]["question"])
                reservation = ask_eval._bound(params, ask_eval.ask.PRICES[model])
                args = ask_eval.argparse.Namespace(model=model, effort="low", runs=1,
                    max_usd=ask_eval._bound(params, ask_eval.ask.PRICES[model]) / 1000000, resume=str(path), yes=True)
                response = {"text": None, "stop_reason": "refusal"}
                if usage is not None:
                    response["usage"] = usage
                client = mock.Mock()
                client.create.return_value = response
                output = io.StringIO()
                with (mock.patch.object(ask_eval, "corpus", return_value=items),
                      mock.patch.object(ask_eval, "_key", return_value="sentinel"),
                      contextlib.redirect_stdout(output)):
                    self.assertEqual(ask_eval.record(args, client_factory=lambda key: client), 1)
                self.assertEqual(client.create.call_count, 1)
                rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
                self.assertEqual(rows[0]["usage"]["reserved_micro"], reservation)
                self.assertIn("spend_cap", output.getvalue())

    def test_resume_invalid_usage_keeps_reservation_and_spend_cap(self):
        items = ask_eval.corpus()[:2]
        valid = {"input_tokens": 0, "output_tokens": 0,
                 "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0}
        cases = [None, {}, {"input_tokens": 0},
                 *({**valid, name: value} for name in valid for value in (-1, True))]
        for usage in cases:
            for completed in (False, True):
                with self.subTest(usage=usage, completed=completed), tempfile.TemporaryDirectory() as directory:
                    path = Path(directory) / "run.jsonl"
                    model = next(iter(ask_eval.ask.PRICES))
                    params = ask_eval.ask.build_request(model, "low", items[0]["projection"], items[0]["question"])
                    reservation = ask_eval._bound(params, ask_eval.ask.PRICES[model])
                    row = {"item": items[0]["item"], "run": 1, "model": model, "effort": "low",
                        "projection_sha256": ask_eval.hashlib.sha256(ask_eval.ask.projection_bytes(items[0]["projection"])).hexdigest(),
                        "text": None, "stop_reason": "refusal" if completed else None}
                    if usage is not None:
                        row["usage"] = {**usage, "reserved_micro": 0}
                    path.write_text(json.dumps(row) + "\n", encoding="utf-8")
                    args = ask_eval.argparse.Namespace(model=model, effort="low", runs=1,
                        max_usd=ask_eval._bound(params, ask_eval.ask.PRICES[model]) / 1000000, resume=str(path), yes=True)
                    client = mock.Mock()
                    client.create.return_value = {"text": None, "stop_reason": "refusal", "usage": valid}
                    output = io.StringIO()
                    with (mock.patch.object(ask_eval, "corpus", return_value=items),
                          mock.patch.object(ask_eval, "_key", return_value="sentinel"),
                          contextlib.redirect_stdout(output)):
                        self.assertEqual(ask_eval.record(args, client_factory=lambda key: client), 1)
                    client.create.assert_not_called()
                    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
                    self.assertEqual(rows[0]["usage"]["reserved_micro"], reservation)
                    self.assertIn("spend_cap", output.getvalue())

    def test_record_complete_usage_releases_reservation(self):
        items = ask_eval.corpus()[:2]
        model = next(iter(ask_eval.ask.PRICES))
        params = ask_eval.ask.build_request(model, "low", items[0]["projection"], items[0]["question"])
        reservation = ask_eval._bound(params, ask_eval.ask.PRICES[model])
        client = mock.Mock()
        client.create.return_value = {"text": None, "stop_reason": "refusal", "usage": {
            "input_tokens": 0, "output_tokens": 0, "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0}}
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "run.jsonl"
            path.write_text("", encoding="utf-8")
            args = ask_eval.argparse.Namespace(model=model, effort="low", runs=1,
                max_usd=ask_eval._bound(params, ask_eval.ask.PRICES[model]) / 1000000, resume=str(path), yes=True)
            with (mock.patch.object(ask_eval, "corpus", return_value=items),
                  mock.patch.object(ask_eval, "_key", return_value="sentinel"),
                  contextlib.redirect_stdout(io.StringIO())):
                self.assertEqual(ask_eval.record(args, client_factory=lambda key: client), 1)
            self.assertEqual(client.create.call_count, 2)
            rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
            self.assertTrue(all(row["usage"]["reserved_micro"] == 0 for row in rows))

    def test_record_persists_reservation_before_call(self):
        item = ask_eval.corpus()[0]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "run.jsonl"
            path.write_text("", encoding="utf-8")
            args = ask_eval.argparse.Namespace(model=next(iter(ask_eval.ask.PRICES)), effort="low", runs=1,
                                              max_usd=1, resume=str(path), yes=True)
            client = mock.Mock()
            def interrupt(params, timeout):
                row = json.loads(path.read_text(encoding="utf-8"))
                self.assertGreater(row["usage"]["reserved_micro"], 0)
                raise KeyboardInterrupt()
            client.create.side_effect = interrupt
            with mock.patch.object(ask_eval, "corpus", return_value=[item]), mock.patch.object(ask_eval, "_key", return_value="sentinel"):
                with self.assertRaises(KeyboardInterrupt):
                    ask_eval.record(args, client_factory=lambda key: client)

    def test_record_bad_request_is_not_retried_and_has_no_output(self):
        item = ask_eval.corpus()[0]
        client = mock.Mock()
        client.create.side_effect = ask_eval.ask.ProviderBadRequest()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "run.jsonl"
            path.write_text("", encoding="utf-8")
            args = ask_eval.argparse.Namespace(model=next(iter(ask_eval.ask.PRICES)), effort="low", runs=1,
                                              max_usd=1, resume=str(path), yes=True)
            with mock.patch.object(ask_eval, "corpus", return_value=[item]), mock.patch.object(ask_eval, "_key", return_value="sentinel"), contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(ask_eval.record(args, client_factory=lambda key: client), 1)
            self.assertEqual(client.create.call_count, 1)
            row = json.loads(path.read_text(encoding="utf-8"))
            self.assertIsNone(row["text"])
            self.assertIsNone(row["graders"])
            self.assertEqual(row["reason"], "provider_error")
            self.assertGreater(row["usage"]["reserved_micro"], 0)


class JudgeTests(unittest.TestCase):
    def test_judge_dry_run_and_cap_do_not_read_key(self):
        row = json.dumps(current_row())
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "run.jsonl"
            path.write_text(row + "\n", encoding="utf-8")
            for yes, cap, expected in ((False, 1, 2), (True, 0.000001, 1)):
                args = ask_eval.argparse.Namespace(path=str(path), run=1, yes=yes, max_usd=cap, batch=None)
                output = io.StringIO()
                with mock.patch.object(ask_eval, "_key", side_effect=AssertionError("key read")), contextlib.redirect_stdout(output):
                    self.assertEqual(ask_eval.judge(args), expected)
                self.assertIn("reservation bound $", output.getvalue())

    def test_judge_persists_batch_before_poll_and_resumes_with_retries(self):
        from types import SimpleNamespace

        row = json.dumps(current_row())
        for batch in (None, "existing-batch"):
            with self.subTest(batch=batch), tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "run.jsonl"
                path.write_text(row + "\n", encoding="utf-8")
                args = ask_eval.argparse.Namespace(path=str(path), run=1, yes=True, max_usd=1, batch=batch)
                client = mock.Mock()
                client.batches.create.return_value = SimpleNamespace(id="new-batch")
                identity = batch or "new-batch"
                output = io.StringIO()
                calls = []
                def retrieve(batch_id):
                    self.assertEqual(batch_id, identity)
                    self.assertEqual(json.loads(Path(str(path) + ".judge.batch.json").read_text(encoding="utf-8"))["batch_id"], identity)
                    self.assertIn(identity, output.getvalue())
                    calls.append(batch_id)
                    if len(calls) == 1:
                        raise ask_eval.ask.ProviderStatus(503)
                    return SimpleNamespace(processing_status="ended")
                client.batches.retrieve.side_effect = retrieve
                client.batches.results.return_value = []
                with mock.patch.object(ask_eval, "_key", return_value="sentinel"), contextlib.redirect_stdout(output):
                    with self.assertRaisesRegex(ValueError, "judge_incomplete"):
                        ask_eval.judge(args, client_factory=lambda key: client, sleep=lambda seconds: None)
                self.assertEqual(len(calls), 2)
                self.assertEqual(client.batches.create.call_count, int(batch is None))

    def test_judge_retries_raw_transient_poll_errors(self):
        from types import SimpleNamespace

        class ResponseError(Exception):
            def __init__(self, status_code):
                self.status_code = status_code

        row = json.dumps(current_row())
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "run.jsonl"
            path.write_text(row + "\n", encoding="utf-8")
            args = ask_eval.argparse.Namespace(path=str(path), run=1, yes=True, max_usd=1, batch="existing-batch")
            client = mock.Mock()
            client.batches.retrieve.side_effect = [ResponseError(429), ResponseError(503), TimeoutError(),
                                                    SimpleNamespace(processing_status="ended")]
            client.batches.results.return_value = []
            with mock.patch.object(ask_eval, "_key", return_value="sentinel"), contextlib.redirect_stdout(io.StringIO()):
                with self.assertRaisesRegex(ValueError, "judge_incomplete"):
                    ask_eval.judge(args, client_factory=lambda key: client, sleep=lambda seconds: None)
            self.assertEqual(client.batches.retrieve.call_count, 4)
            client.batches.create.assert_not_called()

    def test_judge_polling_has_deadline_and_does_not_retry_permanent_errors(self):
        from types import SimpleNamespace

        row = json.dumps(current_row())
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "run.jsonl"
            path.write_text(row + "\n", encoding="utf-8")
            args = ask_eval.argparse.Namespace(path=str(path), run=1, yes=True, max_usd=1, batch="existing-batch")
            client = mock.Mock()
            client.batches.retrieve.return_value = SimpleNamespace(processing_status="in_progress")
            with mock.patch.object(ask_eval, "_key", return_value="sentinel"), contextlib.redirect_stdout(io.StringIO()):
                with self.assertRaisesRegex(ValueError, "judge_timeout"):
                    ask_eval.judge(args, client_factory=lambda key: client, sleep=lambda seconds: None,
                                   clock=mock.Mock(side_effect=[0, 0, 3601]))
                client.batches.retrieve.side_effect = ask_eval.ask.ProviderStatus(400)
                with self.assertRaises(ask_eval.ask.ProviderStatus):
                    ask_eval.judge(args, client_factory=lambda key: client, sleep=lambda seconds: None)
            self.assertEqual(client.batches.retrieve.call_count, 2)

    def test_cli_withholds_batch_exception_text(self):
        import runpy
        import sys

        sentinel = "batch-exception-sentinel"
        row = json.dumps(current_row())
        client = mock.Mock()
        client.batches.create.side_effect = RuntimeError(sentinel)
        stderr = io.StringIO()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "run.jsonl"
            path.write_text(row + "\n", encoding="utf-8")
            with (mock.patch.object(ask_eval.ask, "_read_key", return_value=sentinel),
                  mock.patch.object(ask_eval.ask, "sdk_client", return_value=client),
                  mock.patch.object(sys, "argv", ["ask_eval.py", "judge", str(path), "--max-usd", "1", "--yes"]),
                  contextlib.redirect_stderr(stderr)):
                with self.assertRaises(SystemExit) as stopped:
                    runpy.run_path(str(ROOT / "scripts/ask_eval.py"), run_name="__main__")
            self.assertEqual(stopped.exception.code, 1)
        self.assertEqual(stderr.getvalue(), "ask eval unavailable: RuntimeError\n")
        self.assertNotIn(sentinel, stderr.getvalue())
        client.batches.create.assert_called_once()

    def test_unsupported_statement_prints_item_and_statement_text(self):
        from types import SimpleNamespace

        row = current_row()
        item = next(item for item in ask_eval.corpus() if item["item"] == row["item"])
        selection, _ = ask_eval.ask.parse_selection(row["text"], item["projection"], item["question_id"])
        statement = ask_eval.ask.render_answer(selection, item["projection"])["statements"][0]["text"]
        text = json.dumps({"verdicts": [{"index": 0, "supported": False, "why": "The evidence does not support this statement."}]})
        result = SimpleNamespace(custom_id="item-0", result=SimpleNamespace(type="succeeded",
                                 message=SimpleNamespace(content=[SimpleNamespace(type="text", text=text)])))
        client = mock.Mock()
        client.batches.create.return_value = SimpleNamespace(id="batch")
        client.batches.retrieve.return_value = SimpleNamespace(processing_status="ended")
        client.batches.results.return_value = [result]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "run.jsonl"
            path.write_text(json.dumps(row) + "\n", encoding="utf-8")
            args = ask_eval.argparse.Namespace(path=str(path), run=1, yes=True, max_usd=1, batch=None)
            output = io.StringIO()
            with mock.patch.object(ask_eval, "_key", return_value="sentinel"), contextlib.redirect_stdout(output):
                self.assertEqual(ask_eval.judge(args, client_factory=lambda key: client), 1)
            self.assertIn(row["item"], output.getvalue())
            self.assertIn(statement, output.getvalue())
            self.assertIn("The evidence does not support this statement.", output.getvalue())

    def test_duplicate_result_id_refuses_incomplete_judgement(self):
        from types import SimpleNamespace

        rows = [json.dumps(current_row(index)) for index in range(2)]
        text = json.dumps({"verdicts": [{"index": 0, "supported": True, "why": "The check is recorded."}]})
        result = SimpleNamespace(custom_id="item-0", result=SimpleNamespace(type="succeeded",
                                 message=SimpleNamespace(content=[SimpleNamespace(type="text", text=text)])))
        client = mock.Mock()
        client.batches.create.return_value = SimpleNamespace(id="batch")
        client.batches.retrieve.return_value = SimpleNamespace(processing_status="ended")
        client.batches.results.return_value = [result, result]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "run.jsonl"
            path.write_text("\n".join(rows) + "\n", encoding="utf-8")
            args = ask_eval.argparse.Namespace(path=str(path), run=1, yes=True, max_usd=1, batch=None)
            with mock.patch.object(ask_eval, "_key", return_value="sentinel"), contextlib.redirect_stdout(io.StringIO()):
                with self.assertRaisesRegex(ValueError, "judge_incomplete"):
                    ask_eval.judge(args, client_factory=lambda key: client)


if __name__ == "__main__":
    unittest.main()
