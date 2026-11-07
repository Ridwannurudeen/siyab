"""The README first screen and its Limits section agree with docs/CLAIMS.md and with the build records.

Waiting against claimed. The waiting items are the comma separated phrases between "What waits:" and the
first full stop of that bullet in the README's "## Limits" section. Each phrase is reduced to its key: the
lower case words, with the words in STOPWORDS dropped and a plural s cut from words over three letters,
so "numeric risk ratings" becomes {numeric, risk, rating}. A Claim cell of docs/CLAIMS.md claims an item
done when the cell's words, reduced the same way, hold every word of the item's key and the cell does not
state that the thing is absent (NEGATED, as in "A TEE is not built."). A waiting item claimed done fails.

Versions. The first screen is the text before the first second level heading. It names a contract version
only in two forms: "the <contract> runs X.Y.Z", the version on testnet, and "<contract> X.Y.Z is built",
the version of the crate's Cargo.toml. The version on testnet is read from res/BUILD-<crate>.md: the
version on its Deployed line; while that line reads "Deployed: pending", the version of the Previous
deploy line that says it is still the code on testnet; when the Deployed line names no version, the crate
version of Cargo.toml at the Source commit the record names. When the sentence of an "is built" form says
"not deployed", the record must read "Deployed: pending". Any other version string on the first screen
fails, so a version cannot be stated outside the two checked forms.
"""

import re
import shutil
import subprocess
import tempfile
import tomllib
import unittest
from pathlib import Path

from tests.test_attack_evidence import table_rows

ROOT = Path(__file__).resolve().parent.parent
README = ROOT / "README.md"
CLAIMS = ROOT / "docs" / "CLAIMS.md"
CLAIMS_HEADER = "| Claim | File:function |"
STOPWORDS = {"a", "an", "and", "any", "for", "in", "of", "on", "the", "to"}
NEGATED = re.compile(r"\bnot built\b|\bnot yet\b|\bnone\b", re.IGNORECASE)
CRATES = {"Gate": "gate", "registry": "registry", "reports contract": "reports"}
NAME = "(Gate|registry|reports contract)"
VERSION = r"(\d+\.\d+\.\d+)"
LIVE = re.compile(rf"\bthe {NAME} runs {VERSION}")
BUILT = re.compile(rf"\b{NAME} {VERSION} is built")
ANY_VERSION = re.compile(r"\b\d+\.\d+\.\d+\b")


def key(phrase):
    words = re.findall(r"[a-z0-9]+", phrase.lower())
    return frozenset(word[:-1] if len(word) > 3 and word.endswith("s") else word
                     for word in words if word not in STOPWORDS)


def waiting_items(readme_text):
    limits = readme_text.split("\n## Limits\n", 1)[1].split("\n## ", 1)[0]
    lines = [line for line in limits.splitlines() if line.startswith("- What waits:")]
    if len(lines) != 1:
        raise AssertionError(f"expected one What waits line in Limits, found {len(lines)}")
    listed = lines[0][len("- What waits:"):].split(".", 1)[0]
    return [re.sub(r"^and ", "", item.strip()) for item in listed.split(",") if item.strip()]


def waiting_claimed_done(readme_text, claims_text):
    """Every (waiting item, claim) pair in which the claims table states the waiting item as done."""
    claims = [row[0] for row in table_rows(claims_text, CLAIMS_HEADER)]
    if not claims:
        raise AssertionError("no rows in the claims table")
    hits = []
    for item in waiting_items(readme_text):
        for claim in claims:
            if key(item) <= key(claim) and not NEGATED.search(claim):
                hits.append((item, claim))
    return hits


def first_screen(readme_text):
    return readme_text[:readme_text.index("\n## ")]


def cargo_version(contracts, crate):
    return tomllib.loads((contracts / crate / "Cargo.toml").read_text(encoding="utf-8"))["package"]["version"]


def build_record(res, crate):
    return (res / f"BUILD-{crate}.md").read_text(encoding="utf-8")


def deployed_line(record):
    lines = [line for line in record.splitlines() if line.startswith("Deployed:")]
    if len(lines) != 1:
        raise AssertionError(f"expected one Deployed line, found {len(lines)}")
    return lines[0].strip()


def testnet_version(res, crate):
    record = build_record(res, crate)
    line = deployed_line(record)
    if line == "Deployed: pending":
        previous = [match.group(1) for match in re.finditer(r"^Previous deploy, version (\d+\.\d+\.\d+)\b(.*)$",
                                                            record, re.MULTILINE)
                    if "still the code on testnet" in match.group(2)]
        if len(previous) != 1:
            raise AssertionError(f"BUILD-{crate}.md is pending and names no previous deploy still on testnet")
        return previous[0]
    stated = re.search(r"\bversion (\d+\.\d+\.\d+)", line)
    if stated:
        return stated.group(1)
    commit = re.search(r"^Source commit: ([0-9a-f]{7,40})$", record, re.MULTILINE).group(1)
    manifest = subprocess.run(["git", "show", f"{commit}:contracts/{crate}/Cargo.toml"], cwd=ROOT, check=True,
                              capture_output=True, text=True).stdout
    return tomllib.loads(manifest)["package"]["version"]


def version_errors(readme_text, res, contracts):
    screen = first_screen(readme_text)
    errors = []
    checked = []
    for match in LIVE.finditer(screen):
        name, stated = match.group(1), match.group(2)
        crate = CRATES[name]
        checked.append(match.span(2))
        on_testnet = testnet_version(res, crate)
        if stated != on_testnet:
            errors.append(f"the first screen says the {name} runs {stated}; res/BUILD-{crate}.md puts "
                          f"{on_testnet} on testnet")
    for match in BUILT.finditer(screen):
        name, stated = match.group(1), match.group(2)
        crate = CRATES[name]
        checked.append(match.span(2))
        built = cargo_version(contracts, crate)
        if stated != built:
            errors.append(f"the first screen says {name} {stated} is built; contracts/{crate}/Cargo.toml says "
                          f"{built}")
        end = screen.find("\n", match.start())
        sentence = screen[match.start():end if end != -1 else len(screen)]
        if "not deployed" in sentence and deployed_line(build_record(res, crate)) != "Deployed: pending":
            errors.append(f"the first screen says {name} {stated} is not deployed; res/BUILD-{crate}.md has a "
                          "Deployed line")
    for match in ANY_VERSION.finditer(screen):
        if match.span() not in checked:
            errors.append(f"the first screen names version {match.group(0)} outside the checked forms")
    return errors


class WaitingIsNotClaimedTest(unittest.TestCase):
    def test_the_what_waits_line_has_items(self):
        items = waiting_items(README.read_text(encoding="utf-8"))
        self.assertGreater(len(items), 3)
        self.assertIn("billing", items)

    def test_no_waiting_item_is_claimed_done(self):
        self.assertEqual(waiting_claimed_done(README.read_text(encoding="utf-8"),
                                              CLAIMS.read_text(encoding="utf-8")), [])

    def test_a_planted_claim_of_a_waiting_item_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            claims = Path(tmp) / "CLAIMS.md"
            text = CLAIMS.read_text(encoding="utf-8").rstrip("\n")
            claims.write_text(text + "\n| Exchange labels are recorded for every NEAR exchange account. | "
                              "`intel/labels.py:validate` | `tests/test_labels.py::X.test_y` | offline | no |\n",
                              encoding="utf-8")
            hits = waiting_claimed_done(README.read_text(encoding="utf-8"), claims.read_text(encoding="utf-8"))
        self.assertEqual([item for item, _ in hits], ["exchange labels"])

    def test_a_negated_claim_is_not_a_done_claim(self):
        claims = (f"{CLAIMS_HEADER} Test | Evidence | idea |\n|---|---|---|---|---|\n"
                  "| Billing is not built. | a | b | c | no |\n")
        self.assertEqual(waiting_claimed_done(README.read_text(encoding="utf-8"), claims), [])


class FirstScreenVersionTest(unittest.TestCase):
    def copy_tree(self, tmp):
        res, contracts = Path(tmp) / "res", Path(tmp) / "contracts"
        res.mkdir()
        for crate in set(CRATES.values()):
            shutil.copyfile(ROOT / "res" / f"BUILD-{crate}.md", res / f"BUILD-{crate}.md")
            (contracts / crate).mkdir(parents=True)
            shutil.copyfile(ROOT / "contracts" / crate / "Cargo.toml", contracts / crate / "Cargo.toml")
        return res, contracts

    def test_the_first_screen_names_versions_in_the_checked_forms(self):
        screen = first_screen(README.read_text(encoding="utf-8"))
        self.assertEqual({match.group(1) for match in LIVE.finditer(screen)}, set(CRATES))
        self.assertTrue(BUILT.search(screen))

    def test_the_first_screen_versions_match_the_build_records_and_manifests(self):
        self.assertEqual(version_errors(README.read_text(encoding="utf-8"), ROOT / "res", ROOT / "contracts"), [])

    def test_a_wrong_testnet_version_fails(self):
        readme = README.read_text(encoding="utf-8").replace("the Gate runs 0.1.0", "the Gate runs 0.2.0", 1)
        with tempfile.TemporaryDirectory() as tmp:
            errors = version_errors(readme, *self.copy_tree(tmp))
        self.assertEqual(len(errors), 1)
        self.assertIn("runs 0.2.0", errors[0])

    def test_a_deployed_gate_record_fails_the_pending_claims(self):
        with tempfile.TemporaryDirectory() as tmp:
            res, contracts = self.copy_tree(tmp)
            record = res / "BUILD-gate.md"
            record.write_text(record.read_text(encoding="utf-8").replace(
                "Deployed: pending", "Deployed: 8 Nov, `kestrelridge-main.testnet`, version 0.2.0"), encoding="utf-8")
            errors = version_errors(README.read_text(encoding="utf-8"), res, contracts)
        self.assertEqual(len(errors), 2)
        self.assertIn("puts 0.2.0 on testnet", errors[0])
        self.assertIn("has a Deployed line", errors[1])

    def test_a_built_version_off_the_manifest_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            res, contracts = self.copy_tree(tmp)
            manifest = contracts / "gate" / "Cargo.toml"
            text = manifest.read_text(encoding="utf-8")
            manifest.write_text(text.replace('version = "0.2.0"', 'version = "0.3.0"', 1), encoding="utf-8")
            errors = version_errors(README.read_text(encoding="utf-8"), res, contracts)
        self.assertEqual(len(errors), 1)
        self.assertIn("Cargo.toml says 0.3.0", errors[0])

    def test_a_version_outside_the_checked_forms_fails(self):
        readme = README.read_text(encoding="utf-8").replace("Not yet: ", "Not yet: version 9.9.9 waits; ", 1)
        with tempfile.TemporaryDirectory() as tmp:
            errors = version_errors(readme, *self.copy_tree(tmp))
        self.assertEqual(errors, ["the first screen names version 9.9.9 outside the checked forms"])


if __name__ == "__main__":
    unittest.main()
