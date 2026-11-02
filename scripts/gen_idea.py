"""Write the idea text of the submission from docs/CLAIMS.md.

Usage:
  python scripts/gen_idea.py                   print the idea text, then its length on stderr
  python scripts/gen_idea.py --check           check the idea rows and the length, print the length
  python scripts/gen_idea.py --claims PATH     read another claims table (either mode)

The text is, in this order: the one line, the headline's sentence, the problem paragraph, "What we built:"
followed by the claim of every row whose idea column is yes, in table order, the "How NEAR fits" sentence
and the offline sentence. Only the rows come from the table; the other sentences are fixed below.

The text must hold 20 to 2,000 characters. While it is longer, the rows of each topic in DROP_ORDER are
removed, one topic at a time in that order: the feed, the trace, the scanner, the reproducible build,
lookalike names. A row belongs to a topic when its claim contains one of the topic's words; a claim that
names the MCP server never belongs to the scanner topic, so the MCP sentence stays when the scanner drops.
The exit code is 1 when the text is still out of bounds, and with --check also when an idea row names a
file, a function or a test that does not exist, or names no test.
"""

import argparse
import ast
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CLAIMS = ROOT / "docs" / "CLAIMS.md"
HEADER = "| Claim | File:function |"
MIN_CHARS, MAX_CHARS = 20, 2000
ONE_LINE = "Who your agent pays gets a verdict with evidence. Unknown never passes."
HEADLINE = "ShieldBot for NEAR is a security intelligence layer for AI agent money on NEAR."
PROBLEM = ("Problem: a NEAR function call key cannot attach the 1 yoctoNEAR that ft_transfer requires, so an agent "
           "that moves tokens holds a full access key, and the protocol does not check who the agent pays, on NEAR "
           "or where NEAR Intents sends the money.")
BUILT = "What we built:"
HOW_NEAR = ("How NEAR fits: hints AGENT SECURITY and FUNCTION CALL KEYS; a function call key scoped to one method is "
            "the enforcement boundary, with NEP-141 transfers and NEP-297 events.")
OFFLINE = "It runs offline with Python alone; the testnet transactions are linked in the README with their evidence."
DROP_ORDER = (
    ("the feed", ("feed",)),
    ("the trace", ("trace", "flow")),
    ("the scanner", ("scanner", "python -m gate scan")),
    ("the reproducible build", ("reproducible",)),
    ("lookalike names", ("lookalike",)),
)


def idea_rows(text):
    """The rows of the claims table whose idea column is yes, as dicts, in table order."""
    rows = []
    in_table = False
    for line in text.splitlines():
        if line.startswith(HEADER):
            in_table = True
            continue
        if not in_table:
            continue
        if not line.startswith("|"):
            break
        if line.startswith("|---"):
            continue
        cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
        if len(cells) != 5:
            raise ValueError(f"a claims row has {len(cells)} cells, not 5: {line}")
        claim, source, test, _, idea = cells
        if idea == "yes":
            rows.append({"claim": claim, "source": source.strip("`"), "test": test.strip("`")})
    return rows


def topic(claim):
    """The DROP_ORDER topic the claim belongs to, or None."""
    lowered = claim.lower()
    for name, words in DROP_ORDER:
        if name == "the scanner" and "MCP" in claim:
            continue
        if any(word in lowered for word in words):
            return name
    return None


def assemble(rows):
    sentences = " ".join(row["claim"].replace("`", "") for row in rows)
    return " ".join([ONE_LINE, HEADLINE, PROBLEM, f"{BUILT} {sentences}", HOW_NEAR, OFFLINE])


def build(rows):
    """(text, dropped topic names): the text, after the drop order while it is longer than MAX_CHARS."""
    kept, dropped = list(rows), []
    text = assemble(kept)
    for name, _ in DROP_ORDER:
        if len(text) <= MAX_CHARS:
            break
        kept = [row for row in kept if topic(row["claim"]) != name]
        dropped.append(name)
        text = assemble(kept)
    return text, dropped


def length_problem(text):
    if not MIN_CHARS <= len(text) <= MAX_CHARS:
        return f"the idea text holds {len(text)} characters, outside {MIN_CHARS} to {MAX_CHARS}"
    return None


def declared(path):
    """The function names declared in a Python file (with Class. prefixes) or a Rust file."""
    text = path.read_text(encoding="utf-8")
    if path.suffix != ".py":
        return set(re.findall(r"\bfn\s+([A-Za-z_][A-Za-z0-9_]*)", text))
    found = set()

    def visit(node, prefix):
        for child in getattr(node, "body", ()):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                found.add(prefix + child.name)
                visit(child, f"{prefix}{child.name}.")
            elif isinstance(child, ast.ClassDef):
                visit(child, f"{prefix}{child.name}.")

    visit(ast.parse(text), "")
    return found


def row_problems(row, root=ROOT):
    """What is missing for one idea row: its file and function, and its test."""
    problems = []
    path_text, _, function = row["source"].rpartition(":")
    path = root / path_text
    if not path_text or not path.is_file():
        problems.append(f"no file {path_text or row['source']!r}")
    elif function not in declared(path):
        problems.append(f"no function {function!r} in {path_text}")
    test_path_text, _, test_name = row["test"].partition("::")
    test_path = root / test_path_text
    if not test_name:
        problems.append("no test named")
    elif not test_path.is_file():
        problems.append(f"no test file {test_path_text!r}")
    else:
        expected = test_name.split("::")[-1] if test_path.suffix == ".rs" else test_name
        if expected not in declared(test_path):
            problems.append(f"no test {test_name!r} in {test_path_text}")
    return problems


def main(argv=None):
    parser = argparse.ArgumentParser(description="the idea text, generated from the claims table")
    parser.add_argument("--check", action="store_true", help="check the idea rows and the length only")
    parser.add_argument("--claims", default=str(CLAIMS), help="the claims table to read")
    args = parser.parse_args(argv)
    rows = idea_rows(Path(args.claims).read_text(encoding="utf-8"))
    text, dropped = build(rows)
    failed = False
    if args.check:
        for row in rows:
            for problem in row_problems(row):
                print(f"{row['claim']}: {problem}")
                failed = True
    else:
        print(text)
    report = sys.stdout if args.check else sys.stderr
    if dropped:
        print(f"dropped to fit: {', '.join(dropped)}", file=report)
    print(f"idea: {len(text)} characters", file=report)
    problem = length_problem(text)
    if problem:
        print(problem, file=report)
        failed = True
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
