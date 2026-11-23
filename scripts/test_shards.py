"""Run one deterministic module shard of the normal unittest discovery suite."""

import argparse
import json
import sys
import unittest
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


class ModuleLoader(unittest.TestLoader):
    def __init__(self):
        super().__init__()
        self.module_names = {}
        self.module_depth = 0

    def loadTestsFromModule(self, module, *, pattern=None):
        self.module_depth += 1
        try:
            suite = super().loadTestsFromModule(module, pattern=pattern)
        finally:
            self.module_depth -= 1
        if self.module_depth == 0:
            self.module_names[id(suite)] = module.__name__
        return suite


def discover_modules(start_dir):
    loader = ModuleLoader()
    discovered = loader.discover(str(start_dir))
    if loader.errors:
        raise RuntimeError("\n".join(loader.errors))
    modules = {}
    for suite in discovered:
        name = loader.module_names.get(id(suite))
        if name is None:
            # Discovery represents a module skipped at import as a synthetic test.
            name = next(iter(suite)).id()
        if name in modules:
            raise ValueError(f"discovery repeated module {name}")
        modules[name] = suite
    return modules


def validate_shards(modules, shards):
    assigned = Counter(name for shard in shards for name in shard)
    if assigned != Counter(modules.keys()):
        raise ValueError("shards must contain every discovered module exactly once")


def partition_modules(modules, count, timings=None):
    if count < 1:
        raise ValueError("shard count must be positive")
    if timings is None:
        timings = json.loads((ROOT / "scripts" / "test_shard_timings.json").read_text())
    shards = [{} for _ in range(count)]
    totals = [0] * count
    for name in sorted(modules, key=lambda name: (-timings.get(name, 1000), name)):
        index = min(range(count), key=lambda index: (totals[index], len(shards[index]), index))
        shards[index][name] = modules[name]
        totals[index] += timings.get(name, 1000)
    shards = [dict(sorted(shard.items())) for shard in shards]
    validate_shards(modules, shards)
    return shards


def validate_jobs(pages):
    expected = Counter(f"unit-tests (Python {version}, shard {shard}/4)"
                       for version in ("3.11", "3.12") for shard in range(1, 5))
    jobs = [job for page in pages for job in page["jobs"] if job["name"].startswith("unit-tests")]
    if Counter(job["name"] for job in jobs) != expected:
        raise ValueError("unit test jobs must contain all eight shards exactly once")
    for job in jobs:
        if job["status"] != "completed" or job["conclusion"] != "success":
            raise ValueError(f"{job['name']} has not completed successfully")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--shard", type=int, help="one-based shard number")
    mode.add_argument("--check-jobs", action="store_true", help="check paginated workflow jobs from stdin")
    parser.add_argument("--shards", type=int, default=4, help="total shard count")
    args = parser.parse_args()
    if args.check_jobs:
        validate_jobs(json.load(sys.stdin))
        print("all eight unit test shards completed successfully")
        return 0
    if not 1 <= args.shard <= args.shards:
        parser.error("--shard must be between 1 and --shards")
    sys.path.insert(0, str(ROOT))
    modules = discover_modules(ROOT / "tests")
    shard = partition_modules(modules, args.shards)[args.shard - 1]
    print(f"shard {args.shard}/{args.shards}: {len(shard)} of {len(modules)} modules", flush=True)
    suite = unittest.TestSuite(shard.values())
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    raise SystemExit(main())
