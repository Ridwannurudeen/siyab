"""Reproduce the frozen holdout judgements and scores through checked replay."""

import contextlib
import hashlib
import io
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from eval import score
from gate import rpc
from intel import __main__ as intel_main
from intel import budget, labels, top_receivers

ROOT = Path(__file__).resolve().parent.parent
HOLDOUT = ROOT / "fixtures" / "holdout" / "2026-11-22"
RUN = HOLDOUT / "run"
FROZEN_LABELS = HOLDOUT / "labels.jsonl"
HOLDOUT_SHA256 = "9997b665fcb6eb1fa31d212a744d279efed1b174ac3776fc5ff6125bc4542fe5"
LABELS_SHA256 = "1f3b865366bf94d85dc107e0c34dda35c79eb3d8c682087cc92167ff4a77ecc8"
RULES_COMMIT = "fb77657637cbdd7e8667cfc787ab3cffcc731742"


class HoldoutTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.frozen = {path.relative_to(HOLDOUT): path.read_bytes()
                      for path in HOLDOUT.rglob("*") if path.is_file()}
        cls.addClassCleanup(cls.check_frozen)
        temporary = tempfile.TemporaryDirectory()
        cls.addClassCleanup(temporary.cleanup)
        cls.directory = Path(temporary.name)
        stack = contextlib.ExitStack()
        cls.addClassCleanup(stack.close)
        environment = {name: os.environ[name] for name in
                       ("PATH", "SYSTEMROOT", "WINDIR", "TEMP", "TMP", "GIT_DIR", "GIT_WORK_TREE")
                       if name in os.environ}
        environment["GATE_FIXTURES_MODE"] = "off"
        stack.enter_context(mock.patch.dict(os.environ, environment, clear=True))
        for target, name in ((rpc, "_fetch"), (rpc, "_open_with_deadline"),
                             (rpc.Client, "_metered"), (budget, "reserve"),
                             (budget, "_transaction"), (budget, "use_database"),
                             (top_receivers, "fastnear_headers"), (intel_main, "_record_database")):
            stack.enter_context(mock.patch.object(target, name,
                side_effect=AssertionError("holdout replay attempted network, provider budget or key access")))
        stack.enter_context(mock.patch("urllib.request.urlopen",
            side_effect=AssertionError("holdout replay opened the network")))
        load_labels = labels.load

        def load_frozen_labels(path=labels.STORE_PATH):
            if Path(path) != FROZEN_LABELS:
                raise AssertionError("holdout replay attempted to read the live label store")
            return load_labels(path)

        stack.enter_context(mock.patch.object(labels, "load", side_effect=load_frozen_labels))
        cls.checkpoint = json.loads((RUN / "set.json").read_bytes())
        cls.store = labels.load(FROZEN_LABELS)
        document = {"judged_at_ms": cls.checkpoint["judged_at_ms"], "subjects": [
            {"subject": account, "as": "receiver", "request": intel_main._request(account, False)}
            for account in cls.checkpoint["accounts"]]}
        cls.reproduced = cls.directory / "historical"
        with mock.patch.object(intel_main, "RECEIVER_RPC_DIR", RUN / "rpc"), \
                mock.patch.object(intel_main, "JUDGED_BLOCK_PATH", RUN / "judged-block.json"):
            cls.results = intel_main.judge_receivers(document, cls.store, cls.reproduced,
                reports_account=cls.checkpoint["reports_account"], pin_unknown_age=True, rules_version=3)
        for result in cls.results:
            intel_main._write_json(cls.reproduced / "judgements" / f"{result['subject']}.json",
                                   {**result, "rules_version": 3})

    @classmethod
    def check_frozen(cls):
        after = {path.relative_to(HOLDOUT): path.read_bytes()
                 for path in HOLDOUT.rglob("*") if path.is_file()}
        if after != cls.frozen:
            raise AssertionError("holdout replay changed the frozen files")

    def test_holdout_hash_counts_input_order_and_label_store_match_the_run(self):
        data = (HOLDOUT / "receivers.jsonl").read_bytes()
        self.assertEqual(hashlib.sha256(data).hexdigest(), HOLDOUT_SHA256)
        rows = [json.loads(line) for line in data.splitlines()]
        self.assertEqual(len(rows), 252)
        self.assertEqual(sum(row["label"] == "bad" for row in rows), 34)
        self.assertEqual(sum(row["label"] == "good" for row in rows), 218)
        accounts = (HOLDOUT / "accounts-shuffled.txt").read_text(encoding="utf-8").splitlines()
        self.assertEqual(accounts, sorted((row["account"] for row in rows),
            key=lambda account: hashlib.sha256(account.encode("utf-8")).digest()))
        self.assertEqual(accounts, self.checkpoint["accounts"])
        self.assertEqual(self.store.sha256, LABELS_SHA256)
        self.assertEqual((HOLDOUT / "labels.sha256").read_text(encoding="ascii"),
                         LABELS_SHA256 + "  labels.jsonl\n")
        self.assertEqual(self.store.sha256, self.checkpoint["label_store_sha256"])
        self.assertEqual(self.checkpoint["rules_version"], 3)

    def test_historical_replay_reproduces_every_judgement_byte(self):
        self.assertEqual(len(self.results), 252)
        expected = {path.name: path.read_bytes() for path in (RUN / "judgements").glob("*.json")}
        actual = {path.name: path.read_bytes() for path in (self.reproduced / "judgements").glob("*.json")}
        self.assertEqual(set(actual), set(expected))
        self.assertEqual(len(expected), 252)
        for name in expected:
            with self.subTest(account=name):
                self.assertEqual(actual[name], expected[name])

    def test_scorer_reproduces_both_historical_score_files_byte_for_byte(self):
        for name, extra in (("score.json", []), ("score-excluding-labelled.json", ["--exclude-labelled"])):
            with self.subTest(score=name):
                output, errors = io.BytesIO(), io.StringIO()
                with io.TextIOWrapper(output, encoding="utf-8", newline="\n") as stdout:
                    with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(errors):
                        result = score.main(["--verdicts", str(self.reproduced / "judgements"),
                            "--holdout", str(HOLDOUT / "receivers.jsonl"),
                            "--labels", str(FROZEN_LABELS),
                            "--rules-commit", RULES_COMMIT, "--json", *extra])
                    stdout.flush()
                    self.assertEqual(result, 0, errors.getvalue())
                    self.assertEqual(errors.getvalue(), "")
                    self.assertEqual(output.getvalue(), (HOLDOUT / name).read_bytes())

    def test_checked_command_reproduces_every_judgement_and_both_scores(self):
        destination = self.directory / "checked"
        shutil.copytree(RUN, destination, ignore=shutil.ignore_patterns("judgements"))
        self.assertFalse((destination / "judgements").exists())
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            result = intel_main.main(["judge-accounts", "--accounts", str(HOLDOUT / "accounts-shuffled.txt"),
                                      "--out", str(destination), "--labels", str(FROZEN_LABELS)])
        self.assertEqual(result, 0, stderr.getvalue())
        self.assertEqual(stderr.getvalue(), "")
        self.assertEqual(json.loads(stdout.getvalue())["judged"], 252)
        self.assertEqual({path.name for path in (destination / "judgements").glob("*.json")},
                         {f"{account}.json" for account in self.checkpoint["accounts"]})
        rows = {row["account"]: row for row in
                (json.loads(line) for line in (HOLDOUT / "receivers.jsonl").read_bytes().splitlines())}
        for account in self.checkpoint["accounts"]:
            with self.subTest(account=account):
                original = (RUN / "judgements" / f"{account}.json").read_bytes()
                reproduced = (destination / "judgements" / f"{account}.json").read_bytes()
                answer = json.loads(reproduced)
                self.assertNotIn("scoreable", answer)
                self.assertEqual(reproduced, original)
        barvwin = json.loads((destination / "judgements" / "barvwin.near.json").read_bytes())
        self.assertNotIn("scoreable", barvwin)
        self.assertEqual(barvwin["verdict"], "UNKNOWN")
        self.assertEqual(barvwin["observed"]["block_height"],
                         json.loads((RUN / "judged-block.json").read_bytes())["height"])
        answers, errors, duplicates = score.load_verdicts(destination / "judgements")
        self.assertEqual(errors, [])
        self.assertEqual(duplicates, [])
        report = score.score(list(rows.values()), answers)
        self.assertEqual(report["counts"]["not_judged"], 0)
        self.assertEqual(report["counts"]["judged"], 252)
        self.assertEqual(report["not_judged_accounts"], [])
        self.assertEqual(report["matrix"], {
            "bad": {"DENY": 9, "ALLOW": 25, "ASK": 0, "NOT_JUDGED": 0},
            "good": {"DENY": 0, "ALLOW": 212, "ASK": 6, "NOT_JUDGED": 0},
        })
        for name, extra in (("score.json", []), ("score-excluding-labelled.json", ["--exclude-labelled"])):
            with self.subTest(score=name):
                output, errors = io.BytesIO(), io.StringIO()
                with io.TextIOWrapper(output, encoding="utf-8", newline="\n") as stdout:
                    with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(errors):
                        result = score.main(["--verdicts", str(destination / "judgements"),
                            "--holdout", str(HOLDOUT / "receivers.jsonl"),
                            "--labels", str(FROZEN_LABELS),
                            "--rules-commit", RULES_COMMIT, "--json", *extra])
                    stdout.flush()
                    self.assertEqual(result, 0, errors.getvalue())
                    self.assertEqual(errors.getvalue(), "")
                    self.assertEqual(output.getvalue(), (HOLDOUT / name).read_bytes())

    def test_git_preserves_every_frozen_file_as_exact_bytes(self):
        names = [f"fixtures/holdout/2026-11-22/{path.as_posix()}" for path in sorted(self.frozen)]
        result = subprocess.run(["git", "check-attr", "--stdin", "text"], cwd=ROOT,
            input=("\n".join(names) + "\n").encode("utf-8"), capture_output=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        lines = result.stdout.decode("utf-8").splitlines()
        self.assertEqual(len(lines), len(names))
        for name, line in zip(names, lines):
            with self.subTest(path=name):
                self.assertEqual(line, f"{name}: text: unset")


if __name__ == "__main__":
    unittest.main()
