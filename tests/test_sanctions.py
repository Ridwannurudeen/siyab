import contextlib
import hashlib
import io
import json
import re
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

from intel import __main__ as intel_main
from intel import sanctions

ROOT = Path(__file__).resolve().parent.parent
FETCHED_AT = "2026-11-07T20:00:00Z"
# A cut down SDN_ADVANCED.XML: entry 101 has an email feature, an ETH and a TRX address, a second name
# in another status, an alias and an entry on a second list; entry 102 has no address; entry 103 has
# one XBT address.
SAMPLE = b"""<?xml version="1.0" encoding="utf-8" standalone="yes"?>
<Sanctions xmlns="https://sanctionslistservice.ofac.treas.gov/api/PublicationPreview/exports/ADVANCED_XML" Version="3">
  <DateOfIssue CalendarTypeID="1">
    <Year>2026</Year>
    <Month>11</Month>
    <Day>6</Day>
  </DateOfIssue>
  <ReferenceValueSets>
    <DocNameStatusValues>
      <DocNameStatus ID="1">Primary Latin</DocNameStatus>
      <DocNameStatus ID="2">Others</DocNameStatus>
    </DocNameStatusValues>
    <FeatureTypeValues>
      <FeatureType ID="21" FeatureTypeGroupID="1">Email Address</FeatureType>
      <FeatureType ID="344" FeatureTypeGroupID="1">Digital Currency Address - XBT</FeatureType>
      <FeatureType ID="345" FeatureTypeGroupID="1">Digital Currency Address - ETH</FeatureType>
      <FeatureType ID="992" FeatureTypeGroupID="1">Digital Currency Address - TRX</FeatureType>
    </FeatureTypeValues>
    <ListValues>
      <List ID="1550">SDN List</List>
      <List ID="91512">Consolidated List</List>
    </ListValues>
    <SanctionsTypeValues>
      <SanctionsType ID="1705">Block</SanctionsType>
      <SanctionsType ID="1">Program</SanctionsType>
    </SanctionsTypeValues>
  </ReferenceValueSets>
  <DistinctParties>
    <DistinctParty FixedRef="101">
      <Profile ID="101" PartySubTypeID="4">
        <Identity ID="1" FixedRef="101" Primary="true" False="false">
          <Alias FixedRef="101" AliasTypeID="1403" Primary="true" LowQuality="false">
            <DocumentedName ID="11" FixedRef="101" DocNameStatusID="1">
              <DocumentedNamePart><NamePartValue NamePartGroupID="1">EXAMPLE</NamePartValue></DocumentedNamePart>
              <DocumentedNamePart><NamePartValue NamePartGroupID="2">Person</NamePartValue></DocumentedNamePart>
            </DocumentedName>
            <DocumentedName ID="12" FixedRef="101" DocNameStatusID="2">
              <DocumentedNamePart><NamePartValue NamePartGroupID="3">OTHER SCRIPT</NamePartValue></DocumentedNamePart>
            </DocumentedName>
          </Alias>
          <Alias FixedRef="101" AliasTypeID="1400" Primary="false" LowQuality="false">
            <DocumentedName ID="13" FixedRef="101" DocNameStatusID="1">
              <DocumentedNamePart><NamePartValue NamePartGroupID="4">Alias Name</NamePartValue></DocumentedNamePart>
            </DocumentedName>
          </Alias>
        </Identity>
        <Feature ID="1" FeatureTypeID="21">
          <FeatureVersion ID="1" ReliabilityID="1"><VersionDetail DetailTypeID="1432">person@example.org</VersionDetail></FeatureVersion>
        </Feature>
        <Feature ID="2" FeatureTypeID="345">
          <FeatureVersion ID="2" ReliabilityID="1"><VersionDetail DetailTypeID="1432">0xAbCdEf0123456789aBcDeF0123456789AbCdEf01</VersionDetail></FeatureVersion>
        </Feature>
        <Feature ID="3" FeatureTypeID="992">
          <FeatureVersion ID="3" ReliabilityID="1"><VersionDetail DetailTypeID="1432">TExampleTronAddressXyz123456789abc</VersionDetail></FeatureVersion>
        </Feature>
      </Profile>
    </DistinctParty>
    <DistinctParty FixedRef="102">
      <Profile ID="102" PartySubTypeID="3">
        <Identity ID="2" FixedRef="102" Primary="true" False="false">
          <Alias FixedRef="102" AliasTypeID="1403" Primary="true" LowQuality="false">
            <DocumentedName ID="21" FixedRef="102" DocNameStatusID="1">
              <DocumentedNamePart><NamePartValue NamePartGroupID="5">NO ADDRESS COMPANY</NamePartValue></DocumentedNamePart>
            </DocumentedName>
          </Alias>
        </Identity>
      </Profile>
    </DistinctParty>
    <DistinctParty FixedRef="103">
      <Profile ID="103" PartySubTypeID="3">
        <Identity ID="3" FixedRef="103" Primary="true" False="false">
          <Alias FixedRef="103" AliasTypeID="1403" Primary="true" LowQuality="false">
            <DocumentedName ID="31" FixedRef="103" DocNameStatusID="1">
              <DocumentedNamePart><NamePartValue NamePartGroupID="6">Example Exchange</NamePartValue></DocumentedNamePart>
            </DocumentedName>
          </Alias>
        </Identity>
        <Feature ID="4" FeatureTypeID="344">
          <FeatureVersion ID="4" ReliabilityID="1"><VersionDetail DetailTypeID="1432">bc1qexampleaddress</VersionDetail></FeatureVersion>
        </Feature>
      </Profile>
    </DistinctParty>
  </DistinctParties>
  <SanctionsEntries>
    <SanctionsEntry ID="101" ProfileID="101" ListID="1550">
      <SanctionsMeasure ID="1" SanctionsTypeID="1705" />
      <SanctionsMeasure ID="2" SanctionsTypeID="1"><Comment>CYBER2</Comment></SanctionsMeasure>
      <SanctionsMeasure ID="3" SanctionsTypeID="1"><Comment>SDGT</Comment></SanctionsMeasure>
    </SanctionsEntry>
    <SanctionsEntry ID="102" ProfileID="102" ListID="1550">
      <SanctionsMeasure ID="4" SanctionsTypeID="1"><Comment>IRAN</Comment></SanctionsMeasure>
    </SanctionsEntry>
    <SanctionsEntry ID="103" ProfileID="103" ListID="1550">
      <SanctionsMeasure ID="5" SanctionsTypeID="1"><Comment>TCO</Comment></SanctionsMeasure>
    </SanctionsEntry>
    <SanctionsEntry ID="10000101" ProfileID="101" ListID="91512">
      <SanctionsMeasure ID="6" SanctionsTypeID="1"><Comment>OTHER</Comment></SanctionsMeasure>
    </SanctionsEntry>
  </SanctionsEntries>
</Sanctions>
"""
SAMPLE_LISTINGS = [
    {"uid": 101, "name": "EXAMPLE Person", "programs": ["CYBER2", "SDGT"], "asset": "ETH",
     "address": "0xAbCdEf0123456789aBcDeF0123456789AbCdEf01", "key": "0xabcdef0123456789abcdef0123456789abcdef01"},
    {"uid": 101, "name": "EXAMPLE Person", "programs": ["CYBER2", "SDGT"], "asset": "TRX",
     "address": "TExampleTronAddressXyz123456789abc", "key": "TExampleTronAddressXyz123456789abc"},
    {"uid": 103, "name": "Example Exchange", "programs": ["TCO"], "asset": "XBT", "address": "bc1qexampleaddress",
     "key": "bc1qexampleaddress"},
]
# The NEAR Intents shared EVM treasury from intel/seeds/raw/near-intents-treasury-addresses-*.md.
UNLISTED_EVM = "0x2CfF890f0378a11913B6129B2E97417a2c302680"
EVM = re.compile(r"0x[0-9a-fA-F]{40}")


def no_network(*args, **kwargs):
    raise AssertionError("the lookup opened the network")


class ParseTest(unittest.TestCase):
    def test_each_digital_currency_feature_is_one_listing_with_its_entry(self):
        document = sanctions.extract(SAMPLE, FETCHED_AT)
        self.assertEqual(document, {
            "source": {"url": sanctions.SDN_URL, "content_sha256": hashlib.sha256(SAMPLE).hexdigest(),
                       "fetched_at": FETCHED_AT},
            "publish_date": "2026-11-06", "command": "python -m intel sanctions-fetch",
            "key_rule": sanctions.KEY_RULE, "listings": SAMPLE_LISTINGS})

    def test_a_doctype_or_an_entity_is_refused(self):
        for declaration in (b'<!DOCTYPE Sanctions [<!ENTITY a "a">]>', b'<!ENTITY a "a">'):
            data = SAMPLE.replace(b"<Sanctions ", declaration + b"\n<Sanctions ", 1)
            with self.subTest(declaration=declaration), self.assertRaises(sanctions.SanctionsError):
                sanctions.parse(data)

    def test_another_root_a_cut_file_or_no_date_of_issue_is_refused(self):
        cut = SAMPLE[: SAMPLE.index(b"<SanctionsEntries>")]
        no_date = re.sub(rb"<DateOfIssue.*?</DateOfIssue>", b"", SAMPLE, flags=re.DOTALL)
        for data in (SAMPLE.replace(b"Sanctions", b"sdnList"), cut, no_date):
            with self.assertRaises(sanctions.SanctionsError):
                sanctions.parse(data)

    def test_an_address_with_a_space_or_without_an_sdn_list_entry_is_refused(self):
        spaced = SAMPLE.replace(b"bc1qexampleaddress", b"bc1qexample address")
        unlisted = SAMPLE.replace(b'<SanctionsEntry ID="103" ProfileID="103" ListID="1550">',
                                  b'<SanctionsEntry ID="103" ProfileID="103" ListID="91512">')
        for data in (spaced, unlisted):
            with self.assertRaises(sanctions.SanctionsError):
                sanctions.parse(data)


class LookupTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.document = sanctions.load()
        cls.listings = cls.document["listings"]

    def setUp(self):
        patcher = mock.patch("urllib.request.urlopen", side_effect=no_network)
        patcher.start()
        self.addCleanup(patcher.stop)

    def lookup(self, chain_or_asset, address):
        return sanctions.is_sanctioned(chain_or_asset, address, self.document)

    def test_the_fixture_is_the_extract_the_command_writes(self):
        path = sorted(sanctions.FIXTURE_DIR.glob("ofac-sdn-addresses-*.json"))[-1]
        source = self.document["source"]
        self.assertEqual(source["url"], sanctions.SDN_URL)
        self.assertRegex(source["content_sha256"], r"\A[0-9a-f]{64}\Z")
        self.assertRegex(source["fetched_at"], r"\A\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z\Z")
        self.assertEqual(path.name, f"ofac-sdn-addresses-{re.sub('[-:]', '', source['fetched_at'])}.json")
        self.assertRegex(self.document["publish_date"], r"\A\d{4}-\d{2}-\d{2}\Z")
        self.assertEqual((self.document["command"], self.document["key_rule"]), (sanctions.COMMAND, sanctions.KEY_RULE))
        self.assertTrue(self.listings)
        for listing in self.listings:
            self.assertEqual(list(listing), ["uid", "name", "programs", "asset", "address", "key"])
            self.assertEqual(listing["key"], sanctions.normalise(listing["address"]))
            self.assertTrue(listing["asset"].isupper() and listing["programs"] and listing["name"])

    def test_every_listed_address_matches_with_its_uid(self):
        for listing in self.listings:
            chain = "eth" if EVM.fullmatch(listing["address"]) else "tron"
            for query in (listing["asset"], chain):
                found = [(m["uid"], m["asset"], m["address"]) for m in self.lookup(query, listing["address"])]
                self.assertIn((listing["uid"], listing["asset"], listing["address"]), found)

    def test_a_match_names_the_entry_the_list_date_and_the_file_hash(self):
        listing = self.listings[0]
        [match] = [m for m in self.lookup(listing["asset"], listing["address"]) if m["uid"] == listing["uid"]]
        self.assertEqual(match, {"uid": listing["uid"], "name": listing["name"], "programs": listing["programs"],
                                 "asset": listing["asset"], "address": listing["address"],
                                 "publish_date": self.document["publish_date"],
                                 "source_sha256": self.document["source"]["content_sha256"]})

    def test_an_unlisted_address_does_not_match(self):
        keys = {listing["key"] for listing in self.listings}
        tron = next(listing["address"] for listing in self.listings if listing["asset"] == "TRX")
        changed = tron[:-1] + ("A" if tron[-1] != "A" else "B")
        self.assertNotIn(changed, keys)
        for chain, address in (("eth", UNLISTED_EVM), ("base", UNLISTED_EVM), ("ETH", UNLISTED_EVM),
                               ("tron", changed), ("TRX", changed)):
            self.assertEqual(self.lookup(chain, address), [])

    def test_evm_case_is_ignored_on_every_chain_code(self):
        listing = next(listing for listing in self.listings
                       if listing["asset"] == "ETH" and listing["address"] != listing["address"].lower())
        lower = listing["address"].lower()
        upper = "0x" + lower[2:].upper()
        for chain, address in (("eth", lower), ("base", upper), ("arb", listing["address"]), ("ETH", upper)):
            self.assertIn(listing["uid"], [m["uid"] for m in self.lookup(chain, address)])

    def test_a_chain_code_matches_every_code_and_an_asset_code_only_its_own(self):
        codes = {}
        for listing in self.listings:
            codes.setdefault(listing["key"], set()).add(listing["asset"])
        key = next(key for key, assets in codes.items() if EVM.fullmatch(key) and len(assets) > 1)
        self.assertEqual({m["asset"] for m in self.lookup("eth", key)}, codes[key])
        for asset in codes[key]:
            self.assertEqual({m["asset"] for m in self.lookup(asset, key)}, {asset})

    def test_other_addresses_compare_as_published(self):
        keys = {listing["key"] for listing in self.listings}
        listing = next(listing for listing in self.listings
                       if listing["asset"] == "TRX" and listing["address"].lower() not in keys)
        self.assertEqual(self.lookup("tron", listing["address"].lower()), [])
        self.assertEqual(self.lookup("TRX", listing["address"].lower()), [])

    def test_a_near_account_never_matches(self):
        self.assertEqual(sanctions.counts(self.document)["near"], 0)
        self.assertNotIn(sanctions.NEAR_ASSET, {listing["asset"] for listing in self.listings})
        for listing in self.listings:
            self.assertEqual(self.lookup("near", listing["address"]), [])
            self.assertEqual(self.lookup("near", listing["key"]), [])
        # A 64 hex listing reads like a NEAR implicit account; it matches under its own code only.
        implicit = next(listing for listing in self.listings if re.fullmatch(r"[0-9a-f]{64}", listing["address"]))
        self.assertEqual(self.lookup("near", implicit["address"]), [])
        self.assertIn(implicit["uid"], [m["uid"] for m in self.lookup(implicit["asset"], implicit["address"])])

    def test_a_listing_under_a_near_code_matches_on_near(self):
        document = {**self.document, "listings": [{"uid": 1, "name": "N", "programs": ["P"], "asset": "NEAR",
                                                   "address": "example.near", "key": "example.near"}]}
        self.assertEqual([m["uid"] for m in sanctions.is_sanctioned("near", "example.near", document)], [1])

    def test_the_lookup_reads_the_committed_fixture_without_the_network(self):
        listing = self.listings[0]
        self.assertIn(listing["uid"], [m["uid"] for m in sanctions.is_sanctioned(listing["asset"], listing["address"])])


class CommandTest(unittest.TestCase):
    def run_command(self, opener):
        stdout, stderr = io.StringIO(), io.StringIO()
        with mock.patch("urllib.request.urlopen", opener), contextlib.redirect_stdout(stdout), \
                contextlib.redirect_stderr(stderr):
            code = intel_main.main(["sanctions-fetch"])
        return code, stdout.getvalue(), stderr.getvalue()

    def test_sanctions_fetch_writes_the_extract_and_prints_its_counts(self):
        opener = mock.MagicMock()
        opener.return_value.__enter__.return_value.read.return_value = SAMPLE
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(sanctions, "FIXTURE_DIR", Path(tmp) / "sanctions"):
            code, stdout, _ = self.run_command(opener)
            [path] = sorted((Path(tmp) / "sanctions").iterdir())
            document = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(code, 0)
        request = opener.call_args.args[0]
        self.assertEqual((request.full_url, request.get_header("User-agent")), (sanctions.SDN_URL, "shieldbot-near"))
        self.assertEqual(document, sanctions.extract(SAMPLE, document["source"]["fetched_at"]))
        self.assertEqual(path.name, f"ofac-sdn-addresses-{re.sub('[-:]', '', document['source']['fetched_at'])}.json")
        self.assertIn(sanctions.counts_line(sanctions.counts(document)), stdout)
        self.assertIn("3 listings, 3 distinct (asset, address) pairs, 3 distinct addresses, 2 entries, 3 asset codes, "
                      "NEAR 0", stdout)

    def test_a_failed_fetch_or_a_file_that_does_not_parse_writes_nothing(self):
        broken = mock.MagicMock()
        broken.return_value.__enter__.return_value.read.return_value = b"<html>not the list</html>"
        for opener in (mock.MagicMock(side_effect=urllib.error.URLError("no route")), broken):
            with tempfile.TemporaryDirectory() as tmp, \
                    mock.patch.object(sanctions, "FIXTURE_DIR", Path(tmp) / "sanctions"):
                code, stdout, stderr = self.run_command(opener)
                self.assertFalse((Path(tmp) / "sanctions").exists())
            self.assertEqual((code, stdout), (1, ""))
            self.assertTrue(stderr.startswith("stopped: "))


class DocsTest(unittest.TestCase):
    def test_the_sanctions_page_states_what_the_fixture_holds(self):
        document = sanctions.load()
        digest = document["source"]["content_sha256"]
        text = (ROOT / "docs" / "SANCTIONS.md").read_text(encoding="utf-8")
        for fact in (sanctions.counts_line(sanctions.counts(document)), document["publish_date"],
                     document["source"]["fetched_at"], f"{digest[:4]}...{digest[-4:]}", sanctions.SDN_URL):
            self.assertIn(fact, text)

    def test_the_notice_names_the_source_and_its_terms(self):
        text = (ROOT / "intel" / "seeds" / "raw" / "NOTICE.md").read_text(encoding="utf-8")
        for fact in (sanctions.SDN_URL, "17 U.S.C. 105", "docs/SANCTIONS.md"):
            self.assertIn(fact, text)


if __name__ == "__main__":
    unittest.main()
