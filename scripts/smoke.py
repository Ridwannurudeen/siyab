"""Run the nine offline steps of the README section "Try it offline", read only, and report each.

Usage:
  python scripts/smoke.py

Each step prints one line, "<n>. <step>: PASS (<what it saw>)" or "<n>. <step>: FAIL <reason>", and the
last line is "<passed> of 9 PASS". The exit code is 0 only when all nine pass. It needs Python alone and
runs the same on Windows and POSIX: no shell, no glob, no sha256sum.

Read only: the network is refused inside this process (urllib.request.urlopen raises), and while the
steps run this process sets GATE_FIXTURES_MODE=replay and points TMPDIR, TEMP and TMP at its temporary
directory, so the in process steps and the subprocesses they start replay too; every step that runs a
command starts it as a subprocess with -I (isolated: no PYTHON* variables, no user site), -B (no
bytecode written) and GATE_FIXTURES_MODE=replay, through a short boot that refuses the network the same
way and puts the repository on sys.path; every temporary file, the evidence documents of steps 1 and 6
and the judgements of step 9 included, goes into one temporary directory removed at the end.

  1 python -m gate demo --offline exits 0 with the four outcomes ALLOW, DENY, UNKNOWN, DENY in order
  2 the Python suite, loaded in this process with tests.test_smoke left out (that module runs this
    script, so keeping it would run the suite without end), ends OK; the test and skip counts are shown
  3 res/gate.wasm and res/registry.wasm hash, with hashlib, to their .sha256 files, and python -m gate
    codehash prints the same sha256 and a code_hash that docs/TESTNET.md states
  4 python -m gate wasm-exports prints a sorted export list holding __contract_abi and the contract's
    entry method (request_transfer, record_batch)
  5 python -m gate verify on fixtures/registry/testnet-vendor.json and the one evidence file
    fixtures/evidence/7e77187d*.json names (the glob is resolved here) ends "verified"
  6 python -m mcp.server, fed initialize, notifications/initialized and tools/list through a pipe,
    answers both requests and lists its three tools
  7 python -m intel counts and python -m gate incident rhea print the label counts and the Rhea page
  8 python -m unittest tests.test_canon, the Python half of step 8; the Rust half needs crates.io and
    is not run here
  9 intel receivers replayed into the temporary directory: every judgement equals the committed file
    under fixtures/receivers/judgements/ byte for byte, the same set of files
"""

import contextlib
import hashlib
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

TIMEOUT_S = 600
LEFT_OUT = "test_smoke"
REFUSED = "the smoke run refuses the network"
# Run as python -I -c BOOT <root> <module> <args>: refuse the network, put the repository on sys.path
# (isolated mode leaves the working directory off it), then run the module as python -m would.
BOOT = chr(10).join((
    "import runpy, sys, urllib.request",
    "def refuse(*args, **kwargs):",
    f"    raise OSError({REFUSED!r})",
    "urllib.request.urlopen = refuse",
    "sys.path.insert(0, sys.argv[1])",
    "sys.argv = sys.argv[2:]",
    "runpy.run_module(sys.argv[0], run_name='__main__', alter_sys=True)",
))
OUTCOMES = (("  ALLOW, ", "ALLOW"), ("  DENIED. ", "DENY"), ("  UNKNOWN, ", "UNKNOWN"))
TOOLS = ["check_near_payment", "check_intents_quote", "scan_near_account"]
MCP_SESSION = (
    {"jsonrpc": "2.0", "id": 1, "method": "initialize",
     "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "smoke", "version": "0"}}},
    {"jsonrpc": "2.0", "method": "notifications/initialized"},
    {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
)


class StepFailed(Exception):
    """A step that ran and did not see what the README states."""


def _refuse(*args, **kwargs):
    raise OSError(REFUSED)


def _python(work, module, *args, stdin=b""):
    """(stdout, stderr) of python -m module args, run isolated in replay from the repository root."""
    env = {**os.environ, "GATE_FIXTURES_MODE": "replay", "TMPDIR": str(work), "TEMP": str(work), "TMP": str(work)}
    command = [sys.executable, "-I", "-B", "-X", "utf8", "-c", BOOT, str(ROOT), module, *args]
    process = subprocess.run(command, input=stdin, capture_output=True, cwd=ROOT, env=env, timeout=TIMEOUT_S,
                             check=False)
    stdout, stderr = process.stdout.decode("utf-8", "replace"), process.stderr.decode("utf-8", "replace")
    if process.returncode != 0:
        last = (stderr.strip().splitlines() or ["no output on stderr"])[-1]
        raise StepFailed(f"python -m {' '.join((module, *args))} exited {process.returncode}: {last}")
    return stdout, stderr


def demo(work):
    stdout, _ = _python(work, "gate", "demo", "--offline")
    seen = [word for line in stdout.splitlines() for prefix, word in OUTCOMES if line.startswith(prefix)]
    if seen != ["ALLOW", "DENY", "UNKNOWN", "DENY"]:
        raise StepFailed(f"the outcomes were {', '.join(seen) or 'none'}, not ALLOW, DENY, UNKNOWN, DENY")
    return ", ".join(seen)


def _flatten(suite):
    for item in suite:
        if isinstance(item, unittest.TestSuite):
            yield from _flatten(item)
        else:
            yield item


def suite(work):
    loaded = unittest.TestLoader().discover(str(ROOT / "tests"), top_level_dir=str(ROOT))
    kept = unittest.TestSuite(test for test in _flatten(loaded)
                              if type(test).__module__.rpartition(".")[2] != LEFT_OUT)
    stream = io.StringIO()
    with contextlib.redirect_stdout(stream), contextlib.redirect_stderr(stream):
        result = unittest.TextTestRunner(stream=stream, verbosity=0).run(kept)
    if not result.wasSuccessful() or not result.testsRun:
        failed = [test.id() for test, _ in result.failures + result.errors]
        raise StepFailed(f"ran {result.testsRun} tests: {len(result.failures)} failures, {len(result.errors)} "
                         f"errors, the first {failed[0] if failed else 'none'}")
    return f"Ran {result.testsRun} tests, OK (skipped={len(result.skipped)}), tests.{LEFT_OUT} left out"


def checksums(work):
    testnet = (ROOT / "docs" / "TESTNET.md").read_text(encoding="utf-8")
    for name in ("gate", "registry"):
        wasm = f"res/{name}.wasm"
        expected = (ROOT / "res" / f"{name}.wasm.sha256").read_text(encoding="utf-8").split()[0]
        digest = hashlib.sha256((ROOT / wasm).read_bytes()).hexdigest()
        if digest != expected:
            raise StepFailed(f"{wasm} hashes to {digest[:8]}, not the {expected[:8]} of its .sha256 file")
        stdout, _ = _python(work, "gate", "codehash", wasm)
        printed = dict(line.split(": ", 1) for line in stdout.splitlines() if ": " in line)
        if printed.get("sha256") != digest:
            raise StepFailed(f"python -m gate codehash {wasm} printed sha256 {printed.get('sha256')}")
        if f"`{printed.get('code_hash')}`" not in testnet:
            raise StepFailed(f"the code_hash {printed.get('code_hash')} of {wasm} is not in docs/TESTNET.md")
    return "both wasm files match their .sha256 files and their code_hash is in docs/TESTNET.md"


def exports(work):
    for name, method in (("gate", "request_transfer"), ("registry", "record_batch")):
        stdout, _ = _python(work, "gate", "wasm-exports", f"res/{name}.wasm")
        names = stdout.split()
        if names != sorted(names) or "__contract_abi" not in names or method not in names:
            raise StepFailed(f"res/{name}.wasm exports {', '.join(names) or 'nothing'}")
    return "both export lists are sorted and hold __contract_abi, request_transfer and record_batch"


def verify(work):
    matches = sorted((ROOT / "fixtures" / "evidence").glob("7e77187d*.json"))
    if len(matches) != 1:
        raise StepFailed(f"fixtures/evidence/7e77187d*.json names {len(matches)} files, not 1")
    evidence = matches[0].relative_to(ROOT).as_posix()
    stdout, _ = _python(work, "gate", "verify", "fixtures/registry/testnet-vendor.json", evidence)
    lines = stdout.splitlines()
    if not lines or lines[-1] != "verified":
        raise StepFailed(f"the last line is {lines[-1] if lines else 'missing'!r}, not 'verified'")
    return f"verified with fixtures/evidence/{matches[0].stem[:8]}...json"


def mcp_server(work):
    session = b"".join(json.dumps(message).encode("utf-8") + b"\n" for message in MCP_SESSION)
    stdout, _ = _python(work, "mcp.server", stdin=session)
    replies = [json.loads(line) for line in stdout.splitlines()]
    if [reply.get("id") for reply in replies] != [1, 2]:
        raise StepFailed(f"the server answered ids {[reply.get('id') for reply in replies]}, not [1, 2]")
    info = replies[0].get("result", {}).get("serverInfo", {})
    names = [tool["name"] for tool in replies[1].get("result", {}).get("tools", [])]
    if info.get("name") != "shieldbot-near" or names != TOOLS:
        raise StepFailed(f"serverInfo {info}, tools {names}")
    return f"{info['name']} {info.get('version')}, tools {', '.join(names)}"


def counts(work):
    labels_out, _ = _python(work, "intel", "counts")
    incident_out, _ = _python(work, "gate", "incident", "rhea")
    first = labels_out.splitlines()[0] if labels_out.strip() else ""
    title = incident_out.splitlines()[0] if incident_out.strip() else ""
    if " assertions over " not in first or not title.startswith("Rhea Lend"):
        raise StepFailed(f"intel counts printed {first!r} and gate incident rhea {title!r}")
    return f"{first.split(':')[0]}; {title}"


def canon(work):
    _, stderr = _python(work, "unittest", "tests.test_canon")
    ran = next((line for line in stderr.splitlines() if line.startswith("Ran ")), "no Ran line")
    return f"{ran}, OK; the Rust half (cargo test --lib) needs crates.io and is not run"


def receivers(work):
    from intel import __main__ as intel_main
    from intel import labels

    document = json.loads((intel_main.RECEIVERS_DIR / "set.json").read_text(encoding="utf-8"))
    results = intel_main.judge_receivers(document, labels.load(), work / "receivers")
    written = work / "receivers" / "judgements"
    committed = intel_main.RECEIVERS_DIR / "judgements"
    names = sorted(path.name for path in written.glob("*.json"))
    if names != sorted(path.name for path in committed.glob("*.json")):
        raise StepFailed("the replay wrote another set of judgement files than the committed one")
    for name in names:
        if (written / name).read_bytes() != (committed / name).read_bytes():
            raise StepFailed(f"the replayed judgement {name} differs from the committed file")
    verdicts = [result["verdict"] for result in results]
    return (f"On {len(verdicts)} recorded mainnet receivers the rules returned {verdicts.count('ALLOW')} ALLOW, "
            f"{verdicts.count('DENY')} DENY, {verdicts.count('UNKNOWN')} Unknown; every file equals the committed one")


STEPS = (
    ("python -m gate demo --offline", demo),
    ("python -m unittest discover -s tests", suite),
    ("sha256 of res/*.wasm and python -m gate codehash", checksums),
    ("python -m gate wasm-exports", exports),
    ("python -m gate verify", verify),
    ("python -m mcp.server", mcp_server),
    ("python -m intel counts and python -m gate incident rhea", counts),
    ("python -m unittest tests.test_canon", canon),
    ("python -m intel receivers, replayed into a temporary directory", receivers),
)


def main():
    os.chdir(ROOT)
    sys.dont_write_bytecode = True
    tempdir, passed = tempfile.tempdir, 0
    with (tempfile.TemporaryDirectory(prefix="shieldbot-near-smoke-") as tmp,
          mock.patch("urllib.request.urlopen", side_effect=_refuse),
          mock.patch.dict(os.environ, {"GATE_FIXTURES_MODE": "replay", "TMPDIR": tmp, "TEMP": tmp, "TMP": tmp})):
        tempfile.tempdir = tmp
        try:
            for number, (title, step) in enumerate(STEPS, start=1):
                try:
                    detail = step(Path(tmp))
                except Exception as error:  # noqa: BLE001 (any error a step raises is the FAIL of that step)
                    reason = str(error) if isinstance(error, StepFailed) else f"{type(error).__name__}: {error}"
                    print(f"{number}. {title}: FAIL {reason}", flush=True)
                else:
                    passed += 1
                    print(f"{number}. {title}: PASS ({detail})", flush=True)
        finally:
            tempfile.tempdir = tempdir
    print(f"{passed} of {len(STEPS)} PASS")
    return 0 if passed == len(STEPS) else 1


if __name__ == "__main__":
    sys.exit(main())
