"""scripts/smoke.py: the nine offline steps, read only, PASS or FAIL per step and the summary."""

import contextlib
import hashlib
import importlib.util
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


def load_smoke():
    spec = importlib.util.spec_from_file_location("smoke", ROOT / "scripts" / "smoke.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def snapshot(root):
    """{relative path: sha256} of every file under root; __pycache__ directories are ignored, as git ignores them."""
    files = {}
    for directory, names, filenames in os.walk(root):
        names[:] = [name for name in names if name != "__pycache__"]
        for filename in filenames:
            path = Path(directory) / filename
            files[path.relative_to(root).as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()
    return files


class SmokeTest(unittest.TestCase):
    def test_ci_skips_only_the_nested_suite(self):
        smoke = load_smoke()
        called = []
        nested = mock.Mock(return_value="suite ran")
        steps = [(title, nested if step is smoke.suite else
                  lambda work, title=title: called.append(title) or "fine")
                 for title, step in smoke.STEPS]
        stdout = io.StringIO()
        with (mock.patch.dict(os.environ, {"SIYAB_SMOKE_SKIP_SUITE": "1"}),
              mock.patch.object(smoke, "STEPS", steps),
              mock.patch.object(smoke, "suite", nested),
              contextlib.redirect_stdout(stdout)):
            code = smoke.main()
        nested.assert_not_called()
        self.assertEqual(code, 0)
        self.assertEqual(called, [title for title, step in steps if step is not nested])
        self.assertEqual(len(called), 8)
        lines = stdout.getvalue().splitlines()
        self.assertEqual(lines[1], "2. suite: skipped, run by the CI unit-test step")
        self.assertEqual(lines[-1], "8 of 8 PASS (1 skipped)")

    def test_ci_skip_does_not_hide_another_step_failure(self):
        smoke = load_smoke()

        def broken(work):
            raise smoke.StepFailed("the evidence hash differs from the record")

        nested = mock.Mock(return_value="suite ran")
        steps = [(title, nested if step is smoke.suite else lambda work: "fine")
                 for title, step in smoke.STEPS]
        steps[4] = (steps[4][0], broken)
        stdout = io.StringIO()
        with (mock.patch.dict(os.environ, {"SIYAB_SMOKE_SKIP_SUITE": "1"}),
              mock.patch.object(smoke, "STEPS", steps),
              mock.patch.object(smoke, "suite", nested),
              contextlib.redirect_stdout(stdout)):
            code = smoke.main()
        self.assertEqual(code, 1)
        self.assertIn("5. python -m gate verify: FAIL the evidence hash differs from the record",
                      stdout.getvalue())
        self.assertEqual(stdout.getvalue().splitlines()[-1], "7 of 8 PASS (1 skipped)")

    def test_only_one_enables_the_suite_skip(self):
        smoke = load_smoke()
        for value in (None, "", "0", "true"):
            with self.subTest(value=value):
                nested = mock.Mock(return_value="suite ran")
                steps = [(title, nested if step is smoke.suite else lambda work: "fine")
                         for title, step in smoke.STEPS]
                stdout = io.StringIO()
                with (mock.patch.dict(os.environ),
                      mock.patch.object(smoke, "suite", nested),
                      mock.patch.object(smoke, "STEPS", steps),
                      contextlib.redirect_stdout(stdout)):
                    if value is None:
                        os.environ.pop("SIYAB_SMOKE_SKIP_SUITE", None)
                    else:
                        os.environ["SIYAB_SMOKE_SKIP_SUITE"] = value
                    code = smoke.main()
                nested.assert_called_once()
                self.assertEqual(code, 0)
                self.assertEqual(stdout.getvalue().splitlines()[-1], "9 of 9 PASS")
                self.assertNotIn("skipped", stdout.getvalue())

    def test_mcp_step_accepts_the_listed_tools_with_checks_disabled(self):
        from mcp import server

        smoke = load_smoke()
        with mock.patch.dict(os.environ, {"SIYAB_CHECKS": "0"}):
            replies = [server.handle(message, "unused") for message in smoke.MCP_SESSION]
        stdout = "\n".join(json.dumps(reply) for reply in replies if reply is not None)
        with mock.patch.object(smoke, "_python", return_value=(stdout, "")):
            self.assertEqual(smoke.mcp_server(ROOT),
                             "siyab 0.1.0, tools " + ", ".join(tool["name"] for tool in server.TOOLS))

    def test_smoke_runs_nine_steps_read_only_and_leaves_the_tree_unchanged(self):
        skip_suite = os.environ.get("SIYAB_SMOKE_SKIP_SUITE") == "1"
        before = snapshot(ROOT)
        with tempfile.TemporaryDirectory() as tmp:
            env = {**os.environ, "TMPDIR": tmp, "TEMP": tmp, "TMP": tmp}
            process = subprocess.run([sys.executable, str(ROOT / "scripts" / "smoke.py")], capture_output=True,
                                     cwd=ROOT, env=env, timeout=1800, check=False)
            left_behind = os.listdir(tmp)
        stdout = process.stdout.decode("utf-8", "replace")
        self.assertEqual(process.returncode, 0, stdout + process.stderr.decode("utf-8", "replace"))
        self.assertEqual(process.stderr, b"")
        lines = stdout.splitlines()
        self.assertEqual([line.split(".", 1)[0] for line in lines if ": PASS (" in line],
                         [str(number) for number in range(1, 10) if not (skip_suite and number == 2)])
        self.assertNotIn(": FAIL ", stdout)
        if skip_suite:
            self.assertEqual(lines[1], "2. suite: skipped, run by the CI unit-test step")
        else:
            self.assertNotIn("suite: skipped", stdout)
        self.assertEqual(lines[-1], "8 of 8 PASS (1 skipped)" if skip_suite else "9 of 9 PASS")
        self.assertEqual(left_behind, [])
        self.assertEqual(snapshot(ROOT), before)

    def test_a_failing_step_prints_fail_and_exits_1(self):
        smoke = load_smoke()

        def broken(work):
            raise smoke.StepFailed("the evidence hash differs from the record")

        steps = [(f"step {number}", lambda work: "fine") for number in range(1, 10)]
        steps[4] = ("python -m gate verify", broken)
        stdout = io.StringIO()
        tempdir = tempfile.tempdir
        with mock.patch.object(smoke, "STEPS", steps), contextlib.redirect_stdout(stdout):
            code = smoke.main()
        self.assertEqual(code, 1)
        lines = stdout.getvalue().splitlines()
        self.assertEqual(len(lines), 10)
        self.assertEqual(lines[4], "5. python -m gate verify: FAIL the evidence hash differs from the record")
        self.assertEqual(lines[0], "1. step 1: PASS (fine)")
        self.assertEqual(lines[-1], "8 of 9 PASS")
        self.assertEqual(tempfile.tempdir, tempdir)

    def test_every_step_runs_in_replay_from_the_smoke_temporary_directory(self):
        smoke = load_smoke()
        seen = []

        def record(work):
            seen.append((os.environ["GATE_FIXTURES_MODE"], os.environ["TEMP"], os.environ["TMP"],
                         os.environ["TMPDIR"], work))
            return "recorded"

        stdout = io.StringIO()
        with mock.patch.dict(os.environ, {"GATE_FIXTURES_MODE": "record"}):
            before = dict(os.environ)
            with (mock.patch.object(smoke, "STEPS", [(f"step {number}", record) for number in range(1, 10)]),
                  contextlib.redirect_stdout(stdout)):
                code = smoke.main()
            after = dict(os.environ)
        self.assertEqual(code, 0, stdout.getvalue())
        self.assertEqual(after, before)
        self.assertEqual(len(seen), 9)
        for mode, temp, tmp, tmpdir, work in seen:
            self.assertEqual(mode, "replay")
            self.assertTrue(work.name.startswith("siyab-smoke-"), work)
            self.assertEqual({temp, tmp, tmpdir}, {str(work)})


if __name__ == "__main__":
    unittest.main()
