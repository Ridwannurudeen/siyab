import contextlib
import importlib.util
import io
import json
import math
import re
import tempfile
import unittest
from pathlib import Path

from gate import rpc
from intel import __main__ as intel_main
from intel import labels, render, trace

ROOT = Path(__file__).resolve().parent.parent
TALLY = r"\d+ \(\d+ ALLOW, \d+ DENY, \d+ Unknown\)"
FORMATS = (
    r"On \d+ recorded mainnet receivers the rules returned \d+ ALLOW, \d+ DENY, \d+ Unknown\.",
    (rf"By cohort: ordinary counterparties {TALLY}; organisations from registries {TALLY}; incident accounts "
     rf"{TALLY}; catalog tokens {TALLY}\."),
    (r"Label store: \d+ assertions over \d+ subjects \(onchain \d+, official_source \d+, third_party \d+, "
     r"inferred \d+\)\."),
    r"Incidents: \d+ pages\.",
    (r"Feed: (not run|since \d{4}-\d{2}-\d{2}, \d+ blocks, \d+ events \(\d+ code_hash_matches_incident, \d+ "
     r"fresh_dust_funded_pays_labelled, \d+ watch_account_key_added, \d+ labelled_account_large_transfer, \d+ "
     r"deploy_on_labelled_contract\)|following since \d{4}-\d{2}-\d{2}, \d+ "
     r"blocks, \d+ events \(\d+ code_hash_matches_incident, \d+ fresh_dust_funded_pays_labelled, "
     r"\d+ watch_account_key_added, \d+ labelled_account_large_transfer, \d+ deploy_on_labelled_contract\))\."),
    r"Flows: \d+ exit traces, \d+ hops, depth \d+ of \d+, (complete|stopped while reading (the subject|an account \d+ hops? from the subject))\.",
    r"Latency: (not measured|p50 \d+ ms, p95 \d+ ms over \d+ verdict runs)\.",
    r"Latency, payee off the allowlist: (not measured|p50 \d+ ms, p95 \d+ ms over \d+ verdict runs)\.",
    r"Latency, payee off the allowlist, NearBlocks fallback: (not measured|p50 \d+ ms, p95 \d+ ms over "
    r"\d+ verdict runs)\.",
)


def load_counts():
    spec = importlib.util.spec_from_file_location("counts", ROOT / "scripts" / "counts.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class CountsTest(unittest.TestCase):
    def setUp(self):
        self.counts = load_counts()

    def run_main(self, *argv):
        output = io.StringIO()
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(io.StringIO()):
            code = self.counts.main(list(argv))
        return code, output.getvalue()

    def test_the_lines_follow_the_fixed_format_in_order(self):
        code, output = self.run_main()
        self.assertEqual(code, 0)
        printed = output.splitlines()
        self.assertEqual(len(printed), len(FORMATS) - 2 + self.counts.TIER2_LATENCY_RUNS.is_file()
                         + self.counts.TIER2_FALLBACK_LATENCY_RUNS.is_file())
        for line, pattern in zip(printed, FORMATS):
            with self.subTest(line=line):
                self.assertRegex(line, "^" + pattern + "$")

    def test_the_cohort_line_is_the_one_intel_receivers_prints(self):
        document = json.loads((ROOT / "fixtures" / "receivers" / "set.json").read_text(encoding="utf-8"))
        judgements = {path.stem: json.loads(path.read_text(encoding="utf-8"))
                      for path in (ROOT / "fixtures" / "receivers" / "judgements").glob("*.json")}
        self.assertEqual(sorted(judgements), sorted(entry["subject"] for entry in document["subjects"]))
        self.assertEqual(self.counts.cohorts_line(),
                         intel_main.cohort_line(document, [judgements[e["subject"]] for e in document["subjects"]]))
        self.assertEqual(self.counts.lines()[:2], [self.counts.receivers_line(), self.counts.cohorts_line()])

    def test_the_label_and_incident_lines_equal_a_recount(self):
        store = labels.load()
        p = store.counts()["provenance"]
        self.assertEqual(self.counts.labels_line(),
                         f"Label store: {len(store.assertions)} assertions over "
                         f"{len({(a.chain, a.subject) for a in store.assertions})} subjects (onchain {p['onchain']}, "
                         f"official_source {p['official_source']}, third_party {p['third_party']}, "
                         f"inferred {p['inferred']}).")
        pages = len(list((ROOT / "intel" / "incidents").glob("*.json")))
        self.assertEqual(self.counts.incidents_line(), f"Incidents: {pages} pages.")

    def test_explore_provenance_uses_the_same_retained_assertions_as_counts(self):
        store = labels.load()
        expected = store.counts()["provenance"]
        workspace = render._workspace([], [], store, None)
        rows = re.findall(r'<div class="analytics-row"><span>([^<]+)</span>.*?'
                          r'<strong class="num">(\d+)</strong></div>', workspace)
        actual = {name: int(count) for name, count in rows}
        self.assertEqual({name: count for name, count in actual.items() if count},
                         {name: count for name, count in expected.items() if count})
        self.assertEqual(sum(actual.values()), store.counts()["assertions"])
        self.assertIn(f"Label store: {sum(actual.values())} assertions", self.counts.labels_line())

    def test_the_snapshot_and_tier_2_numbers_in_the_docs_equal_the_committed_files(self):
        evidence = (ROOT / "docs" / "EVIDENCE.md").read_text(encoding="utf-8")
        testnet = (ROOT / "docs" / "TESTNET.md").read_text(encoding="utf-8")
        snapshot = json.loads((ROOT / "intel" / "feed" / "follow-snapshot.json").read_text(encoding="utf-8"))
        self.assertIn(f"following since {snapshot['started_at']}, {snapshot['blocks']} blocks up to height "
                      f"{snapshot['last_height']} ({snapshot['last_time']}), {len(snapshot['skipped_heights'])} "
                      f"skipped heights and {sum(snapshot['events'].values())} events", evidence)
        runs = sorted(run["latency_ms"] for run in json.loads(
            (ROOT / "fixtures" / "latency" / "verdict-runs-tier2.json").read_text(encoding="utf-8")))
        p50, p95 = (runs[max(1, math.ceil(percent / 100 * len(runs))) - 1] for percent in (50, 95))
        self.assertIn(f"The recorded p50 is {p50} ms and p95 is {p95} ms.", evidence)
        self.assertIn(f"`fixtures/latency/verdict-runs-tier2.json` contains {len(runs)} runs of", evidence)
        self.assertIn(f"The tier 2 latency is measured over {len(runs)} runs on", testnet)
        self.assertIn(f"at least {rpc.NEARBLOCKS_MIN_INTERVAL_S} s apart (`NEARBLOCKS_MIN_INTERVAL_S`", evidence)
        self.assertIn(f"with p50 {p50} ms and p95 {p95} ms (`fixtures/latency/verdict-runs-tier2.json`)", testnet)

    def test_the_feed_and_flows_lines_equal_a_recount(self):
        snapshot = ROOT / "intel" / "feed" / "follow-snapshot.json"
        state = json.loads((snapshot if snapshot.is_file() else ROOT / "intel" / "feed" / "state.json")
                           .read_text(encoding="utf-8"))
        signals = ", ".join(f"{count} {name}" for name, count in state["events"].items())
        since = (f"following since {state['started_at'][:10]}" if state.get("mode") == "follow"
                 else f"since {state['start_time'][:10]}")
        self.assertEqual(self.counts.feed_line(), f"Feed: {since}, {state['blocks']} blocks, "
                                                  f"{sum(state['events'].values())} events ({signals}).")
        walked_skips = [height for height in state["skipped_heights"]
                        if state["start_height"] <= height <= state["last_height"]]
        self.assertEqual(state["blocks"], state["last_height"] - state["start_height"] + 1 - len(walked_skips))
        recorded = json.loads((ROOT / "intel" / "feed" / "state.json").read_text(encoding="utf-8"))
        recorded_signals = ", ".join(f"{count} {name}" for name, count in recorded["events"].items())
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "state.json"
            path.write_text(json.dumps(recorded), encoding="utf-8")
            self.assertEqual(self.counts.feed_line(path),
                             f"Feed: since {recorded['start_time'][:10]}, {recorded['blocks']} blocks, "
                             f"{sum(recorded['events'].values())} events ({recorded_signals}).")
        traces = [json.loads(p.read_text(encoding="utf-8")) for p in (ROOT / "intel" / "flows").glob("*.json")]
        self.assertTrue(traces)
        least = min(traces, key=lambda t: (t["depth_reached"], t["completeness"] == "complete"))
        self.assertEqual(self.counts.flows_line(),
                         f"Flows: {len(traces)} exit traces, {sum(len(t['hops']) for t in traces)} hops, depth "
                         f"{least['depth_reached']} of {least['depth']}, {trace.stop_state(least['completeness'])}.")
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(self.counts.flows_line(Path(tmp)), "Flows: 0 exit traces, 0 hops, depth 0 of 0, complete.")

    def test_the_feed_line_reads_not_run_without_a_state_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(self.counts.feed_line(Path(tmp) / "state.json"), "Feed: not run.")

    def test_the_counts_line_says_following_when_the_snapshot_says_so(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "snapshot.json"
            path.write_text(json.dumps({"mode": "follow", "started_at": "2026-11-03T00:00:00Z", "blocks": 20,
                                        "events": {"code_hash_matches_incident": 1,
                                                   "fresh_dust_funded_pays_labelled": 2,
                                                   "watch_account_key_added": 3}}), encoding="utf-8")
            self.counts.FEED_SNAPSHOT = path
            self.assertEqual(self.counts.feed_line(), "Feed: following since 2026-11-03, 20 blocks, 6 events "
                                                       "(1 code_hash_matches_incident, 2 "
                                                       "fresh_dust_funded_pays_labelled, 3 watch_account_key_added).")

    def test_latency_is_not_measured_without_files_and_nearest_rank_with_them(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "verdict-runs.json"
            self.assertEqual(self.counts.latency_line(path), "Latency: not measured.")
            path.write_text(json.dumps(list(range(1, 11)) + [{"latency_ms": n} for n in range(11, 21)]),
                            encoding="utf-8")
            self.assertEqual(self.counts.latency_line(path), "Latency: p50 10 ms, p95 19 ms over 20 verdict runs.")
            path.write_text(json.dumps([1.5]), encoding="utf-8")
            with self.assertRaises(ValueError):
                self.counts.latency_line(path)

    def test_the_second_latency_line_reads_its_own_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            first, second = Path(tmp) / "verdict-runs.json", Path(tmp) / "verdict-runs-tier2.json"
            first.write_text(json.dumps([{"receiver": "kestrelridge-shop.testnet", "run": n, "verdict": "ALLOW",
                                          "latency_ms": n} for n in range(1, 11)]), encoding="utf-8")
            self.counts.LATENCY_RUNS, self.counts.TIER2_LATENCY_RUNS = first, second
            self.assertIsNone(self.counts.tier2_latency_line(second))
            without = self.counts.lines()
            self.assertEqual(len(without), len(FORMATS) - 2)
            for line, pattern in zip(without, FORMATS):
                with self.subTest(line=line):
                    self.assertRegex(line, "^" + pattern + "$")
            self.assertEqual(without[-1], "Latency: p50 5 ms, p95 10 ms over 10 verdict runs.")
            second.write_text(json.dumps([{"receiver": "lindenvale.testnet", "run": n, "verdict": "ALLOW",
                                           "latency_ms": 100 * n,
                                           "path": "tier 1 and 2, account age from the access key nonce"}
                                          for n in range(1, 21)]), encoding="utf-8")
            printed = self.counts.lines()
            self.assertEqual(len(printed), len(FORMATS) - 1)
            for line, pattern in zip(printed, FORMATS):
                with self.subTest(line=line):
                    self.assertRegex(line, "^" + pattern + "$")
            self.assertEqual(printed[:-1], without)
            self.assertEqual(printed[-1], "Latency, payee off the allowlist: p50 1000 ms, p95 1900 ms over 20 verdict "
                                          "runs.")

    def test_the_fallback_latency_line_is_optional_and_uses_its_own_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            first = Path(tmp) / "verdict-runs.json"
            second = Path(tmp) / "verdict-runs-tier2.json"
            fallback = Path(tmp) / "verdict-runs-tier2-fallback.json"
            first.write_text(json.dumps([1]), encoding="utf-8")
            second.write_text(json.dumps([2]), encoding="utf-8")
            self.counts.LATENCY_RUNS = first
            self.counts.TIER2_LATENCY_RUNS = second
            self.counts.TIER2_FALLBACK_LATENCY_RUNS = fallback
            self.assertIsNone(self.counts.tier2_fallback_latency_line(fallback))
            self.assertEqual(len(self.counts.lines()), len(FORMATS) - 1)
            fallback.write_text(json.dumps([]), encoding="utf-8")
            printed = self.counts.lines()
            self.assertEqual(len(printed), len(FORMATS))
            self.assertRegex(printed[-1], "^" + FORMATS[-1] + "$")
            self.assertEqual(printed[-1], "Latency, payee off the allowlist, NearBlocks fallback: not measured.")

    def test_readme_rewrites_only_the_marked_block(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "README.md"
            path.write_text("# Title\n\n<!-- counts:start -->\nold numbers\n<!-- counts:end -->\n\nAfter.\n",
                            encoding="utf-8")
            code, output = self.run_main("--readme", str(path))
            self.assertEqual((code, output), (0, ""))
            lines = self.counts.lines()
            expected = ("# Title\n\n<!-- counts:start -->\n\n" + "".join(line + "\n\n" for line in lines)
                        + "<!-- counts:end -->\n\nAfter.\n")
            self.assertEqual(path.read_text(encoding="utf-8"), expected)
            self.run_main("--readme", str(path))
            self.assertEqual(path.read_text(encoding="utf-8"), expected)

    def test_readme_without_markers_exits_2_and_is_left_alone(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "README.md"
            path.write_text("# Title\n", encoding="utf-8")
            self.assertEqual(self.run_main("--readme", str(path))[0], 2)
            self.assertEqual(path.read_text(encoding="utf-8"), "# Title\n")


if __name__ == "__main__":
    unittest.main()
