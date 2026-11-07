"""Version claims in the README and docs agree with the crate manifests and the build records.

A source claim ("Gate source is 0.2.1", "current Gate source 0.2.1", "Gate 0.2.1 is built", "Reports source is
0.1.2", "current source is Gate 0.2.1 and Reports 0.1.2") must equal the crate's Cargo.toml version. A deployed
claim ("testnet still runs Gate 0.1.0", "testnet runs deployed Reports version 0.1.1") must equal the version
on testnet read from res/BUILD-<crate>.md: the version on the Deployed line, or, while that line says pending,
the version of the "Previous deploy" line. docs/LOG.md and docs/DECISIONS.md are dated history and are skipped.
"""

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SKIP = {"docs/LOG.md", "docs/DECISIONS.md"}
VERSION = r"`?(\d+\.\d+\.\d+)`?"
CRATES = {"gate": "Gate", "reports": "Reports"}


def source_version(crate):
    text = (ROOT / "contracts" / crate / "Cargo.toml").read_text(encoding="utf-8")
    return re.search(r'^version = "(\d+\.\d+\.\d+)"', text, re.MULTILINE).group(1)


def deployed_version(record):
    deployed = re.search(r"^Deployed: (.*)$", record, re.MULTILINE).group(1)
    if deployed.startswith("pending"):
        previous = re.search(r"^Previous deploy, version (\d+\.\d+\.\d+)", record, re.MULTILINE)
        return previous.group(1)
    return re.search(r"version (\d+\.\d+\.\d+)", deployed).group(1)


def source_patterns(title):
    return [
        rf"{title} source(?: is)?(?: now)? {VERSION}",
        rf"[Cc]urrent {title} source(?: is)? {VERSION}",
        rf"\b{title} {VERSION} is built",
        rf"current source is (?:Gate {VERSION} and )?{title} {VERSION}" if title == "Reports" else
        rf"current source is {title} {VERSION}",
    ]


def deployed_patterns(title):
    return [
        rf"(?:testnet )?still runs (?:deployed )?{title}(?: version)? {VERSION}",
        rf"testnet runs (?:deployed )?{title}(?: version)? {VERSION}",
    ]


def claims(text, patterns):
    found = []
    for pattern in patterns:
        for match in re.finditer(pattern, text):
            found.append((match.group(0), [g for g in match.groups() if g][-1]))
    return found


def documents():
    paths = [ROOT / "README.md", *sorted((ROOT / "docs").rglob("*.md"))]
    return [p for p in paths if p.relative_to(ROOT).as_posix() not in SKIP]


def mismatches(texts, crate, source, deployed):
    title = CRATES[crate]
    bad = []
    for name, text in texts:
        for phrase, version in claims(text, source_patterns(title)):
            if version != source:
                bad.append(f"{name}: '{phrase}' but contracts/{crate}/Cargo.toml says {source}")
        for phrase, version in claims(text, deployed_patterns(title)):
            if version != deployed:
                bad.append(f"{name}: '{phrase}' but res/BUILD-{crate}.md says {deployed} on testnet")
    return bad


class VersionClaimsTest(unittest.TestCase):
    def texts(self):
        return [(p.relative_to(ROOT).as_posix(), p.read_text(encoding="utf-8")) for p in documents()]

    def test_every_version_claim_matches_the_manifest_and_the_build_record(self):
        texts = self.texts()
        for crate in CRATES:
            with self.subTest(crate=crate):
                record = (ROOT / "res" / f"BUILD-{crate}.md").read_text(encoding="utf-8")
                self.assertEqual(mismatches(texts, crate, source_version(crate), deployed_version(record)), [])

    def test_the_claims_are_found(self):
        joined = "\n".join(text for _, text in self.texts())
        self.assertTrue(claims(joined, source_patterns("Gate")))
        self.assertTrue(claims(joined, deployed_patterns("Gate")))

    def test_a_stale_claim_fails(self):
        texts = [("x.md", "Gate source is `9.9.9`; testnet still runs Gate 8.8.8.")]
        bad = mismatches(texts, "gate", "0.2.1", "0.1.0")
        self.assertEqual(len(bad), 2, bad)

    def test_deployed_version_reads_pending_and_deployed_records(self):
        pending = "Deployed: pending\n\nPrevious deploy, version 0.1.0 from source commit x\n"
        self.assertEqual(deployed_version(pending), "0.1.0")
        done = "Deployed: 7 Nov, `r.testnet`, version 0.1.1, code_hash `h` MATCH\n"
        self.assertEqual(deployed_version(done), "0.1.1")


if __name__ == "__main__":
    unittest.main()
