"""Write the idea text of the submission from scripts/counts.py, docs/ATTACKS.md and docs/CLAIMS.md.

Usage:
  python scripts/gen_idea.py                   print the idea text, then its length on stderr
  python scripts/gen_idea.py --check           check the idea rows and the length, print the length
  python scripts/gen_idea.py --claims PATH     read another claims table (either mode)

The text is three paragraphs: the problem in two fixed sentences; "What exists:" with the fixed sentence
naming the parts, then the counts lines of scripts/counts.py and the counts sentence of the adversarial
table in docs/ATTACKS.md rewritten as prose by the fixed templates of COUNT_PROSE (the label store and its
provenance split, the incident count, the flows line, the feed line, the receiver line, the cohort line,
the adversarial table, the latency lines, in that order), then the claim of every row whose idea column is
yes, in table order; and the fixed "How NEAR fits" and offline sentences. A counts line no template
matches stops the run with ValueError, and so does an attacks document without exactly one counts
sentence.

The text must hold 20 to 2,000 characters. While it is longer, the sentences of each topic in DROP_ORDER
are removed, one topic at a time in that order: the protocol refusals, the latency lines, the scanner, the
reproducible build, lookalike names, the feed, the trace; the label store, incident, receiver, cohort and
adversarial table sentences are never dropped.
A sentence belongs to a topic when it contains one of the topic's words; a claim that names the MCP server
never belongs to the scanner topic, so the MCP sentence stays when the scanner drops. The exit code is 1
when the text is still out of bounds, and with --check also when an idea row names a file, a function or
a test that does not exist, or names no test.
"""

import argparse
import ast
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import counts

CLAIMS = ROOT / "docs" / "CLAIMS.md"
ATTACKS = ROOT / "docs" / "ATTACKS.md"
HEADER = "| Claim | File:function |"
ATTACK_COUNTS = re.compile(r"^\d+ attempts, \d+ refusals, \d+ bypasses\.", re.MULTILINE)
MIN_CHARS, MAX_CHARS = 20, 2000
PROBLEM = ("A NEAR function call key cannot attach the 1 yoctoNEAR ft_transfer requires, so an agent moving tokens "
           "holds a full access key. The protocol never checks who a prompt injected agent pays.")
BUILT = ("What exists: Siyab, a label store with a provenance class on every row, incident pages and "
         "a guard that pays only on a signed ALLOW bound to the exact transfer and refuses Unknown.")
HOW_NEAR = ("How NEAR fits: hints AGENT SECURITY and FUNCTION CALL KEYS; a function call key scoped to one method is "
            "the enforcement boundary, with NEP-141 transfers and NEP-297 events.")
OFFLINE = "It runs offline with Python alone; the README links the testnet transactions and their evidence."


def _count(number, noun):
    return f"{number} {noun}" + ("" if number == "1" else "s")


# A cohort's subject count and verdicts, as the cohort line states them.
_TALLY = r"\d+ \(\d+ ALLOW, \d+ DENY, \d+ Unknown\)"
# One template per counts line format, in the order the prose states them.
COUNT_PROSE = (
    (re.compile(r"Label store: (?P<a>\d+) assertions over (?P<s>\d+) subjects \(onchain (?P<o>\d+), "
                r"official_source (?P<f>\d+), third_party (?P<t>\d+), inferred (?P<i>\d+)\)\."),
     lambda m: (f"The label store holds {_count(m['a'], 'assertion')} over {_count(m['s'], 'subject')}: onchain "
                f"{m['o']}, official_source {m['f']}, third_party {m['t']}, inferred {m['i']}.")),
    (re.compile(r"Incidents: (?P<n>\d+) pages\."),
     lambda m: f"Incident pages: {m['n']}."),
    (re.compile(r"Flows: (?P<t>\d+) exit traces, (?P<h>\d+) hops, depth (?P<d>\d+) of (?P<b>\d+), "
                r"(?P<s>complete|stopped while reading (?:the subject|an account \d+ hops? from the subject))\."),
     lambda m: (f"The flow trace holds {_count(m['t'], 'exit trace')} over {_count(m['h'], 'hop')}, depth {m['d']} of "
                f"{m['b']}, {m['s']}.")),
    (re.compile(r"Feed: since (?P<d>[0-9-]+), (?P<b>\d+) blocks, (?P<c>\d+) events \(.*\)\."),
     lambda m: (f"The block feed has read {_count(m['b'], 'block')} since {m['d']} and raised "
                f"{_count(m['c'], 'event')}.")),
    (re.compile(r"Feed: following since (?P<d>[0-9-]+), (?P<b>\d+) blocks, (?P<c>\d+) events \(.*\)\."),
     lambda m: (f"The block feed has followed {_count(m['b'], 'block')} since {m['d']} and raised "
                f"{_count(m['c'], 'event')}.")),
    (re.compile(r"Feed: not run\."),
     lambda m: "The block feed has not run."),
    (re.compile(r"On (?P<n>\d+) recorded mainnet receivers the rules returned (?P<a>\d+) ALLOW, (?P<d>\d+) DENY, "
                r"(?P<u>\d+) Unknown\."),
     lambda m: (f"On {_count(m['n'], 'recorded mainnet receiver')} the rules return {m['a']} ALLOW, {m['d']} DENY, "
                f"{m['u']} Unknown.")),
    (re.compile(rf"By cohort: (?P<c>ordinary counterparties {_TALLY}); (?P<o>organisations from registries {_TALLY}); "
                rf"(?P<i>incident accounts {_TALLY}); (?P<t>catalog tokens {_TALLY})\."),
     lambda m: f"By cohort: {m['c']}; {m['o']}; {m['i']}; {m['t']}."),
    (re.compile(r"(?P<a>\d+) attempts, (?P<r>\d+) refusals, (?P<b>\d+) bypasses\."),
     lambda m: f"Adversarial table: {m['a']} attempts, {m['r']} refusals, {m['b']} bypasses."),
    (re.compile(r"Latency: p50 (?P<p50>\d+) ms, p95 (?P<p95>\d+) ms over (?P<r>\d+) verdict runs\."),
     lambda m: f"Verdict latency: p50 {m['p50']} ms, p95 {m['p95']} ms over {_count(m['r'], 'recorded run')}."),
    (re.compile(r"Latency: not measured\."),
     lambda m: "Verdict latency is not measured."),
    (re.compile(r"Latency, payee off the allowlist: p50 (?P<p50>\d+) ms, p95 (?P<p95>\d+) ms over (?P<r>\d+) verdict "
                r"runs\."),
     lambda m: (f"Verdict latency for a payee off the allowlist: p50 {m['p50']} ms, p95 {m['p95']} ms over "
                f"{_count(m['r'], 'recorded run')}.")),
    (re.compile(r"Latency, payee off the allowlist: not measured\."),
     lambda m: "Verdict latency for a payee off the allowlist is not measured."),
    (re.compile(r"Latency, payee off the allowlist, NearBlocks fallback: p50 (?P<p50>\d+) ms, "
                r"p95 (?P<p95>\d+) ms over (?P<r>\d+) verdict runs\."),
     lambda m: (f"Verdict latency for the NearBlocks fallback path: p50 {m['p50']} ms, "
                f"p95 {m['p95']} ms over {_count(m['r'], 'recorded run')}.")),
    (re.compile(r"Latency, payee off the allowlist, NearBlocks fallback: not measured\."),
     lambda m: "Verdict latency for the NearBlocks fallback path is not measured."),
)
DROP_ORDER = (
    ("the protocol refusals", ("refused by the protocol",)),
    ("the latency lines", ("latency",)),
    ("the scanner", ("scanner", "python -m gate scan")),
    ("the reproducible build", ("reproducible",)),
    ("lookalike names", ("lookalike",)),
    ("the feed", ("feed",)),
    ("the trace", ("trace", "flow")),
)


def count_rows(count_lines):
    """The counts lines as prose rows ({"claim": sentence}), in COUNT_PROSE order."""
    placed = []
    for line in count_lines:
        for index, (pattern, prose) in enumerate(COUNT_PROSE):
            match = pattern.fullmatch(line)
            if match:
                placed.append((index, prose(match)))
                break
        else:
            raise ValueError(f"no prose template for the counts line: {line}")
    return [{"claim": sentence} for _, sentence in sorted(placed, key=lambda item: item[0])]


def attack_line(path=ATTACKS):
    """The counts sentence of the adversarial table: the line of the attacks document that opens with the counts."""
    found = ATTACK_COUNTS.findall(Path(path).read_text(encoding="utf-8"))
    if len(found) != 1:
        raise ValueError(f"{path} holds {len(found)} attack counts sentences, not 1")
    return found[0]


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
    exists = " ".join([BUILT] + [row["claim"].replace("`", "") for row in rows])
    return "\n\n".join([PROBLEM, exists, f"{HOW_NEAR} {OFFLINE}"])


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
    text, dropped = build(count_rows(counts.lines() + [attack_line()]) + rows)
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
