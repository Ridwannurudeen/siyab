"""scripts/smoke.py: the nine offline steps, read only, PASS or FAIL per step and the summary."""

import contextlib
import hashlib
import importlib.util
import io
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
    def test_smoke_runs_nine_steps_read_only_and_leaves_the_tree_unchanged(self):
        before = snapshot(ROOT)
        with tempfile.TemporaryDirectory() as tmp:
            env = {**os.environ, "TMPDIR": tmp, "TEMP": tmp, "TMP": tmp}
            process = subprocess.run([sys.executable, str(ROOT / "scripts" / "smoke.py")], capture_output=True,
                                     cwd=ROOT, env=env, timeout=1800, check=False)
            left_behind = os.listdir(tmp)
        stdout = process.stdout.decode("utf-8", "replace")
        self.assertEqual(process.returncode, 0, stdout + process.stderr.decode("utf-8", "replace"))
        lines = stdout.splitlines()
        self.assertEqual([line.split(".", 1)[0] for line in lines if ": PASS (" in line],
                         [str(number) for number in range(1, 10)])
        self.assertNotIn(": FAIL ", stdout)
        self.assertEqual(lines[-1], "9 of 9 PASS")
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
            self.assertTrue(work.name.startswith("shieldbot-near-smoke-"), work)
            self.assertEqual({temp, tmp, tmpdir}, {str(work)})


if __name__ == "__main__":
    unittest.main()
