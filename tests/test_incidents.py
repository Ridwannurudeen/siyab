import contextlib
import io
import json
import re
import unittest
from pathlib import Path
from unittest import mock

from gate import __main__ as gate_main
from gate import incident
from intel import labels, seed

ROOT = Path(__file__).resolve().parent.parent
RHEA_PATH = ROOT / "intel" / "incidents" / "rhea.json"
COLLECTOR = "31ac7a2705a0686ff427b1a52d3ffd1fcfaa4b1f3cb3e83a0f767494e724a540"
TOKENS = ("c122aad2a188ed3abec0517f99c4dd03591d19e6bfcf7f015424351fbdd71163",
          "7d06ad005e03aa875875481fb6a2bd1acf9e722100ab8747e4e97801ba340640",
          "9c0df321dd5d9fc1138a09f405f523333fffe74de2ee10932ff756e27ae930ee",
          "80f357d92e67f0ea53f299bb16d3af363ba0ef66e0211091edc2181cd323a705")
WORKERS = ("d4e8e706f5a59610d85924af858f0562031930d14eb7add9a433edea50cf0b03",
           "d39fd6da5eda882fe5f6716212552356cde2613054c670ffd6b49b3b25aef473",
           "839810a1aef8ed1adb03e795b2a9785d783d4e2f415ab80c8b6fb30483e160f2",
           "9053223460198e5e1e54dc15c18295ff0e98dca41ecd6c3872da1ff634d99616",
           "5a7695e3626aeae7c0d340b6e58f813c71455d1be490eeb44e90bd9682343040")
MASTER = "72633832db9d039b97906320169a8411bc2fa0f78303a4ecb1d35de71af621c5"
ETH_WALLET = "0xbb5fa936469cadb8907f3aef80f5b53f55bc11f6"
CODE_HASH = "BBeoVgxZC5Ce1ef7nErevX98QonNsedBFfqzep5RV1Vu"
BARRED = re.compile(r"scam|clone|fake|drainer", re.IGNORECASE)
HEX64 = re.compile(r"[0-9a-f]{64}")
TIME = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z")
# The PLAN-v3 section 3c incident schema: key -> type, plus the keys each list item holds.
SCHEMA = {"id": str, "title": str, "date": str, "chains": list, "sources": list, "accounts": list,
          "onchain_facts": list, "rules_today": list, "not_public": list, "standard": str}
ITEM_KEYS = {
    "sources": {"id", "publisher", "url", "content_sha256", "fetched_at"},
    "accounts": {"subject", "chain", "role_as_reported", "quote", "assertion_ids"},
    "onchain_facts": {"subject", "method", "args", "block", "result_sha256", "summary"},
    "rules_today": {"subject", "as", "request", "verdict", "checks", "reasons", "labels"},
}


def no_network(*args, **kwargs):
    raise AssertionError("the test opened the network")


def strings(value, path=()):
    """Every string in value with the path of keys and indexes that leads to it."""
    if isinstance(value, str):
        yield path, value
    elif isinstance(value, dict):
        for key, item in value.items():
            yield from strings(item, path + (key,))
    elif isinstance(value, list):
        for index, item in enumerate(value):
            yield from strings(item, path + (index,))


def barred_words_outside_quotes(document):
    """(path, text) for each string with a barred word that is not the text of a quote object
    whose source is one of the document's sources."""
    sources = {source["id"] for source in document.get("sources", [])}
    hits = []
    for path, text in strings(document):
        if not BARRED.search(text):
            continue
        holder = document
        for step in path[:-1]:
            holder = holder[step]
        quoted = (len(path) >= 2 and path[-2] == "quote" and path[-1] == "text"
                  and holder.get("source") in sources)
        if not quoted:
            hits.append((path, text))
    return hits


class RheaIncidentTest(unittest.TestCase):
    def setUp(self):
        self.document = json.loads(RHEA_PATH.read_text(encoding="utf-8"))
        self.store = labels.load()
        self.ids = {a.id: a for a in self.store.assertions}

    def test_the_file_matches_the_schema(self):
        for key, kind in SCHEMA.items():
            with self.subTest(key=key):
                self.assertIsInstance(self.document[key], kind)
        self.assertEqual(self.document["id"], "rhea")
        self.assertEqual(self.document["date"], "2026-04-16")
        for key, keys in ITEM_KEYS.items():
            self.assertTrue(self.document[key], key)
            for item in self.document[key]:
                with self.subTest(key=key, item=str(item)[:80]):
                    self.assertEqual(set(item), keys)
        for source in self.document["sources"]:
            self.assertTrue(source["url"].startswith("https://"))
            self.assertRegex(source["content_sha256"], HEX64)
            self.assertRegex(source["fetched_at"], TIME)
        self.assertTrue(all(isinstance(text, str) and text for text in self.document["not_public"]))

    def test_the_report_accounts_are_all_there(self):
        subjects = [(a["subject"], a["chain"]) for a in self.document["accounts"]]
        expected = ([(COLLECTOR, "near"), (MASTER, "near")] + [(w, "near") for w in WORKERS]
                    + [(t, "near") for t in TOKENS]
                    + [(f"rhea000{n}.multica.near", "near") for n in (453, 462, 505)]
                    + [("intents.near", "near"), ("zec.omft.near", "near"), (ETH_WALLET, "eth")])
        self.assertEqual(subjects, expected)
        self.assertEqual(self.document["code_hash_as_reported"]["value"], CODE_HASH)
        self.assertEqual(self.document["ref_pools_as_reported"]["ids"], list(range(8514, 8539)))

    def test_every_account_cites_assertions_in_the_store(self):
        for account in self.document["accounts"]:
            with self.subTest(subject=account["subject"]):
                self.assertTrue(account["assertion_ids"])
                for assertion_id in account["assertion_ids"]:
                    assertion = self.ids[assertion_id]
                    self.assertEqual((assertion.subject, assertion.chain), (account["subject"], account["chain"]))
                self.assertIn("third_party", {self.ids[i].provenance for i in account["assertion_ids"]})

    def test_every_onchain_assertion_and_fact_carries_a_block(self):
        cited = [self.ids[i] for a in self.document["accounts"] for i in a["assertion_ids"]]
        onchain = [a for a in cited if a.provenance == "onchain"]
        self.assertTrue(onchain)
        for assertion in onchain:
            with self.subTest(id=assertion.id):
                self.assertIsInstance(assertion.source["block"], int)
                self.assertGreater(assertion.source["block"], 0)
        for fact in self.document["onchain_facts"]:
            with self.subTest(fact=fact["summary"]):
                self.assertIsInstance(fact["block"], int)
                self.assertRegex(fact["result_sha256"], HEX64)

    def test_the_token_contracts_are_described_by_facts(self):
        for token in TOKENS:
            with self.subTest(token=token):
                current = [a for a in self.store.by_subject(token, "near") if a.superseded_by is None]
                facts = {a.label for a in current if a.provenance == "onchain"}
                self.assertIn("code hash equals the hash named in the Rhea report", facts)
                self.assertTrue(any(label.startswith("ft_metadata fails on the contract: ") for label in facts))
                self.assertFalse(any("no ft_metadata" in label for label in facts))
                [inferred] = [a for a in current if a.provenance == "inferred"]
                self.assertEqual((inferred.category, inferred.rule_id), ("denylist", "fake_token_shared_hash"))
                inputs = [self.ids[i] for i in inferred.source["inputs"]]
                self.assertEqual({a.provenance for a in inputs}, {"onchain"})
                self.assertEqual({a.subject for a in inputs}, {token})

    def test_no_barred_word_outside_a_quote_with_its_source(self):
        self.assertEqual(barred_words_outside_quotes(self.document), [])
        self.assertTrue(any(BARRED.search(a["quote"]["text"]) for a in self.document["accounts"]))

    def test_the_wording_rule_catches_authored_text(self):
        document = {"sources": [{"id": "s"}], "accounts": [{"quote": {"source": "s", "text": "a fake token"}}]}
        self.assertEqual(barred_words_outside_quotes(document), [])
        for bad in ({"sources": [{"id": "s"}], "title": "a scam"},
                    {"sources": [{"id": "s"}], "quote": {"source": "other", "text": "a drainer"}},
                    {"sources": [], "accounts": [{"role_as_reported": "clone"}]}):
            with self.subTest(bad=bad):
                self.assertTrue(barred_words_outside_quotes(bad))

    def test_the_rhea_label_rows_keep_barred_words_inside_the_quote(self):
        rows = [json.loads(line) for line in labels.STORE_PATH.read_text(encoding="utf-8").splitlines()]
        rhea = [r for r in rows if r["source"].get("url") == seed.REKT_RHEA_URL or r["subject"] in TOKENS
                or r["subject"] in WORKERS or r["subject"] in (COLLECTOR, MASTER)]
        self.assertTrue(rhea)
        for row in rhea:
            for field in ("entity", "label", "category", "note"):
                with self.subTest(id=row["id"], field=field):
                    outside = re.sub(r'^quote "(.*?)"(;.*)?$', lambda m: m.group(2) or "", row[field], flags=re.DOTALL)
                    self.assertIsNone(BARRED.search(outside))

    def test_rules_today_refuse_the_tokens_and_the_collector(self):
        verdicts = {entry["subject"]: entry for entry in self.document["rules_today"]}
        for subject in TOKENS + (COLLECTOR,):
            with self.subTest(subject=subject):
                entry = verdicts[subject]
                self.assertEqual(entry["verdict"], "DENY")
                checks = {c["id"]: c for c in entry["checks"]}
                self.assertEqual(checks["label_denylist"]["status"], "signal")
                for assertion_id in entry["labels"]:
                    self.assertIn(assertion_id, self.ids)
        self.assertEqual({verdicts[t]["as"] for t in TOKENS}, {"token"})
        exits = ("intents.near", "zec.omft.near")
        self.assertEqual(self.document["route_subjects"], list(exits))
        for subject in exits:
            with self.subTest(subject=subject):
                entry = verdicts[subject]
                self.assertEqual(entry["as"], "receiver")
                self.assertEqual({c["id"]: c["status"] for c in entry["checks"]}["label_denylist"], "pass")
        self.assertEqual({e["verdict"] for e in self.document["rules_today"] if e["subject"] not in exits},
                         {"DENY"})

    def test_rules_today_reproduce_in_replay(self):
        with mock.patch("urllib.request.urlopen", side_effect=no_network):
            rebuilt = seed.rhea_incident("replay")
        self.assertEqual([(e["subject"], e["verdict"], [(c["id"], c["status"]) for c in e["checks"]])
                          for e in rebuilt["rules_today"]],
                         [(e["subject"], e["verdict"], [(c["id"], c["status"]) for c in e["checks"]])
                          for e in self.document["rules_today"]])
        for key in ("accounts", "onchain_facts", "sources", "not_public", "code_hash_as_reported"):
            with self.subTest(key=key):
                self.assertEqual(rebuilt[key], self.document[key])


class IncidentPageTest(unittest.TestCase):
    def run_command(self, *argv):
        output, errors = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
            code = gate_main.main(["incident", *argv])
        return code, output.getvalue(), errors.getvalue()

    def test_the_rhea_page_renders_under_80_columns(self):
        code, output, _ = self.run_command("rhea")
        self.assertEqual(code, 0)
        lines = output.splitlines()
        self.assertEqual(lines[0], "Rhea Lend margin trading route exploit (2026-04-16)")
        self.assertTrue(all(len(line) < 80 for line in lines))
        document = json.loads(RHEA_PATH.read_text(encoding="utf-8"))
        for heading in ("Sources", "Accounts named in the report", "On chain facts", "Not public", "Standard"):
            self.assertIn(heading, lines)
        self.assertIn(f"    sha256 {document['sources'][0]['content_sha256']}", lines)
        self.assertIn(f"    fetched {document['sources'][0]['fetched_at']}", lines)
        for account in document["accounts"]:
            self.assertIn(f"  {account['subject']} ({account['chain']})", lines)
            for assertion_id in account["assertion_ids"]:
                self.assertIn(f"      {assertion_id}", lines)
        flat = " ".join(line.strip() for line in lines)
        for fact in document["onchain_facts"]:
            self.assertIn(f"{fact['method']} at block {fact['block']}:", flat)
        for entry in document["rules_today"]:
            self.assertIn(f"{entry['subject']} as {entry['as']}: {entry['verdict']}", flat)
        for item in document["not_public"]:
            self.assertIn(f"- {item}", flat)

    def test_the_route_accounts_have_their_own_heading(self):
        lines = self.run_command("rhea")[1].splitlines()
        start = lines.index("Protocol accounts the report names as the route (infrastructure and bridge")
        end = lines.index("Code hash as reported")
        listed = [line.split()[0] for line in lines[start:end] if line.startswith("  ") and line.endswith("(near)")]
        self.assertEqual(listed, ["intents.near", "zec.omft.near"])
        before = lines[:start]
        self.assertNotIn("  intents.near (near)", before)
        self.assertIn(f"  {COLLECTOR} (near)", before)

    def test_json_prints_the_file(self):
        code, output, _ = self.run_command("--json", "rhea")
        self.assertEqual(code, 0)
        self.assertEqual(output, RHEA_PATH.read_text(encoding="utf-8"))

    def test_an_unknown_or_unsafe_id_exits_2(self):
        for incident_id in ("nope", "../labels", "Rhea", "rhea.json"):
            with self.subTest(incident_id=incident_id):
                code, output, errors = self.run_command(incident_id)
                self.assertEqual(code, 2)
                self.assertEqual(output, "")
                self.assertIn("no incident", errors)

    def test_a_quote_from_an_unlisted_source_is_refused(self):
        document = json.loads(RHEA_PATH.read_text(encoding="utf-8"))
        document["accounts"][0]["quote"]["source"] = "elsewhere"
        with self.assertRaises(ValueError):
            incident.render(document)


if __name__ == "__main__":
    unittest.main()
