import subprocess
import unittest
from pathlib import Path

from tests.test_redeploy import bash_path
from tests.test_scripts import BASH, ScriptTest

ROOT = Path(__file__).resolve().parent.parent
OLD = "1" * 40
NEW = "2" * 40
FAKE_GIT = r"""#!/bin/sh
printf 'git' >> "$UPDATE_LOG"
for arg in "$@"; do printf ' %s' "$arg" >> "$UPDATE_LOG"; done
printf '\n' >> "$UPDATE_LOG"
if [ "$1" = "-C" ]; then shift 2; fi
case "$1 $2" in
  'rev-parse --show-toplevel') printf '%s\n' "$UPDATE_REPO" ;;
  'rev-parse HEAD') cat "$UPDATE_HEAD" ;;
  'status --porcelain') ;;
  'fetch origin') ;;
  'worktree add') mkdir -p "$4" ;;
  'worktree prune') ;;
  'checkout --detach') printf '%s\n' "$3" > "$UPDATE_HEAD" ;;
  *) printf 'unexpected git request\n' >&2; exit 42 ;;
esac
"""
FAKE_SYSTEMCTL = r"""#!/bin/sh
printf 'systemctl' >> "$UPDATE_LOG"
for arg in "$@"; do printf ' %s' "$arg" >> "$UPDATE_LOG"; done
printf '\n' >> "$UPDATE_LOG"
if [ "$FAIL_RESTART" = always ]; then exit 1; fi
if [ "$FAIL_RESTART" = first ] && [ ! -f "$UPDATE_RESTARTED" ]; then
  touch "$UPDATE_RESTARTED"
  exit 1
fi
"""
FAKE_PYTHON = r"""#!/bin/sh
if [ "$1" = -m ]; then
  printf 'Ran 1 test\nOK\n'
  exit "$TEST_STATUS"
fi
exit "$HYGIENE_STATUS"
"""


class UpdateTest(ScriptTest):
    def setUp(self):
        super().setUp()
        self.log = Path(self.tempdir.name) / "update.log"
        self.head = Path(self.tempdir.name) / "head"
        self.head.write_text(OLD + "\n", encoding="utf-8")
        self.env.update({
            "UPDATE_LOG": bash_path(self.log),
            "UPDATE_REPO": bash_path(ROOT),
            "UPDATE_HEAD": bash_path(self.head),
            "UPDATE_RESTARTED": bash_path(Path(self.tempdir.name) / "restarted"),
            "FAIL_RESTART": "never",
            "TEST_STATUS": "0",
            "HYGIENE_STATUS": "0",
        })
        bin_dir = Path(self.tempdir.name) / "bin"
        for name, text in (("git", FAKE_GIT), ("systemctl", FAKE_SYSTEMCTL),
                           ("python3", FAKE_PYTHON), ("sudo", '#!/bin/sh\nexec "$@"\n')):
            path = bin_dir / name
            path.write_text(text, encoding="utf-8", newline="\n")
            path.chmod(0o755)

    def update(self, **env):
        return subprocess.run([BASH, "deploy/update.sh", NEW], capture_output=True,
                              cwd=ROOT, env={**self.env, **env}, timeout=30,
                              check=False, text=True)

    def calls(self):
        return self.log.read_text(encoding="utf-8").splitlines()

    def restart(self):
        units = ["shieldbot-near-feed.service", "shieldbot-near-api.service"]
        units += [path.name for path in sorted((ROOT / "deploy").glob("siyab-*.service"))]
        return "systemctl restart " + " ".join(units)

    def test_green_candidate_restarts_all_units(self):
        process = self.update()
        self.assertEqual(process.returncode, 0, process.stderr)
        self.assertEqual(self.head.read_text(encoding="utf-8").strip(), NEW)
        self.assertEqual([call for call in self.calls() if call.startswith("systemctl")],
                         [self.restart()])

    def test_failed_restart_restores_previous_checkout_and_restarts_again(self):
        process = self.update(FAIL_RESTART="first")
        self.assertNotEqual(process.returncode, 0)
        self.assertEqual(self.head.read_text(encoding="utf-8").strip(), OLD)
        calls = self.calls()
        self.assertEqual([call for call in calls if call.startswith("systemctl")],
                         [self.restart(), self.restart()])
        first = calls.index(self.restart())
        restore = calls.index(f"git checkout --detach {OLD}")
        self.assertLess(first, restore)
        self.assertLess(restore, len(calls) - 1 - calls[::-1].index(self.restart()))
        self.assertIn("previous checkout restarted", process.stderr)

    def test_failed_recovery_still_exits_nonzero_and_reports_failure(self):
        process = self.update(FAIL_RESTART="always")
        self.assertNotEqual(process.returncode, 0)
        self.assertEqual(self.head.read_text(encoding="utf-8").strip(), OLD)
        self.assertEqual([call for call in self.calls() if call.startswith("systemctl")],
                         [self.restart(), self.restart()])
        self.assertIn("previous checkout restart also failed", process.stderr)

    def test_failed_tests_or_hygiene_keep_previous_checkout_without_restart(self):
        for status in ("TEST_STATUS", "HYGIENE_STATUS"):
            with self.subTest(status=status):
                process = self.update(**{status: "1"})
                self.assertNotEqual(process.returncode, 0)
                self.assertEqual(self.head.read_text(encoding="utf-8").strip(), OLD)
                self.assertFalse(any(call.startswith(("systemctl", "git checkout"))
                                     for call in self.calls()))


if __name__ == "__main__":
    unittest.main()
