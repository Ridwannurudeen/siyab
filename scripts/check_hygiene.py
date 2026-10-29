"""Reject content that breaks the repository hygiene rules.

Usage:
  python scripts/check_hygiene.py --staged   check the staged content of added or modified files
  python scripts/check_hygiene.py --all      check every tracked or untracked, not ignored file
  python scripts/check_hygiene.py PATH...    check the named files on disk

Prints file:line: reason for every hit and exits 1 on any hit, 0 otherwise.
"""

import argparse
import os
import re
import subprocess
import sys

HEX_EXTENSIONS = {".md", ".py", ".rs", ".sh", ".toml", ".yml"}
HEX_EXEMPT_DIRS = ("fixtures/", "tests/", "intel/seeds/raw/", "res/")
PHRASE_EXTENSIONS = {".md", ".py", ".rs", ".sh", ".html", ".json", ".toml", ".yml"}
RAW_DIR = "intel/seeds/raw/"
# This file has to name the phrases it rejects, so the phrase rule skips it.
SELF = "scripts/check_hygiene.py"

HEX64 = re.compile(r"[0-9a-fA-F]{64}")
KEY_FORMS = (
    (re.compile(r"ed25519:[1-9A-HJ-NP-Za-km-z]{80,}"), "ed25519 private key form"),
    (re.compile(r'"(secret_key|private_key)"\s*:\s*"[^"]+"'), "secret_key or private_key value"),
    (re.compile(r"BEGIN [A-Z ]*PRIVATE KEY"), "PEM private key block"),
)
DASHES = re.compile("[\u2013\u2014]")
PHRASES = re.compile(
    r"real time|real-time|first on near|comprehensive|go-to|go to layer|go to security"
    r"|ai-powered|battle-tested|trustless|100%|arkham|co-authored-by",
    re.IGNORECASE,
)


def git(root, *args):
    return subprocess.run(["git", *args], cwd=root, check=True, capture_output=True).stdout


def repo_root():
    return git(os.getcwd(), "rev-parse", "--show-toplevel").decode().strip()


def check_text(path, text):
    ext = os.path.splitext(path)[1].lower()
    hex_rule = ext in HEX_EXTENSIONS and not path.startswith(HEX_EXEMPT_DIRS)
    dash_rule = not path.startswith(RAW_DIR) and path != "LICENSE"
    phrase_rule = ext in PHRASE_EXTENSIONS and not path.startswith(RAW_DIR) and path != SELF
    hits = []
    for number, line in enumerate(text.splitlines(), start=1):
        if hex_rule and HEX64.search(line):
            hits.append((number, "64 hex literal"))
        for pattern, reason in KEY_FORMS:
            if pattern.search(line):
                hits.append((number, reason))
        if dash_rule and DASHES.search(line):
            hits.append((number, "em or en dash"))
        if phrase_rule:
            match = PHRASES.search(line)
            if match:
                hits.append((number, f"forbidden phrase {match.group(0)!r}"))
    return hits


def check_bytes(path, data):
    if path.endswith(".wasm") or b"\0" in data[:8192]:
        return []
    return check_text(path, data.decode("utf-8", errors="replace"))


def staged_files(root):
    out = git(root, "diff", "--cached", "--name-only", "--diff-filter=AM", "-z")
    for path in filter(None, out.decode().split("\0")):
        yield path, git(root, "show", f":{path}")


def all_files(root):
    out = git(root, "ls-files", "--cached", "--others", "--exclude-standard", "-z")
    for path in sorted(set(filter(None, out.decode().split("\0")))):
        full = os.path.join(root, path)
        if os.path.isfile(full):
            with open(full, "rb") as handle:
                yield path, handle.read()


def named_files(root, paths):
    for name in paths:
        rel = os.path.relpath(os.path.abspath(name), root).replace(os.sep, "/")
        with open(name, "rb") as handle:
            yield rel, handle.read()


def main(argv=None):
    parser = argparse.ArgumentParser(description="Repository hygiene check")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--staged", action="store_true", help="check staged content")
    mode.add_argument("--all", action="store_true", help="check the whole tree")
    parser.add_argument("paths", nargs="*", help="files to check")
    args = parser.parse_args(argv)
    if not (args.staged or args.all or args.paths):
        parser.error("give --staged, --all or at least one path")
    if args.paths and (args.staged or args.all):
        parser.error("paths cannot be combined with --staged or --all")

    root = repo_root()
    if args.staged:
        files = staged_files(root)
    elif args.all:
        files = all_files(root)
    else:
        files = named_files(root, args.paths)

    failed = False
    for path, data in files:
        for number, reason in check_bytes(path, data):
            print(f"{path}:{number}: {reason}")
            failed = True
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
