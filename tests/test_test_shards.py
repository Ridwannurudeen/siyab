"""The CI module shards cover standard unittest discovery exactly once."""

import importlib.util
import json
import subprocess
import sys
import tempfile
import unittest
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SPEC = importlib.util.spec_from_file_location("test_shards", ROOT / "scripts" / "test_shards.py")
SHARDS = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(SHARDS)


def test_ids(suite):
    for test in suite:
        if isinstance(test, unittest.TestSuite):
            yield from test_ids(test)
        else:
            yield test.id()


class TestShardsTest(unittest.TestCase):
    def successful_jobs(self):
        return [{"name": f"unit-tests (Python {version}, shard {shard}/4)",
                 "status": "completed", "conclusion": "success"}
                for version in ("3.11", "3.12") for shard in range(1, 5)]

    def test_job_gate_accepts_all_shards_across_pages(self):
        jobs = self.successful_jobs()
        jobs.append({"name": "offline (3.11)", "status": "in_progress", "conclusion": None})
        SHARDS.validate_jobs([{"jobs": jobs[:3]}, {"jobs": jobs[3:]}])

    def test_job_gate_rejects_a_missing_shard(self):
        with self.assertRaisesRegex(ValueError, "exactly once"):
            SHARDS.validate_jobs([{"jobs": self.successful_jobs()[:-1]}])

    def test_job_gate_rejects_a_duplicate_shard(self):
        jobs = self.successful_jobs()
        jobs.append(dict(jobs[0]))
        with self.assertRaisesRegex(ValueError, "exactly once"):
            SHARDS.validate_jobs([{"jobs": jobs}])

    def test_job_gate_rejects_an_extra_shard(self):
        jobs = self.successful_jobs()
        jobs.append(dict(jobs[0], name="unit-tests (Python 3.13, shard 1/4)"))
        with self.assertRaisesRegex(ValueError, "exactly once"):
            SHARDS.validate_jobs([{"jobs": jobs}])

    def test_job_gate_rejects_incomplete_or_unsuccessful_shards(self):
        for status, conclusion in (("queued", None), ("in_progress", None),
                                   ("queued", "success"), ("completed", "cancelled"),
                                   ("completed", "failure"), ("completed", "skipped"),
                                   ("completed", None)):
            with self.subTest(status=status, conclusion=conclusion):
                jobs = self.successful_jobs()
                jobs[0].update(status=status, conclusion=conclusion)
                with self.assertRaisesRegex(ValueError, "completed successfully"):
                    SHARDS.validate_jobs([{"jobs": jobs}])

    def test_job_gate_rejects_malformed_or_empty_pages(self):
        for pages in ([], {}, [None], [{"jobs": None}], [{"jobs": [{}]}]):
            with self.subTest(pages=pages), self.assertRaises((ValueError, TypeError, KeyError)):
                SHARDS.validate_jobs(pages)

    def test_job_gate_cli_reads_pages_and_refuses_missing_or_invalid_json(self):
        command = [sys.executable, str(ROOT / "scripts" / "test_shards.py"), "--check-jobs"]
        complete = subprocess.run(command, input=json.dumps([{"jobs": self.successful_jobs()}]),
                                  capture_output=True, text=True)
        self.assertEqual(complete.returncode, 0, complete.stderr)
        for data in ("[]", "{invalid json"):
            with self.subTest(data=data):
                refused = subprocess.run(command, input=data, capture_output=True, text=True)
                self.assertNotEqual(refused.returncode, 0)

    def test_repository_shards_cover_every_discovered_module_once(self):
        modules = SHARDS.discover_modules(ROOT / "tests")
        shards = SHARDS.partition_modules(modules, 4)
        SHARDS.validate_shards(modules, shards)
        self.assertEqual(Counter(name for shard in shards for name in shard), Counter(modules.keys()))
        timings = json.loads((ROOT / "scripts" / "test_shard_timings.json").read_text())
        totals = [sum(timings.get(name, 1000) for name in shard) for shard in shards]
        alphabetical = sorted(modules)
        previous = [sum(timings.get(name, 1000) for name in alphabetical[index::4]) for index in range(4)]
        self.assertLessEqual(max(totals), max(previous))
        self.assertLessEqual(max(totals) - min(totals), max(timings.values()))
        for shard in shards:
            self.assertEqual(list(shard), sorted(shard))
        direct = unittest.TestLoader().discover(str(ROOT / "tests"))
        assigned = unittest.TestSuite(suite for shard in shards for suite in shard.values())
        self.assertEqual(Counter(test_ids(assigned)), Counter(test_ids(direct)))

    def test_partition_is_deterministic_for_input_order(self):
        modules = {name: unittest.TestSuite() for name in ("c", "a", "b", "d", "e")}
        first = SHARDS.partition_modules(modules, 4, {})
        second = SHARDS.partition_modules(dict(reversed(list(modules.items()))), 4, {})
        self.assertEqual([list(shard) for shard in first], [["a", "e"], ["b"], ["c"], ["d"]])
        self.assertEqual(first, second)

    def test_longest_modules_are_balanced_before_shorter_modules(self):
        modules = {name: unittest.TestSuite() for name in "abcdef"}
        timings = dict(zip("abcdef", (10000, 9000, 8000, 7000, 6000, 5000)))
        shards = SHARDS.partition_modules(modules, 3, timings)
        self.assertEqual([list(shard) for shard in shards], [["a", "f"], ["b", "e"], ["c", "d"]])
        self.assertEqual([sum(timings[name] for name in shard) for shard in shards], [15000] * 3)

    def test_new_modules_without_timings_are_still_assigned_once(self):
        modules = {name: unittest.TestSuite() for name in ("known", "new_a", "new_b", "new_c")}
        shards = SHARDS.partition_modules(modules, 4, {"known": 8000, "removed": 9000})
        SHARDS.validate_shards(modules, shards)
        self.assertEqual([list(shard) for shard in shards], [["known"], ["new_a"], ["new_b"], ["new_c"]])

    def test_guard_rejects_a_missing_module(self):
        with self.assertRaisesRegex(ValueError, "exactly once"):
            SHARDS.validate_shards({"a": None, "b": None}, [{"a": None}])

    def test_guard_rejects_a_duplicate_module(self):
        with self.assertRaisesRegex(ValueError, "exactly once"):
            SHARDS.validate_shards({"a": None}, [{"a": None}, {"a": None}])

    def test_guard_rejects_an_extra_module(self):
        with self.assertRaisesRegex(ValueError, "exactly once"):
            SHARDS.validate_shards({"a": None}, [{"a": None, "b": None}])

    def test_invalid_shard_count_is_rejected(self):
        with self.assertRaises(ValueError):
            SHARDS.partition_modules({}, 0)

    def test_module_load_tests_hook_is_preserved(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "test_ci_hook.py").write_text(
                "import unittest\n"
                "class Example(unittest.TestCase):\n"
                "    def check_hook(self): pass\n"
                "def load_tests(loader, tests, pattern):\n"
                "    return unittest.TestSuite([Example('check_hook')])\n",
                encoding="utf-8",
            )
            modules = SHARDS.discover_modules(root)
            self.assertEqual(list(modules), ["test_ci_hook"])
            self.assertEqual(list(test_ids(modules["test_ci_hook"])), ["test_ci_hook.Example.check_hook"])

    def test_import_failure_is_rejected_before_running_shards(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "test_ci_broken.py").write_text("raise RuntimeError('broken import')\n", encoding="utf-8")
            with self.assertRaisesRegex(RuntimeError, "broken import"):
                SHARDS.discover_modules(root)

    def test_package_load_tests_keeps_its_discovered_children_together(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            package = root / "ci_hook_package"
            package.mkdir()
            (package / "__init__.py").write_text(
                "from pathlib import Path\n"
                "def load_tests(loader, tests, pattern):\n"
                "    return loader.discover(str(Path(__file__).parent), pattern=pattern)\n",
                encoding="utf-8",
            )
            (package / "test_child.py").write_text(
                "import unittest\n"
                "class Child(unittest.TestCase):\n"
                "    def test_child(self): pass\n",
                encoding="utf-8",
            )
            modules = SHARDS.discover_modules(root)
            self.assertEqual(list(modules), ["ci_hook_package"])
            self.assertEqual(list(test_ids(modules["ci_hook_package"])),
                             ["ci_hook_package.test_child.Child.test_child"])

    def test_empty_modules_are_included_in_the_partition(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "test_ci_empty.py").write_text("", encoding="utf-8")
            modules = SHARDS.discover_modules(root)
            self.assertEqual(list(modules), ["test_ci_empty"])
            self.assertEqual(modules["test_ci_empty"].countTestCases(), 0)
            SHARDS.validate_shards(modules, SHARDS.partition_modules(modules, 4))

    def test_import_skip_is_retained(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "test_ci_skip.py").write_text("import unittest\nraise unittest.SkipTest('optional')\n", encoding="utf-8")
            modules = SHARDS.discover_modules(root)
            self.assertEqual(sum(suite.countTestCases() for suite in modules.values()), 1)
            shards = SHARDS.partition_modules(modules, 4)
            SHARDS.validate_shards(modules, shards)


if __name__ == "__main__":
    unittest.main()
