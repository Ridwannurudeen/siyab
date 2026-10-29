"""Print the counts the README states, from committed files only, in a fixed format.

Usage:
  python scripts/counts.py                    print the five lines
  python scripts/counts.py --readme README.md rewrite the block between the counts markers

The five lines, in this order (every number is counted from the files named):

  On N recorded mainnet receivers the rules returned A ALLOW, D DENY, U Unknown.
      fixtures/receivers/judgements/*.json, one judgement per subject
  Label store: L assertions over E subjects (onchain O, official_source F, third_party T, inferred I).
      intel/labels.jsonl; E counts distinct (chain, subject) pairs
  Incidents: I pages.
      intel/incidents/*.json
  Feed: since D, B blocks, C events (X code_hash_matches_incident, Y fresh_dust_funded_pays_labelled,
  Z watch_account_key_added).
      intel/feed/state.json: its start date, block count and event count per signal, in the file's
      order; "Feed: not run." while that file does not exist
  Latency: p50 X ms, p95 Y ms over R verdict runs.
      fixtures/latency/*.json, each a JSON list of integers or of objects with an integer latency_ms;
      the percentiles are nearest rank; "Latency: not measured." while no such file exists

--readme replaces everything between the lines <!-- counts:start --> and <!-- counts:end --> with the
five lines, each followed by a blank line so each renders as its own paragraph, and exits 2 when the
file lacks the markers.
"""

import argparse
import json
import math
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from intel import labels

JUDGEMENTS_DIR = ROOT / "fixtures" / "receivers" / "judgements"
INCIDENTS_DIR = ROOT / "intel" / "incidents"
FEED_STATE = ROOT / "intel" / "feed" / "state.json"
LATENCY_DIR = ROOT / "fixtures" / "latency"
START, END = "<!-- counts:start -->", "<!-- counts:end -->"


def receivers_line(directory=JUDGEMENTS_DIR):
    verdicts = [json.loads(path.read_text(encoding="utf-8"))["verdict"] for path in sorted(Path(directory).glob("*.json"))]
    return (f"On {len(verdicts)} recorded mainnet receivers the rules returned {verdicts.count('ALLOW')} ALLOW, "
            f"{verdicts.count('DENY')} DENY, {verdicts.count('UNKNOWN')} Unknown.")


def labels_line(store_path=labels.STORE_PATH):
    counts = labels.load(store_path).counts()
    p = counts["provenance"]
    return (f"Label store: {counts['assertions']} assertions over {counts['subjects']} subjects (onchain "
            f"{p['onchain']}, official_source {p['official_source']}, third_party {p['third_party']}, "
            f"inferred {p['inferred']}).")


def incidents_line(directory=INCIDENTS_DIR):
    return f"Incidents: {len(list(Path(directory).glob('*.json')))} pages."


def feed_line(state_path=FEED_STATE):
    path = Path(state_path)
    if not path.is_file():
        return "Feed: not run."
    state = json.loads(path.read_text(encoding="utf-8"))
    signals = ", ".join(f"{count} {name}" for name, count in state["events"].items())
    return (f"Feed: since {state['start_time'][:10]}, {state['blocks']} blocks, {sum(state['events'].values())} "
            f"events ({signals}).")


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


def latency_line(directory=LATENCY_DIR):
    runs = sorted(ms for path in sorted(Path(directory).glob("*.json")) for ms in _latencies(path))
    if not runs:
        return "Latency: not measured."
    return (f"Latency: p50 {_nearest_rank(runs, 50)} ms, p95 {_nearest_rank(runs, 95)} ms over {len(runs)} "
            "verdict runs.")


def lines():
    return [receivers_line(), labels_line(), incidents_line(), feed_line(), latency_line()]


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
