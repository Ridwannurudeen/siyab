"""A read only checklist for the day the repository goes public.

Usage:
  python scripts/public_flip_check.py

Reads every file `git ls-files` lists in the repository that holds the current directory, changes
nothing, and fails on:

  a path that holds one of PATH_MARKERS (upper case, as written), or a file name starting RUN-
  a line of text that names one of NAMES (case sensitive, whole words)
  a line that holds an ed25519 private key form: "ed25519:" and 60 or more base58 characters
  a tracked .env file (named .env or .env.<anything>)
  a line of the root README.md that names HOSTNAME (any case)

Prints path:line: reason for each failure (path: reason for a path failure), never the matched key text,
and exits 1 on any failure, 0 otherwise. Binary files (a NUL byte in the first 8192 bytes) are checked by
path only.
"""

import os
import re
import subprocess
import sys

# The names are split so this file does not name them itself.
NAMES = ("Fa" "ble", "Sa" "int", "Op" "us")
PATH_MARKERS = ("CRITIQUE", "FA" "BLE", "SA" "INT", "HANDOFF", "VOTE", "BUILD-PLAN")
NAME_PATTERN = re.compile(r"\b(" + "|".join(NAMES) + r")\b")
KEY_PATTERN = re.compile(r"ed25519:[1-9A-HJ-NP-Za-km-z]{60,}")
HOSTNAME = "shieldbotsecurity.online"


def tracked_files(root):
    out = subprocess.run(["git", "ls-files", "-z"], cwd=root, check=True, capture_output=True).stdout
    return sorted(filter(None, out.decode().split("\0")))


def path_failures(path):
    name = path.rsplit("/", 1)[-1]
    failures = [f"{path}: planning marker {marker} in the path" for marker in PATH_MARKERS if marker in path]
    if name.startswith("RUN-"):
        failures.append(f"{path}: RUN- prompt file")
    if name == ".env" or name.startswith(".env."):
        failures.append(f"{path}: tracked .env file")
    return failures


def text_failures(path, text):
    failures = []
    for number, line in enumerate(text.splitlines(), start=1):
        for match in NAME_PATTERN.finditer(line):
            failures.append(f"{path}:{number}: names {match.group(1)}")
        if KEY_PATTERN.search(line):
            failures.append(f"{path}:{number}: ed25519 private key form")
        if path == "README.md" and HOSTNAME in line.lower():
            failures.append(f"{path}:{number}: names the hosted hostname {HOSTNAME}")
    return failures


def check(root):
    failures = []
    for path in tracked_files(root):
        failures.extend(path_failures(path))
        full = os.path.join(root, path)
        if not os.path.isfile(full):
            continue
        with open(full, "rb") as handle:
            data = handle.read()
        if b"\0" not in data[:8192]:
            failures.extend(text_failures(path, data.decode("utf-8", errors="replace")))
    return failures


def main():
    root = subprocess.run(["git", "rev-parse", "--show-toplevel"], check=True, capture_output=True,
                          text=True).stdout.strip()
    failures = check(root)
    for failure in failures:
        print(failure)
    if failures:
        print(f"{len(failures)} failure{'' if len(failures) == 1 else 's'}")
    else:
        print("public flip check: no failures")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
