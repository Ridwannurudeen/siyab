"""docs/DEMO.md: the post text measured, the line the attack shot quotes from the offline replay, and the pitch
sentence kept to it."""

import re
import unittest
from pathlib import Path
from unittest import mock

from gate import injected

ROOT = Path(__file__).resolve().parent.parent
DEMO_DOC = ROOT / "docs" / "DEMO.md"
FIRST_LINE = "A poisoned invoice tells an AI agent to pay an account an exploit report names."
URL_PLACEHOLDER = "<repo URL>"
# The link weight and the length limit of the platform, both UNVERIFIED (docs/DEMO.md says so).
URL_WEIGHT = 23
LIMIT = 280
PITCH = "security intelligence, the way the best EVM products do it, with the evidence on chain"
POST = re.compile(r"<!-- post:start -->\n```\n(.*?)\n```\n<!-- post:end -->", re.DOTALL)
# scripts/check_hygiene.py rejects a 64 character hex run in any .md under docs/, so the document quotes each run
# shortened, as the README does.
HEX64 = re.compile(r"[0-9a-fA-F]{64}")


def no_network(*args, **kwargs):
    raise AssertionError("the replay opened the network")


def post_text(document):
    found = POST.findall(document)
    if len(found) != 1:
        raise AssertionError(f"expected one post block, found {len(found)}")
    return found[0]


def shortened(line):
    """The line with every 64 character hex run cut to its leading eight characters, three dots and its trailing
    eight."""
    return HEX64.sub(lambda match: f"{match.group(0)[:8]}...{match.group(0)[-8:]}", line)


class PostTest(unittest.TestCase):
    def setUp(self):
        self.document = DEMO_DOC.read_text(encoding="utf-8")
        self.post = post_text(self.document)

    def test_the_post_starts_with_the_fixed_first_line_and_names_the_hackathon(self):
        self.assertTrue(self.post.startswith(FIRST_LINE + " "))
        self.assertIn("Built at NEAR HACKATHON", self.post)
        self.assertIn("@NEARProtocol", self.post)
        self.assertIn("#NEARHackathon", self.post)
        self.assertEqual(self.post.count("?"), 1)
        self.assertEqual(self.post.count(URL_PLACEHOLDER), 1)
        self.assertTrue(self.post.endswith(URL_PLACEHOLDER))

    def test_the_post_is_under_the_limit_with_the_url_counted_as_23(self):
        before_url = len(self.post) - len(URL_PLACEHOLDER)
        weighted = before_url + URL_WEIGHT
        self.assertLessEqual(weighted, LIMIT)
        self.assertIn(f"{before_url} characters before the URL, counting the space before it, {weighted} with the URL "
                      f"counted as {URL_WEIGHT}",
                      self.document)

    def test_both_limits_are_marked_unverified(self):
        after = self.document[self.document.index("<!-- post:end -->"):]
        self.assertIn("UNVERIFIED", after)
        self.assertIn(f"{LIMIT} character limit", after)
        self.assertIn(f"{URL_WEIGHT} character weight", after)

    def test_the_post_holds_one_line_and_the_full_name_is_never_shortened(self):
        self.assertNotIn("\n", self.post)
        self.assertIsNone(re.search(r"Shield(?!Bot)", self.post))


class AttackShotTest(unittest.TestCase):
    def test_the_attack_shot_quotes_the_line_the_offline_replay_prints(self):
        printed = []
        with mock.patch("urllib.request.urlopen", side_effect=no_network):
            self.assertEqual(injected.run(out=printed.append), 0)
        denied = [line.strip() for line in printed if line.strip().startswith("DENIED.")]
        self.assertEqual(len(denied), 1)
        document = DEMO_DOC.read_text(encoding="utf-8")
        shot = document[document.index("### 4."):document.index("### 5.")]
        self.assertIn("```\n" + shortened(denied[0]) + "\n```", shot)


class PitchSentenceTest(unittest.TestCase):
    def test_the_pitch_sentence_appears_once_in_the_narration_and_nowhere_else(self):
        document = DEMO_DOC.read_text(encoding="utf-8")
        self.assertEqual(document.count(PITCH), 1)
        self.assertIn(f"Narration, the one sentence of pitch voice: \"{PITCH}\"", document)
        for name in ("README.md", "AGENTS.md"):
            with self.subTest(file=name):
                self.assertNotIn(PITCH, (ROOT / name).read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
