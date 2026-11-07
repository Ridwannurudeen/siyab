"""Print the counts the README states, from committed files only, in a fixed format.

Usage:
  python scripts/counts.py                    print the lines
  python scripts/counts.py --readme README.md rewrite the block between the counts markers

The lines, in this order (every number is counted from the files named); the two off-allowlist lines are
printed only while their files exist, so there are seven to nine:

  On N recorded mainnet receivers the rules returned A ALLOW, D DENY, U Unknown.
      fixtures/receivers/judgements/*.json, one judgement per subject
  By cohort: ordinary counterparties N (A ALLOW, D DENY, U Unknown); organisations from registries N
  (A ALLOW, D DENY, U Unknown); incident accounts N (...); catalog tokens N (...).
      the cohort of each subject of fixtures/receivers/set.json and the verdict of its judgement, counted
      by cohort_line of intel/__main__.py, the function python -m intel receivers prints
  Label store: L assertions over E subjects (onchain O, official_source F, third_party T, inferred I).
      intel/labels.jsonl; E counts distinct (chain, subject) pairs
  Incidents: I pages.
      intel/incidents/*.json
  Feed: since D, B blocks, C events (X code_hash_matches_incident, Y fresh_dust_funded_pays_labelled,
  Z watch_account_key_added).
  Feed: following since D, B blocks, C events (X code_hash_matches_incident, Y
  fresh_dust_funded_pays_labelled, Z watch_account_key_added).
      intel/feed/follow-snapshot.json when it exists, else intel/feed/state.json; the second form
      when that file says mode "follow", else the first. D is the date of its start_time (the first
      form) or of its started_at (the second), then its block count and event count per signal, in
      the file's order; "Feed: not run." while neither file exists
  Flows: T exit traces, H hops, depth D of B, S.
      intel/flows/*.json, the hops of every trace; D (depth_reached) and B (depth) of the trace with
      the least progress (the smallest depth_reached, a stopped one first among equals); S is
      "complete" when every trace is complete, else the stop of that trace (or of the first stopped
      one): "stopped while reading the subject" or "stopped while reading an account N hop(s) from
      the subject"; "Flows: 0 exit traces, 0 hops, depth 0 of 0, complete." without a trace
  Latency: p50 X ms, p95 Y ms over R verdict runs.
      fixtures/latency/verdict-runs.json (the allowlisted payee's runs), a JSON list of integers or of
      objects with an integer latency_ms; the percentiles are nearest rank; "Latency: not measured."
      while the file does not exist or holds no run
  Latency, payee off the allowlist: p50 X ms, p95 Y ms over R verdict runs.
      fixtures/latency/verdict-runs-tier2.json in the same form and the same way; no line while the file
      does not exist
  Latency, payee off the allowlist, NearBlocks fallback: p50 X ms, p95 Y ms over R verdict runs.
      fixtures/latency/verdict-runs-tier2-fallback.json in the same form and the same way; no line while
      the file does not exist

--readme replaces everything between the lines <!-- counts:start --> and <!-- counts:end --> with the
lines, each followed by a blank line so each renders as its own paragraph, and exits 2 when the file
lacks the markers.
"""

import argparse
import json
import math
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from intel import __main__ as intel_main
from intel import labels, trace

JUDGEMENTS_DIR = ROOT / "fixtures" / "receivers" / "judgements"
RECEIVER_SET = ROOT / "fixtures" / "receivers" / "set.json"
INCIDENTS_DIR = ROOT / "intel" / "incidents"
FEED_STATE = ROOT / "intel" / "feed" / "state.json"
FEED_SNAPSHOT = ROOT / "intel" / "feed" / "follow-snapshot.json"
FLOWS_DIR = ROOT / "intel" / "flows"
LATENCY_RUNS = ROOT / "fixtures" / "latency" / "verdict-runs.json"
TIER2_LATENCY_RUNS = ROOT / "fixtures" / "latency" / "verdict-runs-tier2.json"
TIER2_FALLBACK_LATENCY_RUNS = ROOT / "fixtures" / "latency" / "verdict-runs-tier2-fallback.json"
START, END = "<!-- counts:start -->", "<!-- counts:end -->"


def receivers_line(directory=JUDGEMENTS_DIR):
    verdicts = [json.loads(path.read_text(encoding="utf-8"))["verdict"] for path in sorted(Path(directory).glob("*.json"))]
    return (f"On {len(verdicts)} recorded mainnet receivers the rules returned {verdicts.count('ALLOW')} ALLOW, "
            f"{verdicts.count('DENY')} DENY, {verdicts.count('UNKNOWN')} Unknown.")


def cohorts_line(set_path=RECEIVER_SET, directory=JUDGEMENTS_DIR):
    document = json.loads(Path(set_path).read_text(encoding="utf-8"))
    judgements = [json.loads(path.read_text(encoding="utf-8")) for path in sorted(Path(directory).glob("*.json"))]
    return intel_main.cohort_line(document, judgements)


def labels_line(store_path=labels.STORE_PATH):
    counts = labels.load(store_path).counts()
    p = counts["provenance"]
    return (f"Label store: {counts['assertions']} assertions over {counts['subjects']} subjects (onchain "
            f"{p['onchain']}, official_source {p['official_source']}, third_party {p['third_party']}, "
            f"inferred {p['inferred']}).")


def incidents_line(directory=INCIDENTS_DIR):
    return f"Incidents: {len(list(Path(directory).glob('*.json')))} pages."


def feed_line(state_path=FEED_STATE):
    path = FEED_SNAPSHOT if Path(state_path) == FEED_STATE and FEED_SNAPSHOT.is_file() else Path(state_path)
    if not path.is_file():
        return "Feed: not run."
    state = json.loads(path.read_text(encoding="utf-8"))
    signals = ", ".join(f"{count} {name}" for name, count in state["events"].items())
    if state.get("mode") == "follow":
        return (f"Feed: following since {state['started_at'][:10]}, {state['blocks']} blocks, "
                f"{sum(state['events'].values())} events ({signals}).")
    return (f"Feed: since {state['start_time'][:10]}, {state['blocks']} blocks, {sum(state['events'].values())} "
            f"events ({signals}).")


def flows_line(directory=FLOWS_DIR):
    traces = [json.loads(path.read_text(encoding="utf-8")) for path in sorted(Path(directory).glob("*.json"))]
    if not traces:
        return "Flows: 0 exit traces, 0 hops, depth 0 of 0, complete."
    least = min(traces, key=lambda t: (t["depth_reached"], t["completeness"] == "complete"))
    stopped = [t for t in traces if t["completeness"] != "complete"]
    state = "complete" if not stopped else trace.stop_state((least if least in stopped else stopped[0])["completeness"])
    return (f"Flows: {len(traces)} exit traces, {sum(len(t['hops']) for t in traces)} hops, depth "
            f"{least['depth_reached']} of {least['depth']}, {state}.")


def _latencies(path):
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, list):
        raise TypeError(f"{path}: a latency file holds a JSON list")
    runs = [item["latency_ms"] if isinstance(item, dict) else item for item in value]
    if not all(isinstance(ms, int) and not isinstance(ms, bool) for ms in runs):
        raise ValueError(f"{path}: every latency is an integer number of milliseconds")
    return runs


def _nearest_rank(ordered, percent):
    return ordered[max(1, math.ceil(percent / 100 * len(ordered))) - 1]


def _latency(label, path):
    runs = sorted(_latencies(path)) if Path(path).is_file() else []
    if not runs:
        return f"{label}: not measured."
    return (f"{label}: p50 {_nearest_rank(runs, 50)} ms, p95 {_nearest_rank(runs, 95)} ms over {len(runs)} "
            "verdict runs.")


def latency_line(path=LATENCY_RUNS):
    return _latency("Latency", path)


def tier2_latency_line(path=TIER2_LATENCY_RUNS):
    """The latency line of the payee off the allowlist, or None while its file does not exist."""
    return _latency("Latency, payee off the allowlist", path) if Path(path).is_file() else None


def tier2_fallback_latency_line(path=TIER2_FALLBACK_LATENCY_RUNS):
    """The latency line for the NearBlocks fallback path, or None while its file does not exist."""
    return (_latency("Latency, payee off the allowlist, NearBlocks fallback", path)
            if Path(path).is_file() else None)


def lines():
    tier2 = tier2_latency_line(TIER2_LATENCY_RUNS)
    fallback = tier2_fallback_latency_line(TIER2_FALLBACK_LATENCY_RUNS)
    return ([receivers_line(), cohorts_line(), labels_line(), incidents_line(), feed_line(), flows_line(),
             latency_line(LATENCY_RUNS)] + ([tier2] if tier2 else []) + ([fallback] if fallback else []))


def readme_block(count_lines):
    return START + "\n\n" + "".join(line + "\n\n" for line in count_lines) + END


def rewrite_readme(path, count_lines):
    """Replace the counts block of the file; False when the file has no markers."""
    text = Path(path).read_text(encoding="utf-8")
    start, end = text.find(START), text.find(END)
    if start == -1 or end == -1 or end < start:
        return False
    updated = text[:start] + readme_block(count_lines) + text[end + len(END):]
    Path(path).write_bytes(updated.encode("utf-8"))
    return True


def main(argv=None):
    parser = argparse.ArgumentParser(description="the counts the README states, from committed files")
    parser.add_argument("--readme", help="rewrite the counts block of this file")
    args = parser.parse_args(argv)
    count_lines = lines()
    if args.readme:
        if not rewrite_readme(args.readme, count_lines):
            print(f"{args.readme} has no {START} and {END} markers", file=sys.stderr)
            return 2
        return 0
    for line in count_lines:
        print(line)
    return 0


if __name__ == "__main__":
    sys.exit(main())
