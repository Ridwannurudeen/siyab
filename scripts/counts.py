"""Print the counts the README states, from committed files only, in a fixed format.

Usage:
  python scripts/counts.py                    print the lines
  python scripts/counts.py --readme README.md rewrite the block between the counts markers
  python scripts/counts.py --unknown-rate     print the pinned receiver UNKNOWN series
  python scripts/counts.py --unknown-rate-row print a row from hackathon-submission
  python scripts/counts.py --append-unknown-rate PATH --new-series start a series
  python scripts/counts.py --append-unknown-rate PATH --revision HEAD append a committed observation

UNKNOWN rows carry observed (the UTC date in the judgements), commit and commit_date (the UTC
committer date). Their sample id is the SHA256 of the sorted pinned subject list serialized as
compact JSON with separators (",", ":"), encoded as UTF-8, with no time fields or trailing newline.
A new sample requires --new-series and an empty destination; existing series are only appended to.
Duplicates match sample_id, observed and commit; neither date may precede the last row's date.

The lines, in this order (every number is counted from the files named); the two off-allowlist lines are
printed only while their files exist, so there are seven to nine (each count names its noun in the
singular when it is 1):

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

--unknown-rate prints only:
  UNKNOWN on the pinned N: P% (reads of D Mon, rules of D Mon); P% (reads of D Mon, rules of D Mon).
      fixtures/receivers/unknown-rate.jsonl; rounded whole percentages, one entry per row
It is excluded from lines() and --readme output.

--readme replaces everything between the lines <!-- counts:start --> and <!-- counts:end --> with the
lines, each followed by a blank line so each renders as its own paragraph, and exits 2 when the file
lacks the markers.
"""

import argparse
import hashlib
import io
import json
import math
import subprocess
import sys
import tarfile
from collections import Counter
from datetime import date, datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from intel import __main__ as intel_main
from intel import labels, trace

JUDGEMENTS_DIR = ROOT / "fixtures" / "receivers" / "judgements"
RECEIVER_SET = ROOT / "fixtures" / "receivers" / "set.json"
UNKNOWN_RATE = ROOT / "fixtures" / "receivers" / "unknown-rate.jsonl"
INCIDENTS_DIR = ROOT / "intel" / "incidents"
FEED_STATE = ROOT / "intel" / "feed" / "state.json"
FEED_SNAPSHOT = ROOT / "intel" / "feed" / "follow-snapshot.json"
FLOWS_DIR = ROOT / "intel" / "flows"
LATENCY_RUNS = ROOT / "fixtures" / "latency" / "verdict-runs.json"
TIER2_LATENCY_RUNS = ROOT / "fixtures" / "latency" / "verdict-runs-tier2.json"
TIER2_FALLBACK_LATENCY_RUNS = ROOT / "fixtures" / "latency" / "verdict-runs-tier2-fallback.json"
START, END = "<!-- counts:start -->", "<!-- counts:end -->"


def _count(number, noun):
    return f"{number} {noun}" + ("" if number == 1 else "s")


def receivers_line(directory=JUDGEMENTS_DIR):
    verdicts = [json.loads(path.read_text(encoding="utf-8"))["verdict"] for path in sorted(Path(directory).glob("*.json"))]
    return (f"On {_count(len(verdicts), 'recorded mainnet receiver')} the rules returned {verdicts.count('ALLOW')} ALLOW, "
            f"{verdicts.count('DENY')} DENY, {verdicts.count('UNKNOWN')} Unknown.")


def cohorts_line(set_path=RECEIVER_SET, directory=JUDGEMENTS_DIR):
    document = json.loads(Path(set_path).read_text(encoding="utf-8"))
    judgements = [json.loads(path.read_text(encoding="utf-8")) for path in sorted(Path(directory).glob("*.json"))]
    return intel_main.cohort_line(document, judgements)


def unknown_rate_row(revision="hackathon-submission"):
    """Reproduce a local revision's row; sample_id hashes only compact JSON of sorted subjects,
    encoded as UTF-8 without a trailing newline, excluding chosen_at and judged_at_ms.
    """
    commit = subprocess.run(["git", "rev-parse", "--verify", f"{revision}^{{commit}}"], cwd=ROOT, check=True,
                            capture_output=True, text=True).stdout.strip()
    committed_at = subprocess.run(["git", "show", "-s", "--format=%cI", commit], cwd=ROOT, check=True,
                                 capture_output=True, text=True).stdout.strip()
    commit_date = datetime.fromisoformat(committed_at).astimezone(timezone.utc).date().isoformat()
    sample_path = "fixtures/receivers/top.json"
    directory = "fixtures/receivers/top/judgements/"
    archive = subprocess.run(["git", "archive", commit, sample_path, directory], cwd=ROOT,
                             check=True, capture_output=True).stdout
    with tarfile.open(fileobj=io.BytesIO(archive)) as tree:
        sample = tree.extractfile(sample_path).read()
        judgements = [json.load(tree.extractfile(member)) for member in tree.getmembers()
                      if member.isfile() and member.name.startswith(directory) and member.name.endswith(".json")]
    subjects = [entry["subject"] for entry in json.loads(sample)["subjects"]]
    judged = [row["subject"] for row in judgements]
    if len(set(subjects)) != len(subjects) or Counter(subjects) != Counter(judged):
        raise ValueError("judgements must cover each pinned receiver exactly once")
    dates = {datetime.fromtimestamp(row["observed"]["timestamp_ms"] / 1000, timezone.utc).date().isoformat()
             for row in judgements}
    if len(dates) != 1:
        raise ValueError("judgements must share one UTC observed date")
    verdicts = Counter(row["verdict"] for row in judgements)
    if set(verdicts) - {"ALLOW", "DENY", "UNKNOWN"}:
        raise ValueError("unexpected judgement verdict")
    causes = Counter()
    for row in judgements:
        if row["verdict"] == "UNKNOWN":
            causes.update([check["id"] for check in row["checks"] if check["status"] == "unknown"]
                          or ["unspecified"])
    canonical = json.dumps(sorted(subjects), separators=(",", ":")).encode("utf-8")
    return {"observed": dates.pop(), "commit": commit, "commit_date": commit_date,
            "sample_id": hashlib.sha256(canonical).hexdigest(),
            "totals": {**{name: verdicts[name] for name in ("ALLOW", "DENY", "UNKNOWN")}, "total": len(subjects)},
            "causes": dict(sorted(causes.items()))}


def append_unknown_rate(path, row, new_series=False):
    """Append without rewriting any byte; a new sample starts in a separate, empty file."""
    path = Path(path)
    previous = path.read_bytes() if path.is_file() else b""
    if new_series:
        if previous:
            raise ValueError("a new series requires an empty destination")
    elif not previous:
        raise ValueError("start a new series explicitly with --new-series")
    rows = [json.loads(line) for line in previous.splitlines()]
    if any(old["sample_id"] != row["sample_id"] for old in rows):
        raise ValueError("different sample id; start a new series in a separate file")
    if any((old["sample_id"], old["observed"], old["commit"]) ==
           (row["sample_id"], row["observed"], row["commit"]) for old in rows):
        raise ValueError("sample id, observed date and commit already recorded")
    if rows and date.fromisoformat(row["commit_date"]) < date.fromisoformat(rows[-1]["commit_date"]):
        raise ValueError("commit date is earlier than the last row")
    if rows and date.fromisoformat(row["observed"]) < date.fromisoformat(rows[-1]["observed"]):
        raise ValueError("observed date is earlier than the last row")
    if previous and not previous.endswith(b"\n"):
        raise ValueError("existing series must end with a newline")
    with path.open("ab") as destination:
        destination.write((json.dumps(row, sort_keys=True) + "\n").encode("utf-8"))


def unknown_rate_line(path=UNKNOWN_RATE):
    rows = [json.loads(line) for line in Path(path).read_text(encoding="utf-8").splitlines()]
    if not rows or len({(row["sample_id"], row["totals"]["total"]) for row in rows}) != 1:
        raise ValueError("an UNKNOWN series must contain one pinned sample")
    months = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")
    entries = []
    for row in rows:
        observed = date.fromisoformat(row["observed"])
        committed = date.fromisoformat(row["commit_date"])
        percent = math.floor(100 * row["totals"]["UNKNOWN"] / row["totals"]["total"] + 0.5)
        entries.append(f"{percent}% (reads of {observed.day} {months[observed.month - 1]}, "
                       f"rules of {committed.day} {months[committed.month - 1]})")
    return f"UNKNOWN on the pinned {rows[0]['totals']['total']}: " + "; ".join(entries) + "."


def labels_line(store_path=labels.STORE_PATH):
    counts = labels.load(store_path).counts()
    p = counts["provenance"]
    return (f"Label store: {_count(counts['assertions'], 'assertion')} over {_count(counts['subjects'], 'subject')} (onchain "
            f"{p['onchain']}, official_source {p['official_source']}, third_party {p['third_party']}, "
            f"inferred {p['inferred']}).")


def incidents_line(directory=INCIDENTS_DIR):
    return f"Incidents: {_count(len(list(Path(directory).glob('*.json'))), 'page')}."


def feed_line(state_path=FEED_STATE):
    path = FEED_SNAPSHOT if Path(state_path) == FEED_STATE and FEED_SNAPSHOT.is_file() else Path(state_path)
    if not path.is_file():
        return "Feed: not run."
    state = json.loads(path.read_text(encoding="utf-8"))
    signals = ", ".join(f"{count} {name}" for name, count in state["events"].items())
    if state.get("mode") == "follow":
        return (f"Feed: following since {state['started_at'][:10]}, {_count(state['blocks'], 'block')}, "
                f"{_count(sum(state['events'].values()), 'event')} ({signals}).")
    return (f"Feed: since {state['start_time'][:10]}, {_count(state['blocks'], 'block')}, "
            f"{_count(sum(state['events'].values()), 'event')} ({signals}).")


def flows_line(directory=FLOWS_DIR):
    traces = [json.loads(path.read_text(encoding="utf-8")) for path in sorted(Path(directory).glob("*.json"))]
    if not traces:
        return "Flows: 0 exit traces, 0 hops, depth 0 of 0, complete."
    least = min(traces, key=lambda t: (t["depth_reached"], t["completeness"] == "complete"))
    stopped = [t for t in traces if t["completeness"] != "complete"]
    state = "complete" if not stopped else trace.stop_state((least if least in stopped else stopped[0])["completeness"])
    return (f"Flows: {_count(len(traces), 'exit trace')}, {_count(sum(len(t['hops']) for t in traces), 'hop')}, depth "
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
    return (f"{label}: p50 {_nearest_rank(runs, 50)} ms, p95 {_nearest_rank(runs, 95)} ms over "
            f"{_count(len(runs), 'verdict run')}.")


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
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--readme", help="rewrite the counts block of this file")
    mode.add_argument("--unknown-rate", action="store_true", help="print the pinned receiver UNKNOWN series")
    mode.add_argument("--unknown-rate-row", action="store_true", help="print a reproducible UNKNOWN row")
    mode.add_argument("--append-unknown-rate", metavar="PATH", help="append a reproducible UNKNOWN row")
    parser.add_argument("--revision", default="hackathon-submission", help="local git revision for UNKNOWN rows")
    parser.add_argument("--new-series", action="store_true", help="explicitly start a series in an empty file")
    args = parser.parse_args(argv)
    if args.new_series and not args.append_unknown_rate:
        parser.error("--new-series requires --append-unknown-rate")
    if args.unknown_rate_row or args.append_unknown_rate:
        try:
            row = unknown_rate_row(args.revision)
            if args.append_unknown_rate:
                append_unknown_rate(args.append_unknown_rate, row, args.new_series)
            else:
                print(json.dumps(row, sort_keys=True))
        except (ValueError, OSError, subprocess.CalledProcessError) as error:
            print(str(error), file=sys.stderr)
            return 2
        return 0
    if args.unknown_rate:
        print(unknown_rate_line())
        return 0
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
