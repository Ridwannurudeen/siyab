"""Score a folder of verdict files against the holdout receiver set.

Usage:
  python eval/score.py --verdicts DIR [--holdout FILE] [--json]

Each verdict file is one account's answer. The account is read from `subject`,
`account` or `receiver_id`, or else from the file name without `.json`. The
outcome is read from `verdict` when it is a string, else from `outcome`, and
UNKNOWN counts as ASK.

Precision and recall are about DENY against the bad label. ASK is counted on
its own and left out of both, so a cautious system is not scored as if it had
decided. Accounts with no verdict file are listed as not judged and left out.
"""

import argparse
import json
import sys
from pathlib import Path

LABELS = ("bad", "good")
DECISIONS = ("DENY", "ALLOW", "ASK")
DEFAULT_HOLDOUT = Path(__file__).resolve().parent / "holdout" / "receivers.jsonl"


def load_holdout(path):
    rows, seen, errors = [], {}, []
    with open(path, encoding="utf-8") as handle:
        for number, line in enumerate(handle, start=1):
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except ValueError as error:
                errors.append(f"line {number}: invalid JSON: {error}")
                continue
            if not isinstance(row, dict):
                errors.append(f"line {number}: not an object")
                continue
            account = row.get("account")
            label = row.get("label")
            source_url = row.get("source_url")
            note = row.get("note")
            if not isinstance(account, str) or not account:
                errors.append(f"line {number}: account is missing")
                continue
            if label not in LABELS:
                errors.append(f"line {number}: label must be bad or good")
                continue
            if not isinstance(source_url, str) or not source_url.startswith(("http://", "https://")):
                errors.append(f"line {number}: source_url must be an http or https URL")
                continue
            if not isinstance(note, str) or not note:
                errors.append(f"line {number}: note is missing")
                continue
            if account in seen:
                errors.append(f"line {number}: duplicate account {account} (first on line {seen[account]})")
                continue
            seen[account] = number
            rows.append({"account": account, "label": label, "source_url": source_url, "note": note})
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
        value = answer.get(key)
        if isinstance(value, str) and value:
            return value
    return path.stem


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
        account = account_of(path, answer)
        outcome = outcome_of(answer)
        if outcome is None:
            errors.append(f"{path}: no ALLOW, DENY or ASK outcome")
            continue
        if account in answers:
            duplicates.append(account)
        answers[account] = outcome
    return answers, errors, duplicates


def asserted_subjects(path):
    subjects = set()
    try:
        with open(path, encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                row = json.loads(line)
                if (isinstance(row, dict) and row.get("chain") == "near"
                        and row.get("category") in ("incident", "denylist")):
                    subjects.add(row.get("subject"))
    except (OSError, ValueError) as error:
        sys.stderr.write("label store " + str(error) + "\n")
    return subjects


def score(rows, answers):
    matrix = {(label, decision): 0 for label in LABELS for decision in DECISIONS}
    not_judged = []
    for row in rows:
        decision = answers.get(row["account"])
        if decision is None:
            not_judged.append(row["account"])
            continue
        matrix[(row["label"], decision)] += 1
    true_positive = matrix[("bad", "DENY")]
    false_positive = matrix[("good", "DENY")]
    false_negative = matrix[("bad", "ALLOW")]
    true_negative = matrix[("good", "ALLOW")]
    precision = true_positive / (true_positive + false_positive) if true_positive + false_positive else 0.0
    recall = true_positive / (true_positive + false_negative) if true_positive + false_negative else 0.0
    return {
        "matrix": {"bad": {d: matrix[("bad", d)] for d in DECISIONS},
                   "good": {d: matrix[("good", d)] for d in DECISIONS}},
        "precision": precision,
        "recall": recall,
        "counts": {
            "rows": len(rows),
            "bad": sum(1 for row in rows if row["label"] == "bad"),
            "good": sum(1 for row in rows if row["label"] == "good"),
            "judged": len(rows) - len(not_judged),
            "not_judged": len(not_judged),
            "ask": matrix[("bad", "ASK")] + matrix[("good", "ASK")],
        },
        "not_judged_accounts": not_judged,
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
        "               DENY  ALLOW    ASK",
        "bad       " + f"{bad['DENY']:>9}{bad['ALLOW']:>7}{bad['ASK']:>7}",
        "good      " + f"{good['DENY']:>9}{good['ALLOW']:>7}{good['ASK']:>7}",
        "",
        "precision (DENY vs bad): " + f"{result['precision']:.3f}" + " ("
        + str(bad["DENY"]) + "/" + str(bad["DENY"] + good["DENY"]) + ")",
        "recall (bad refused):    " + f"{result['recall']:.3f}" + " ("
        + str(bad["DENY"]) + "/" + str(bad["DENY"] + bad["ALLOW"]) + ")",
        "ASK kept apart:          " + str(bad["ASK"]) + " bad, " + str(good["ASK"])
        + " good, " + str(counts["ask"]) + " total",
    ]
    if counts["not_judged"]:
        lines.append("not judged:              " + str(counts["not_judged"])
                     + " (" + ", ".join(result["not_judged_accounts"][:5])
                     + (" ..." if counts["not_judged"] > 5 else "") + ")")
    return "\n".join(lines)


def main(argv=None):
    parser = argparse.ArgumentParser(description="Score verdict files against the holdout set")
    parser.add_argument("--verdicts", required=True, help="folder of verdict JSON files")
    parser.add_argument("--holdout", default=str(DEFAULT_HOLDOUT), help="holdout JSONL file")
    parser.add_argument("--json", action="store_true", dest="as_json", help="print the result as JSON")
    parser.add_argument("--exclude-labelled", action="store_true",
                        help="also report the score without accounts the current label store already asserts")
    parser.add_argument("--labels", default="intel/labels.jsonl", help="label store used by --exclude-labelled")
    args = parser.parse_args(argv)

    if not Path(args.holdout).is_file():
        parser.error("holdout file not found: " + args.holdout)
    if not Path(args.verdicts).is_dir():
        parser.error("verdict folder not found: " + args.verdicts)

    rows, errors = load_holdout(args.holdout)
    if errors:
        for error in errors:
            sys.stderr.write("holdout " + error + "\n")
        return 2
    answers, errors, duplicates = load_verdicts(args.verdicts)
    for error in errors:
        sys.stderr.write("verdict " + error + "\n")
    for account in sorted(set(duplicates)):
        sys.stderr.write("verdict duplicate for " + account + ", the last file wins\n")
    if not answers:
        sys.stderr.write("no usable verdict files in " + args.verdicts + "\n")
        return 2

    result = score(rows, answers)
    if args.exclude_labelled:
        labelled = asserted_subjects(args.labels)
        remaining = [row for row in rows if row["account"] not in labelled]
        result["excluding_label_store"] = {"subjects": len(labelled),
                                           "excluded_rows": len(rows) - len(remaining),
                                           "report": score(remaining, answers)}
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
