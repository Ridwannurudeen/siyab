"""docs/REPORTING.md and docs/service_times.json: the published service times are one set of numbers."""

import json
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
POLICY = ROOT / "docs" / "REPORTING.md"
TIMES = ROOT / "docs" / "service_times.json"
ROW = re.compile(r"^\| `([a-z_]+)` \| ([0-9]+) hours \|", re.MULTILINE)
HOURS = re.compile(r"\b([0-9]+) hours?\b")
PROSE_TIME = re.compile(r"`([a-z_]+)` step is ([0-9]+) hours")


def prose_times_match(policy, expected):
    prose = "\n".join(line for line in policy.splitlines() if not line.lstrip().startswith("|"))
    pairs = PROSE_TIME.findall(prose)
    return (
        len(HOURS.findall(prose)) == len(pairs)
        and len(pairs) == len(expected)
        and {step: int(hours) for step, hours in pairs} == expected
    )


class ServiceTimesTest(unittest.TestCase):
    def setUp(self):
        self.policy = POLICY.read_text(encoding="utf-8")
        self.times = json.loads(TIMES.read_text(encoding="utf-8"))

    def test_the_json_parses_with_whole_hours(self):
        self.assertEqual(set(self.times), {"version", "policy", "hours"})
        self.assertEqual(self.times["version"], 1)
        self.assertEqual(self.times["policy"], "docs/REPORTING.md")
        self.assertEqual(set(self.times["hours"]), {"acknowledge", "decide", "dispute_decision"})
        for step, hours in self.times["hours"].items():
            with self.subTest(step=step):
                self.assertIs(type(hours), int)
                self.assertGreater(hours, 0)

    def test_the_policy_table_equals_the_json(self):
        table = {step: int(hours) for step, hours in ROW.findall(self.policy)}
        self.assertEqual(table, self.times["hours"])

    def test_every_time_the_policy_states_is_a_json_value(self):
        stated = {int(hours) for hours in HOURS.findall(self.policy)}
        self.assertEqual(stated, set(self.times["hours"].values()))

    def test_each_prose_service_time_matches_its_json_step(self):
        self.assertTrue(prose_times_match(self.policy, self.times["hours"]))

    def test_a_prose_time_assigned_to_the_wrong_step_fails(self):
        fixture = (
            "The `acknowledge` step is 72 hours; "
            "the `decide` step is 72 hours; "
            "the `dispute_decision` step is 168 hours."
        )
        self.assertFalse(prose_times_match(fixture, self.times["hours"]))

    def test_the_policy_cites_the_json(self):
        self.assertIn("`docs/service_times.json`", self.policy)


if __name__ == "__main__":
    unittest.main()
