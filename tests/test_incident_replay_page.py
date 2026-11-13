import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from intel import render


ROOT = Path(__file__).resolve().parent.parent


def no_network(*args, **kwargs):
    raise AssertionError("the test opened the network")


class IncidentReplayPageTest(unittest.TestCase):
    def setUp(self):
        self.document = json.loads((ROOT / "intel/incidents/rhea.json").read_text(encoding="utf-8"))
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.replays = Path(self.tmp.name)
        for target in ("urllib.request.urlopen", "gate.rpc._open_with_deadline", "gate.rpc._fetch"):
            patch = mock.patch(target, side_effect=no_network)
            patch.start()
            self.addCleanup(patch.stop)
        patch = mock.patch.object(render, "REPLAYS_DIR", self.replays, create=True)
        patch.start()
        self.addCleanup(patch.stop)
        checks = [{"id": name, "status": "pass", "detail": "Check ran."}
                  for name in ("receiver_exists", "label_denylist", "memo_injection", "account_age",
                               "dust_funded", "lookalike", "code_without_metadata")]
        checks[3].update(status="signal", detail="Account is one day old.")
        self.run = {"incident_id": "rhea", "as_of": "2026-04-16T00:00:00Z", "height": 100,
                    "block_timestamp": 1776297599000000000,
                    "label_store": {"labels": [], "incidents": []},
                    "accounts": [{"account_id": "young.near", "role": "worker", "verdict": "DENY",
                                  "outcome": "DENY", "checks": checks}]}

    def write_run(self):
        path = self.replays / "rhea/run.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(self.run), encoding="utf-8", newline="\n")

    def test_generated_rhea_page_contains_recorded_replay(self):
        recorded = json.loads((ROOT / "fixtures/replay/rhea/run.json").read_text(encoding="utf-8"))
        page = (ROOT / "demo/site/incidents/rhea.html").read_text(encoding="utf-8")
        self.assertIn("<h2>Replayed as of 16 Apr 2026, with no labels</h2>", page)
        section = page.split("<h2>Replayed as of", 1)[1].split("</section>", 1)[0]
        self.assertIn(f"block {recorded['height']}", section)
        for row in recorded["accounts"]:
            self.assertIn(row["account_id"], section)
            self.assertIn(row["outcome"], section)

    def test_replay_section_exists_only_with_a_run_file(self):
        before = render.incident_page(self.document, {})
        self.assertNotIn("Replayed as of", before)
        self.write_run()
        page = render.incident_page(self.document, {})
        self.assertIn("<h2>Replayed as of 16 Apr 2026, with no labels</h2>", page)
        self.assertIn('class="profile-data-table"', page)
        for text in ("worker", "young.near", "DENY", "account_age", "signal", "Account is one day old.",
                     "2026-04-15 23:59:59 UTC", "block 100", "only on-chain state", "no labels",
                     "a replay, not a live prediction"):
            self.assertIn(text, page)
        (self.replays / "rhea/run.json").unlink()
        self.assertEqual(render.incident_page(self.document, {}), before)

    def test_malformed_block_metadata_renders_invalid_replay(self):
        for field in ("height", "block_timestamp"):
            original = self.run[field]
            for value in (True, False, 100.0, 0.5, -1, "100", None):
                with self.subTest(field=field, value=value):
                    self.run[field] = value
                    self.write_run()
                    page = render.incident_page(self.document, {})
                    self.assertIn("<h2>Replay record invalid</h2>", page)
                    self.assertNotIn("<h2>Replayed as of", page)
                    self.assertNotIn('id="incident-replay"', page)
            self.run[field] = original

    def test_zero_block_metadata_is_valid(self):
        self.run.update(height=0, block_timestamp=0)
        self.write_run()
        page = render.incident_page(self.document, {})
        self.assertIn("block 0 (1970-01-01 00:00:00 UTC)", page)
        self.assertNotIn("Replay record invalid", page)

    def test_unknown_check_keeps_ask_outcome(self):
        row = self.run["accounts"][0]
        row.update(verdict="UNKNOWN", outcome="ASK")
        row["checks"][3].update(status="unknown", detail="Account age is unavailable.")
        self.write_run()
        page = render.incident_page(self.document, {})
        section = page.split("<h2>Replayed as of", 1)[1].split("</section>", 1)[0]
        self.assertIn("ASK", section)
        self.assertIn("unknown", section)
        self.assertIn("Account age is unavailable.", section)
        self.assertNotIn("ALLOW", section)

    def test_replay_fields_are_escaped_and_full_account_is_retained(self):
        row = self.run["accounts"][0]
        row.update(account_id="a" * 64, role="<worker>")
        row["checks"][3]["detail"] = "<missing>"
        self.write_run()
        page = render.incident_page(self.document, {})
        for text in ("&lt;worker&gt;", "&lt;missing&gt;",
                     "a" * 12 + "…" + "a" * 10, "a" * 64, "Full identifier"):
            self.assertIn(text, page)
        for text in ("<worker>", "<missing>"):
            self.assertNotIn(text, page)

    def test_unknown_check_with_tampered_allow_outcome_is_rejected(self):
        row = self.run["accounts"][0]
        row.update(verdict="ALLOW", outcome="ALLOW")
        row["checks"][3]["status"] = "unknown"
        self.write_run()
        with self.assertRaises(ValueError):
            render.incident_page(self.document, {})

    def test_partial_empty_and_duplicate_checks_are_rejected(self):
        row = self.run["accounts"][0]
        row.update(verdict="ALLOW", outcome="ALLOW")
        complete = [{**check, "status": "pass"} for check in row["checks"]]
        cases = (None, [], [{"id": "memo_injection", "status": "pass", "detail": "No memo."}],
                 complete + [complete[0]], [{"id": check["id"], "status": check["status"]}
                                           for check in complete])
        for checks in cases:
            with self.subTest(checks=checks):
                row["checks"] = checks
                self.write_run()
                with self.assertRaises(ValueError):
                    render.incident_page(self.document, {})
        del row["checks"]
        self.write_run()
        with self.assertRaises(ValueError):
            render.incident_page(self.document, {})

    def test_unrecognized_status_and_verdict_are_rejected(self):
        row = self.run["accounts"][0]
        row["checks"][3]["status"] = "partial"
        self.write_run()
        with self.assertRaises(ValueError):
            render.incident_page(self.document, {})
        row["checks"][3]["status"] = "signal"
        row["verdict"] = "partial"
        self.write_run()
        with self.assertRaises(ValueError):
            render.incident_page(self.document, {})

    def test_nonempty_label_store_is_rejected(self):
        for key in ("labels", "incidents"):
            with self.subTest(key=key):
                self.run["label_store"] = {"labels": [], "incidents": []}
                self.run["label_store"][key].append("later information")
                self.write_run()
                with self.assertRaises(ValueError):
                    render.incident_page(self.document, {})

    def test_wrong_incident_id_is_rejected(self):
        self.run["incident_id"] = "other"
        self.write_run()
        with self.assertRaises(ValueError):
            render.incident_page(self.document, {})


if __name__ == "__main__":
    unittest.main()
