import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

from intel import __main__ as intel_main


class ReplayReceiversTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        for name in ("urllib.request.urlopen", "gate.rpc._fetch", "gate.rpc._open_with_deadline"):
            patcher = mock.patch(name, side_effect=AssertionError("the replay opened the network"))
            patcher.start()
            self.addCleanup(patcher.stop)

    def replay(self, name, version=None):
        destination = self.root / f"{name}-{version}"
        args = ["replay-receivers", "--set", name, "--out", str(destination)]
        if version is not None:
            args += ["--rules-version", str(version)]
        output = io.StringIO()
        with redirect_stdout(output):
            self.assertEqual(intel_main.main(args), 0)
        summary = json.loads(output.getvalue())
        self.assertEqual(json.loads((destination / "summary.json").read_text(encoding="utf-8")), summary)
        return destination, summary

    def test_default_ordinary_replay_is_byte_identical_even_when_environment_requests_live(self):
        with mock.patch.dict(os.environ, {"GATE_FIXTURES_MODE": "off",
                                          "FASTNEAR_API_KEY_FILE": "not-read",
                                          "REPORTS_ACCOUNT_ID": "not-read.near"}):
            destination, summary = self.replay("ordinary")
        self.assertEqual(summary, {"set": "ordinary", "rules_version": 1, "judged": 84,
                                   "verdicts": {"ALLOW": 70, "DENY": 14, "UNKNOWN": 0},
                                   "changed_verdicts": 0, "transitions": {}})
        for saved in (intel_main.RECEIVERS_DIR / "judgements").glob("*.json"):
            self.assertEqual((destination / "judgements" / saved.name).read_bytes(), saved.read_bytes())

    def test_ordinary_version_3_replay_has_no_verdict_changes(self):
        _, summary = self.replay("ordinary", 3)
        self.assertEqual(summary["verdicts"], {"ALLOW": 70, "DENY": 14, "UNKNOWN": 0})
        self.assertEqual((summary["rules_version"], summary["judged"], summary["changed_verdicts"]), (3, 84, 0))

    def test_top_version_1_replay_is_byte_identical_including_pinned_fallback(self):
        destination, summary = self.replay("top", 1)
        self.assertEqual(summary["verdicts"], {"ALLOW": 850, "DENY": 138, "UNKNOWN": 2})
        self.assertEqual((summary["judged"], summary["changed_verdicts"]), (990, 0))
        for saved in (intel_main.TOP_RECEIVERS_DIR / "judgements").glob("*.json"):
            self.assertEqual((destination / "judgements" / saved.name).read_bytes(), saved.read_bytes())

    def test_top_version_3_changes_only_age_only_denials(self):
        destination, summary = self.replay("top", 3)
        self.assertEqual(summary, {"set": "top", "rules_version": 3, "judged": 990,
                                   "verdicts": {"ALLOW": 850, "DENY": 1, "UNKNOWN": 139},
                                   "changed_verdicts": 137, "transitions": {"DENY to UNKNOWN": 137}})
        changed = []
        for saved in (intel_main.TOP_RECEIVERS_DIR / "judgements").glob("*.json"):
            before = json.loads(saved.read_text(encoding="utf-8"))
            after = json.loads((destination / "judgements" / saved.name).read_text(encoding="utf-8"))
            self.assertEqual(after["checks"], before["checks"])
            if after["verdict"] != before["verdict"]:
                changed.append(saved.name)
                self.assertEqual({c["id"] for c in after["checks"] if c["status"] == "signal"}, {"account_age"})
                self.assertIn("among the checks that ran", after["reasons"][0])
                self.assertEqual(after["reasons"][-1], "Not on the owner's allowlist.")
            else:
                if after["verdict"] == "UNKNOWN":
                    before["reasons"].append("Not on the owner's allowlist.")
                self.assertEqual(after, before)
        self.assertEqual(len(changed), 137)

    def test_existing_destination_is_refused_without_judging_or_overwriting(self):
        marker = self.root / "kept.txt"
        marker.write_text("keep", encoding="utf-8")
        with redirect_stderr(io.StringIO()), mock.patch.object(intel_main, "judge_receivers") as judge:
            result = intel_main.main(["replay-receivers", "--set", "top", "--out", str(self.root)])
        self.assertEqual(result, 2)
        judge.assert_not_called()
        self.assertEqual(marker.read_text(encoding="utf-8"), "keep")
        self.assertEqual(list(self.root.iterdir()), [marker])

    def test_destination_inside_source_tree_is_refused(self):
        destination = intel_main.RECEIVERS_DIR / "must-not-create"
        with redirect_stderr(io.StringIO()), mock.patch.object(intel_main, "judge_receivers") as judge:
            result = intel_main.main(["replay-receivers", "--set", "ordinary", "--out", str(destination)])
        self.assertEqual(result, 2)
        judge.assert_not_called()
        self.assertFalse(destination.exists())

    def test_recording_and_unrecognised_version_are_refused_before_judging(self):
        for option in (["--record"], ["--rules-version", "4"]):
            with self.subTest(option=option), redirect_stderr(io.StringIO()), \
                    mock.patch.object(intel_main, "judge_receivers") as judge, self.assertRaises(SystemExit) as caught:
                intel_main.main(["replay-receivers", "--set", "top", "--out", str(self.root / "new")] + option)
            self.assertEqual(caught.exception.code, 2)
            judge.assert_not_called()
        self.assertFalse((self.root / "new").exists())


if __name__ == "__main__":
    unittest.main()
