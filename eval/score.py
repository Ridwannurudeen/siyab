"""Score a folder of verdict files against the holdout receiver set.

Usage:
  python eval/score.py --verdicts DIR [--holdout FILE] [--json]

Each verdict file is one account's answer. Evidence uses subject.receiver and
service responses use request.receiver_id. Simple verdict files may name the
account in subject, account, receiver_id or their file name. UNKNOWN counts as
ASK. ALLOW, DENY and ASK keep their columns in every document shape; the service
action does not change the decision being scored.

Precision and recall are about DENY against the bad label. Every bad row is in
the recall denominator, including ASK and rows without a verdict. ASK, missing
verdicts and accounts outside the holdout are reported separately.
"""

import argparse
import hashlib
import json
import subprocess
import sys
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from gate import canon  # noqa: E402
from intel import labels  # noqa: E402

LABELS = ("bad", "good")
DECISIONS = ("DENY", "ALLOW", "ASK")
COLUMNS = DECISIONS + ("NOT_JUDGED",)
DEFAULT_HOLDOUT = Path(__file__).resolve().parent / "holdout" / "receivers.jsonl"


def load_holdout(path, *, data=None):
    rows, seen, errors = [], {}, []
    if data is None:
        data = Path(path).read_bytes()
    for number, line in enumerate(data.splitlines(), start=1):
        if not line.strip():
            continue
        try:
            row = json.loads(line.decode("utf-8"))
        except ValueError as error:
            errors.append(f"line {number}: invalid JSON or UTF-8: {error}")
            continue
        if not isinstance(row, dict):
            errors.append(f"line {number}: not an object")
            continue
        account = row.get("account")
        label = row.get("label")
        source_url = row.get("source_url")
        note = row.get("note")
        try:
            canon.account_id(account, "account")
        except canon.CanonError as error:
            errors.append(f"line {number}: {error}")
            continue
        if account in seen:
            errors.append(f"line {number}: duplicate account {account} (first on line {seen[account]})")
            continue
        seen[account] = number
        if label not in LABELS:
            errors.append(f"line {number}: label must be bad or good")
            continue
        if label == "bad":
            try:
                source = urlparse(source_url) if isinstance(source_url, str) else None
                valid_source = (source is not None and source.scheme in ("http", "https")
                                and source.hostname and not any(char.isspace() for char in source_url))
            except ValueError:
                valid_source = False
            if not valid_source:
                errors.append(f"line {number}: source_url must be an http or https URL with a host")
                continue
        if label == "good" and (not isinstance(note, str) or not note.strip()):
            errors.append(f"line {number}: note is missing")
            continue
        rows.append({"account": account, "label": label, "source_url": source_url, "note": note})
    if not rows and not errors:
        errors.append("no holdout rows")
    return rows, errors


def outcome_of(answer):
    value = answer.get("verdict")
    if not isinstance(value, str):
        value = answer.get("outcome")
    if not isinstance(value, str):
        return None
    value = value.strip().upper()
    if value == "UNKNOWN":
        return "ASK"
    return value if value in DECISIONS else None


def account_of(path, answer):
    for key in ("subject", "account", "receiver_id"):
        if key not in answer:
            continue
        value = answer[key]
        if key == "subject" and isinstance(value, dict):
            value = value.get("receiver")
            key = "subject.receiver"
        return canon.account_id(value, key)
    if "request" in answer:
        request = answer["request"]
        value = request.get("receiver_id") if isinstance(request, dict) else None
        return canon.account_id(value, "request.receiver_id")
    return canon.account_id(path.stem, "filename")


def load_verdicts(folder):
    answers, errors, duplicates = {}, [], []
    for path in sorted(Path(folder).rglob("*.json")):
        try:
            answer = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as error:
            errors.append(f"{path}: {error}")
            continue
        if not isinstance(answer, dict):
            errors.append(f"{path}: not an object")
            continue
        try:
            account = account_of(path, answer)
        except ValueError as error:
            errors.append(f"{path}: {error}")
            continue
        outcome = outcome_of(answer)
        if outcome is None:
            errors.append(f"{path}: no ALLOW, DENY or ASK outcome")
            continue
        if account in answers:
            duplicates.append(account)
            continue
        answers[account] = outcome
    return answers, errors, duplicates


def asserted_subjects(path):
    store = labels.load(path)
    if not store.assertions:
        raise ValueError(f"{path}: no assertions")
    return {row.subject for row in store.assertions
            if row.chain == "near" and row.category in ("incident", "denylist") and row.superseded_by is None}


def score(rows, answers):
    matrix = {(label, decision): 0 for label in LABELS for decision in COLUMNS}
    not_judged = []
    for row in rows:
        decision = answers.get(row["account"], "NOT_JUDGED")
        if decision == "NOT_JUDGED":
            not_judged.append(row["account"])
        matrix[(row["label"], decision)] += 1
    true_positive = matrix[("bad", "DENY")]
    false_positive = matrix[("good", "DENY")]
    false_negative = sum(matrix[("bad", decision)] for decision in COLUMNS if decision != "DENY")
    true_negative = sum(matrix[("good", decision)] for decision in COLUMNS if decision != "DENY")
    precision = true_positive / (true_positive + false_positive) if true_positive + false_positive else 0.0
    recall = true_positive / (true_positive + false_negative) if true_positive + false_negative else 0.0
    outside_holdout = sorted(set(answers) - {row["account"] for row in rows})
    return {
        "matrix": {"bad": {d: matrix[("bad", d)] for d in COLUMNS},
                   "good": {d: matrix[("good", d)] for d in COLUMNS}},
        "confusion": {"true_positive": true_positive, "false_positive": false_positive,
                      "false_negative": false_negative, "true_negative": true_negative},
        "precision": precision,
        "recall": recall,
        "counts": {
            "rows": len(rows),
            "bad": sum(1 for row in rows if row["label"] == "bad"),
            "good": sum(1 for row in rows if row["label"] == "good"),
            "judged": len(rows) - len(not_judged),
            "not_judged": len(not_judged),
            "ask": matrix[("bad", "ASK")] + matrix[("good", "ASK")],
            "outside_holdout": len(outside_holdout),
        },
        "not_judged_accounts": not_judged,
        "outside_holdout_accounts": outside_holdout,
    }


def format_report(result):
    counts = result["counts"]
    bad = result["matrix"]["bad"]
    good = result["matrix"]["good"]
    lines = [
        "holdout: " + str(counts["rows"]) + " rows (" + str(counts["bad"]) + " bad, "
        + str(counts["good"]) + " good)",
        "verdicts: " + str(counts["judged"]) + " judged, " + str(counts["not_judged"]) + " not judged",
        "",
        "               DENY  ALLOW    ASK  NOT_JUDGED",
        "bad       " + f"{bad['DENY']:>9}{bad['ALLOW']:>7}{bad['ASK']:>7}{bad['NOT_JUDGED']:>12}",
        "good      " + f"{good['DENY']:>9}{good['ALLOW']:>7}{good['ASK']:>7}{good['NOT_JUDGED']:>12}",
        "",
        "true positive (bad DENY):         " + str(result["confusion"]["true_positive"]),
        "false positive (good DENY):       " + str(result["confusion"]["false_positive"]),
        "false negative (bad without DENY): " + str(result["confusion"]["false_negative"]),
        "true negative (good without DENY): " + str(result["confusion"]["true_negative"]),
        "",
        "precision (DENY vs bad): " + f"{result['precision']:.3f}" + " ("
        + str(bad["DENY"]) + "/" + str(bad["DENY"] + good["DENY"]) + ")",
        "recall (bad refused):    " + f"{result['recall']:.3f}" + " ("
        + str(bad["DENY"]) + "/" + str(counts["bad"]) + ")",
        "ASK kept apart:          " + str(bad["ASK"]) + " bad, " + str(good["ASK"])
        + " good, " + str(counts["ask"]) + " total",
        "not judged kept apart:   " + str(bad["NOT_JUDGED"]) + " bad, " + str(good["NOT_JUDGED"]) + " good",
        "outside holdout:         " + str(counts["outside_holdout"]),
    ]
    if "holdout_sha256" in result:
        lines[:0] = ["holdout sha256: " + result["holdout_sha256"], "rules commit: " + result["rules_commit"], ""]
    if counts["not_judged"]:
        lines.append("not judged:              " + str(counts["not_judged"])
                     + " (" + ", ".join(result["not_judged_accounts"]) + ")")
    if counts["outside_holdout"]:
        lines.append("outside accounts:        " + ", ".join(result["outside_holdout_accounts"]))
    return "\n".join(lines)


def main(argv=None):
    parser = argparse.ArgumentParser(description="Score verdict files against the holdout set")
    parser.add_argument("--verdicts", required=True, help="folder of verdict JSON files")
    parser.add_argument("--holdout", default=str(DEFAULT_HOLDOUT), help="holdout JSONL file")
    parser.add_argument("--json", action="store_true", dest="as_json", help="print the result as JSON")
    parser.add_argument("--exclude-labelled", action="store_true",
                        help="also report the score without accounts the current label store already asserts")
    parser.add_argument("--labels", default=str(ROOT / "intel" / "labels.jsonl"),
                        help="label store used by --exclude-labelled")
    parser.add_argument("--rules-commit", default="HEAD", help="Git revision that produced the verdicts (default: HEAD)")
    args = parser.parse_args(argv)

    if not Path(args.holdout).is_file():
        parser.error("holdout file not found: " + args.holdout)
    if not Path(args.verdicts).is_dir():
        parser.error("verdict folder not found: " + args.verdicts)

    try:
        holdout_data = Path(args.holdout).read_bytes()
    except OSError as error:
        sys.stderr.write("holdout " + str(error) + "\n")
        return 2
    rows, errors = load_holdout(args.holdout, data=holdout_data)
    if errors:
        for error in errors:
            sys.stderr.write("holdout " + error + "\n")
        return 2
    answers, errors, duplicates = load_verdicts(args.verdicts)
    for error in errors:
        sys.stderr.write("verdict " + error + "\n")
    for account in sorted(set(duplicates)):
        sys.stderr.write("verdict duplicate for " + account + "\n")
    if errors or duplicates:
        return 2

    if args.exclude_labelled:
        try:
            labelled = asserted_subjects(args.labels)
        except (OSError, ValueError) as error:
            sys.stderr.write("label store " + str(error) + "\n")
            return 2
    try:
        revision = subprocess.run(
            ["git", "rev-parse", "--verify", "--end-of-options", args.rules_commit + "^{commit}"],
            cwd=ROOT, capture_output=True, text=True, check=True, timeout=10,
        )
    except (OSError, subprocess.SubprocessError) as error:
        sys.stderr.write("rules commit " + str(error) + "\n")
        return 2

    result = score(rows, answers)
    result["holdout_sha256"] = hashlib.sha256(holdout_data).hexdigest()
    result["rules_commit"] = revision.stdout.strip()
    if args.exclude_labelled:
        excluded = {row["account"] for row in rows if row["account"] in labelled}
        remaining = [row for row in rows if row["account"] not in labelled]
        remaining_answers = {account: decision for account, decision in answers.items() if account not in excluded}
        result["excluding_label_store"] = {"subjects": len(labelled),
                                           "excluded_rows": len(rows) - len(remaining),
                                           "report": score(remaining, remaining_answers)}
    if args.as_json:
        sys.stdout.write(json.dumps(result, indent=1, sort_keys=True) + "\n")
    else:
        sys.stdout.write(format_report(result) + "\n")
        if args.exclude_labelled:
            extra = result["excluding_label_store"]
            sys.stdout.write("\nwithout " + str(extra["excluded_rows"])
                             + " rows the current label store already asserts:\n")
            sys.stdout.write(format_report(extra["report"]) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
