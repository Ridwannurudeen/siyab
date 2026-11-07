"""Generate a scorecard from the committed receiver, label, testnet, feed and claims files."""

import argparse
import json
import re
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import counts
from intel import labels

RECEIVER_SET = Path("fixtures/receivers/set.json")
JUDGEMENTS = Path("fixtures/receivers/judgements")
LABELS = Path("intel/labels.jsonl")
INCIDENTS = Path("intel/incidents")
FEED_SNAPSHOT = Path("intel/feed/follow-snapshot.json")
TESTNET = Path("docs/TESTNET.md")
REGISTRY = Path("fixtures/registry")
CLAIMS = Path("docs/CLAIMS.md")
SCORECARD = Path("docs/SCORECARD.md")
TEST_CITATION = re.compile(r"(?:\.test_[A-Za-z0-9_]+|::tests::[A-Za-z0-9_]+)")


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def parse_count_line(line, pattern, source):
    match = re.fullmatch(pattern, line)
    if match is None:
        raise ValueError(f"{source}: unexpected counts helper output")
    return match


def receiver_rows(root):
    receiver_set_path = root / RECEIVER_SET
    judgement_dir = root / JUDGEMENTS
    receiver_set = read_json(receiver_set_path)
    subjects = [item["subject"] for item in receiver_set["subjects"]]
    if len(subjects) != len(set(subjects)):
        raise ValueError(f"{RECEIVER_SET}: duplicate receiver subjects")
    judgement_paths = sorted(judgement_dir.glob("*.json"))
    if {path.stem for path in judgement_paths} != set(subjects):
        raise ValueError(f"{JUDGEMENTS}: judgement files do not match the receiver set")

    helper_line = counts.receivers_line(judgement_dir)
    match = parse_count_line(
        helper_line,
        r"On (?P<total>\d+) recorded mainnet receivers the rules returned "
        r"(?P<allow>\d+) ALLOW, (?P<deny>\d+) DENY, (?P<unknown>\d+) Unknown\.",
        "scripts/counts.py",
    )
    total = len(subjects)
    if int(match["total"]) != total:
        raise ValueError(f"{RECEIVER_SET}: receiver and judgement counts differ")

    outcomes = {name: int(match[name]) for name in ("allow", "deny", "unknown")}
    source = f"{RECEIVER_SET.as_posix()}; {JUDGEMENTS.as_posix()}/*.json"
    rows = [
        ("Receiver ALLOW", f"{outcomes['allow']:,} / {total:,} receivers", source),
        ("Receiver DENY", f"{outcomes['deny']:,} / {total:,} receivers", source),
        ("Receiver UNKNOWN", f"{outcomes['unknown']:,} / {total:,} receivers", source),
    ]

    unknown_causes = Counter()
    for path in judgement_paths:
        judgement = read_json(path)
        if judgement["verdict"] == "UNKNOWN":
            unknown_causes.update(
                check["id"] for check in judgement.get("checks", []) if check.get("status") == "unknown"
            )
    if unknown_causes:
        for cause, count in sorted(unknown_causes.items()):
            rows.append((
                f"UNKNOWN cause: {cause}",
                f"{count:,} / {outcomes['unknown']:,} UNKNOWN judgements; causes can overlap",
                source,
            ))
    else:
        return rows, [
            ("Receiver UNKNOWN causes", "Not measured: judgement files record no unknown-status check IDs",
             source)
        ]
    return rows, []


def label_rows(root):
    label_path = root / LABELS
    helper_line = counts.labels_line(label_path)
    match = re.match(r"Label store: (?P<total>\d+) assertions\b", helper_line)
    if match is None:
        raise ValueError("scripts/counts.py: unexpected label counts helper output")
    total = int(match["total"])
    categories = labels.load(label_path).counts()["category"]
    hostile = categories["incident"] + categories["denylist"]
    reference = total - hostile
    if sum(categories.values()) != total:
        raise ValueError(f"{LABELS}: category counts do not sum to the assertion total")
    return [
        ("Label assertions", f"{total:,} / {total:,} committed assertion rows", LABELS.as_posix()),
        ("Hostile assertions (incident and denylist)", f"{hostile:,} / {total:,} assertions",
         f"{LABELS.as_posix()}; {INCIDENTS.as_posix()}/*.json"),
        ("Reference assertions (all other categories)", f"{reference:,} / {total:,} assertions",
         LABELS.as_posix()),
    ]


def markdown_cells(line):
    stripped = line.strip()
    if not stripped.startswith("|") or not stripped.endswith("|"):
        raise ValueError("table row is not pipe-delimited")
    return [cell.strip() for cell in stripped[1:-1].split("|")]


def parse_table(text, section_prefix, expected_header, source):
    lines = text.splitlines()
    section_starts = [
        index for index, line in enumerate(lines)
        if re.match(r"^#{1,6} ", line) and line.lstrip("# ").startswith(section_prefix)
    ]
    if len(section_starts) != 1:
        raise ValueError(f"{source}: expected one {section_prefix} section")
    start = section_starts[0] + 1
    end = next(
        (index for index in range(start, len(lines)) if re.match(r"^#{1,6} ", lines[index])),
        len(lines),
    )
    section_lines = lines[start:end]
    header_positions = []
    for index, line in enumerate(section_lines):
        if line.strip().startswith("|"):
            try:
                if markdown_cells(line) == expected_header:
                    header_positions.append(index)
            except ValueError:
                continue
    if len(header_positions) != 1:
        raise ValueError(f"{source}: expected one table with columns {', '.join(expected_header)}")
    header_index = header_positions[0]
    if header_index + 1 >= len(section_lines):
        raise ValueError(f"{source}: table has no separator row")
    separator = markdown_cells(section_lines[header_index + 1])
    if len(separator) != len(expected_header) or not all(
        re.fullmatch(r":?-{3,}:?", cell) for cell in separator
    ):
        raise ValueError(f"{source}: table separator is malformed")

    rows = []
    for line in section_lines[header_index + 2:]:
        if not line.strip().startswith("|"):
            break
        cells = markdown_cells(line)
        if len(cells) != len(expected_header) or not all(cells):
            raise ValueError(f"{source}: a table row has missing or extra cells")
        rows.append(cells)
    if not rows:
        raise ValueError(f"{source}: table has no data rows")
    return rows


def testnet_rows(root):
    source = TESTNET.as_posix()
    text = (root / TESTNET).read_text(encoding="utf-8")
    rows = []
    not_measured = []
    try:
        payments = parse_table(text, "Payments", ["Payment", "Verdict", "Transaction"], source)
        rows.append(("Testnet payments", f"{len(payments):,} / {len(payments):,} payment table rows", source))
    except ValueError as error:
        not_measured.append(("Testnet payments", f"Not measured: {error}", source))
    try:
        refusals = parse_table(text, "Attack runs", ["Attack", "Refused by", "Transaction"], source)
        if not all(row[1] for row in refusals):
            raise ValueError(f"{source}: an attack row has no refusal entry")
        rows.append(("Testnet refusals", f"{len(refusals):,} / {len(refusals):,} attack table rows", source))
    except ValueError as error:
        not_measured.append(("Testnet refusals", f"Not measured: {error}", source))
    return rows, not_measured


def registry_rows(root):
    registry_dir = root / REGISTRY
    record_files = []
    for path in sorted(registry_dir.glob("testnet-*.json")):
        record = read_json(path)
        if record.get("event") == "verdict_recorded":
            entries = record.get("data")
            if not isinstance(entries, list) or not entries:
                raise ValueError(f"{path}: verdict event has no record entries")
            count = len(entries)
        elif "verdict" in record:
            count = 1
        else:
            raise ValueError(f"{path}: unrecognized testnet registry record")
        record_files.append((path, count))

    for path in sorted(registry_dir.glob("event-*.json")):
        event = read_json(path)
        if event.get("event") != "verdict_recorded":
            continue
        entries = event.get("data")
        if not isinstance(entries, list) or not entries:
            raise ValueError(f"{path}: verdict event has no record entries")
        if not all(isinstance(entry.get("subject"), str) and entry["subject"].endswith(".testnet")
                   for entry in entries):
            continue
        record_files.append((path, len(entries)))

    record_files.sort(key=lambda item: item[0].name)
    record_count = sum(count for _, count in record_files)
    source = "; ".join(path.relative_to(root).as_posix() for path, _ in record_files)
    if not source:
        source = REGISTRY.as_posix()
    return [
        ("Testnet registry records", f"{record_count:,} record entries / {len(record_files):,} fixture files",
         source)
    ]


def feed_rows(root):
    snapshot_path = root / FEED_SNAPSHOT
    snapshot = read_json(snapshot_path)
    helper_line = counts.feed_line(snapshot_path)
    match = parse_count_line(
        helper_line,
        r"Feed: (?:following )?since (?P<start>\d{4}-\d{2}-\d{2}), "
        r"(?P<blocks>\d+) blocks, (?P<events>\d+) events \(.*\)\.",
        "scripts/counts.py",
    )
    blocks = int(match["blocks"])
    events = int(match["events"])
    if snapshot["mode"] != "follow":
        raise ValueError(f"{FEED_SNAPSHOT}: expected a follow snapshot")
    if not isinstance(snapshot.get("last_time"), str):
        raise ValueError(f"{FEED_SNAPSHOT}: last_time is missing")
    source = FEED_SNAPSHOT.as_posix()
    return [
        ("Feed blocks followed", f"{blocks:,} / {blocks:,} blocks recorded by the snapshot", source),
        ("Feed events since start", f"{events:,} / {blocks:,} followed blocks", source),
        ("Feed snapshot interval", f"{snapshot['started_at']} to {snapshot['last_time']}", source),
        ("Feed basis", "snapshot, not live", source),
    ]


def claims_rows(root):
    source = CLAIMS.as_posix()
    text = (root / CLAIMS).read_text(encoding="utf-8")
    claims = parse_table(text, "Claims to code", ["Claim", "File:function", "Test", "Evidence", "idea"], source)
    tested = sum(bool(TEST_CITATION.search(row[2])) for row in claims)
    return [
        ("Claims rows", f"{len(claims):,} / {len(claims):,} parsed claim rows", source),
        ("Claims citing a test", f"{tested:,} / {len(claims):,} claim rows", source),
    ]


def render(root=ROOT):
    root = Path(root)
    measured = []
    not_measured = []

    receiver_metrics, receiver_not_measured = receiver_rows(root)
    measured.extend(receiver_metrics)
    not_measured.extend(receiver_not_measured)
    measured.extend(label_rows(root))
    testnet_metrics, testnet_not_measured = testnet_rows(root)
    measured.extend(testnet_metrics)
    not_measured.extend(testnet_not_measured)
    measured.extend(registry_rows(root))
    measured.extend(feed_rows(root))
    try:
        measured.extend(claims_rows(root))
    except ValueError as error:
        not_measured.append(("Claims rows and test citations", f"Not measured: {error}", CLAIMS.as_posix()))

    not_measured.append((
        "Test method count",
        "Not measured: scripts/counts.py does not count test methods",
        "scripts/counts.py",
    ))

    lines = [
        "# Scorecard",
        "",
        "This scorecard measures committed receiver, label, testnet, feed and claims data; every number is "
        "generated by scripts/scorecard.py from committed files.",
        "",
        "These counts are not a precision or recall measurement against any outside ground truth.",
        "",
        "## Measured",
        "",
        "| Measure | Numerator / denominator | Source |",
        "| --- | --- | --- |",
    ]
    lines.extend(f"| {name} | {value} | {source} |" for name, value, source in measured)
    lines.extend([
        "",
        "## Not measured",
        "",
        "| Measure | Reason | Source |",
        "| --- | --- | --- |",
    ])
    lines.extend(f"| {name} | {reason} | {source} |" for name, reason, source in not_measured)
    return "\n".join(lines) + "\n"


def main(argv=None):
    parser = argparse.ArgumentParser(description="generate or check docs/SCORECARD.md")
    parser.add_argument("--check", action="store_true", help="exit 1 if docs/SCORECARD.md is stale")
    args = parser.parse_args(argv)
    target = ROOT / SCORECARD
    generated = render()
    if args.check:
        if not target.is_file() or target.read_text(encoding="utf-8") != generated:
            print(f"{SCORECARD.as_posix()} differs from a fresh render", file=sys.stderr)
            return 1
        return 0
    target.write_bytes(generated.encode("utf-8"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
