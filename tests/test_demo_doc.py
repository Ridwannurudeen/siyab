"""docs/DEMO.md: the post text measured, the opening line the rules print, and the pitch sentence kept to it."""

import re
import shutil
import unittest
from pathlib import Path
from unittest import mock

from gate import demo

ROOT = Path(__file__).resolve().parent.parent
DEMO_DOC = ROOT / "docs" / "DEMO.md"
FIRST_LINE = ("Built at NEAR HACKATHON: security intelligence for NEAR with a source on every row. Who your agent pays "
              "gets a verdict with evidence, the guard refuses the rest, on NEAR and at the bridge.")
URL_PLACEHOLDER = "<repo URL>"
# The link weight and the length limit of the platform, both UNVERIFIED (docs/DEMO.md says so).
URL_WEIGHT = 23
LIMIT = 280
PITCH = "security intelligence, the way the best EVM products do it, with the evidence on chain"
POST = re.compile(r"<!-- post:start -->\n```\n(.*?)\n```\n<!-- post:end -->", re.DOTALL)


def no_network(*args, **kwargs):
    raise AssertionError("the demo opened the network")


def post_text(document):
    found = POST.findall(document)
    if len(found) != 1:
        raise AssertionError(f"expected one post block, found {len(found)}")
    return found[0]


class PostTest(unittest.TestCase):
    def setUp(self):
        self.document = DEMO_DOC.read_text(encoding="utf-8")
        self.post = post_text(self.document)

    def test_the_post_starts_with_the_fixed_first_line_and_names_the_hackathon(self):
        self.assertTrue(self.post.startswith(FIRST_LINE + " "))
        self.assertIn("Built at NEAR HACKATHON", self.post)
        self.assertIn("Track: AI Security & Defense.", self.post)
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


class OpeningLineTest(unittest.TestCase):
    def test_the_opening_line_is_the_line_the_offline_demo_prints_for_the_lookalike(self):
        printed = []
        with mock.patch("urllib.request.urlopen", side_effect=no_network):
            results = demo.run(out=printed.append)
        self.addCleanup(shutil.rmtree, Path(results[0]["evidence_path"]).parent)
        denied = [line.strip() for line in printed if line.strip().startswith(f"DENIED. {demo.LOOKALIKE} ")]
        self.assertEqual(len(denied), 1)
        document = DEMO_DOC.read_text(encoding="utf-8")
        opening = document[document.index("### 1."):document.index("### 2.")]
        self.assertIn("```\n" + denied[0] + "\n```", opening)
        self.assertNotIn("deposit.", denied[0])


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
