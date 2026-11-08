"""A read only checklist for the day the repository goes public.

Usage:
  python scripts/public_flip_check.py [--fetch-evidence]

Reads every file `git ls-files` lists in the repository that holds the current directory, changes
nothing, and fails on:

  a path that holds one of PATH_MARKERS (upper case, as written), or a file name starting RUN-
  a line of text that names one of NAMES (case sensitive, whole words)
  a line that holds an ed25519 private key form: "ed25519:" and 60 or more base58 characters
  a tracked .env file (named .env or .env.<anything>)
  a line of the root README.md that names HOSTNAME (any case)

With --fetch-evidence, also reads every fixtures/runs/live-beat-*.txt log, fetches its evidence_url
with a 30 s timeout, and fails naming the log if the fetch fails or the response sha256 differs from
its recorded evidence_sha256.

Prints path:line: reason for each failure (path: reason for a path or evidence failure), never the
matched key text, and exits 1 on any failure, 0 otherwise. Binary files (a NUL byte in the first
8192 bytes) are checked by path only.
"""

import argparse
import glob
import hashlib
import http.client
import os
import re
import subprocess
import sys
import urllib.error
import urllib.request

# The names are split so this file does not name them itself.
NAMES = ("Fa" "ble", "Sa" "int", "Op" "us")
PATH_MARKERS = ("CRITIQUE", "FA" "BLE", "SA" "INT", "HANDOFF", "VOTE", "BUILD-PLAN")
NAME_PATTERN = re.compile(r"\b(" + "|".join(NAMES) + r")\b")
KEY_PATTERN = re.compile(r"ed25519:[1-9A-HJ-NP-Za-km-z]{60,}")
HOSTNAME = "shieldbotsecurity.online"
EVIDENCE_URL_PATTERN = re.compile(r"^evidence_url:\s*(\S+)\s*$")
EVIDENCE_SHA256_PATTERN = re.compile(r"^evidence_sha256 \(sha256 of [^)]+\):\s*([0-9a-f]{64})\s*$")
EVIDENCE_TIMEOUT_SECONDS = 30


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


def live_beat_logs(root):
    pattern = os.path.join(root, "fixtures", "runs", "live-beat-*.txt")
    return sorted(
        os.path.relpath(path, root).replace(os.sep, "/")
        for path in glob.glob(pattern)
        if os.path.isfile(path)
    )


def parse_evidence_headers(text):
    url = None
    expected = None
    for line in text.splitlines():
        if url is None:
            match = EVIDENCE_URL_PATTERN.match(line)
            if match:
                url = match.group(1)
        if expected is None:
            match = EVIDENCE_SHA256_PATTERN.match(line)
            if match:
                expected = match.group(1)
        if url is not None and expected is not None:
            break
    return url, expected


def check_evidence(root):
    logs = live_beat_logs(root)
    if not logs:
        return [], 0
    failures = []
    for rel in logs:
        full = os.path.join(root, *rel.split("/"))
        with open(full, "rb") as handle:
            text = handle.read().decode("utf-8", errors="replace")
        url, expected = parse_evidence_headers(text)
        if url is None or expected is None:
            failures.append(f"{rel}: missing evidence_url or evidence_sha256 line")
            continue
        if not url.startswith("https://"):
            failures.append(f"{rel}: evidence_url must use https://")
            continue
        try:
            with urllib.request.urlopen(url, timeout=EVIDENCE_TIMEOUT_SECONDS) as response:
                body = response.read()
        except (OSError, urllib.error.URLError, http.client.HTTPException, ValueError) as error:
            failures.append(f"{rel}: evidence fetch failed for {url}: {error}")
            continue
        actual = hashlib.sha256(body).hexdigest()
        if actual != expected:
            failures.append(f"{rel}: evidence sha256 mismatch: got {actual}, expected {expected}")
    return failures, len(logs)


def main(argv=None):
    parser = argparse.ArgumentParser(description="Read only checklist for the day the repository goes public")
    parser.add_argument(
        "--fetch-evidence",
        action="store_true",
        help="fetch the evidence URL recorded in each fixtures/runs/live-beat-*.txt and check its sha256",
    )
    args = parser.parse_args(argv)
    root = subprocess.run(["git", "rev-parse", "--show-toplevel"], check=True, capture_output=True,
                          text=True).stdout.strip()
    failures = check(root)
    if args.fetch_evidence:
        evidence_failures, log_count = check_evidence(root)
        if log_count == 0:
            print("evidence fetch check: no fixtures/runs/live-beat-*.txt logs; nothing to check")
        failures.extend(evidence_failures)
    for failure in failures:
        print(failure)
    if failures:
        print(f"{len(failures)} failure{'' if len(failures) == 1 else 's'}")
    else:
        print("public flip check: no failures")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
