import contextlib
import importlib.util
import io
import json
import tempfile
import unittest
from pathlib import Path

from intel import labels, trace

ROOT = Path(__file__).resolve().parent.parent
FORMATS = (
    r"On \d+ recorded mainnet receivers the rules returned \d+ ALLOW, \d+ DENY, \d+ Unknown\.",
    (r"Label store: \d+ assertions over \d+ subjects \(onchain \d+, official_source \d+, third_party \d+, "
     r"inferred \d+\)\."),
    r"Incidents: \d+ pages\.",
    (r"Feed: (not run|since \d{4}-\d{2}-\d{2}, \d+ blocks, \d+ events \(\d+ code_hash_matches_incident, \d+ "
     r"fresh_dust_funded_pays_labelled, \d+ watch_account_key_added\))\."),
    r"Flows: \d+ exit traces, \d+ hops, depth \d+ of \d+, (complete|stopped while reading (the subject|an account \d+ hops? from the subject))\.",
    r"Latency: (not measured|p50 \d+ ms, p95 \d+ ms over \d+ verdict runs)\.",
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
        self.assertEqual(len(printed), len(FORMATS))
        for line, pattern in zip(printed, FORMATS):
            with self.subTest(line=line):
                self.assertRegex(line, "^" + pattern + "$")

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

    def test_the_feed_and_flows_lines_equal_a_recount(self):
        state = json.loads((ROOT / "intel" / "feed" / "state.json").read_text(encoding="utf-8"))
        signals = ", ".join(f"{count} {name}" for name, count in state["events"].items())
        self.assertEqual(self.counts.feed_line(), f"Feed: since {state['start_time'][:10]}, {state['blocks']} blocks, "
                                                  f"{sum(state['events'].values())} events ({signals}).")
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

    def test_latency_is_not_measured_without_files_and_nearest_rank_with_them(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(self.counts.latency_line(Path(tmp)), "Latency: not measured.")
            (Path(tmp) / "a.json").write_text(json.dumps(list(range(1, 11))), encoding="utf-8")
            (Path(tmp) / "b.json").write_text(json.dumps([{"latency_ms": n} for n in range(11, 21)]),
                                              encoding="utf-8")
            self.assertEqual(self.counts.latency_line(Path(tmp)), "Latency: p50 10 ms, p95 19 ms over 20 verdict runs.")
            (Path(tmp) / "c.json").write_text(json.dumps([1.5]), encoding="utf-8")
            with self.assertRaises(ValueError):
                self.counts.latency_line(Path(tmp))

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
