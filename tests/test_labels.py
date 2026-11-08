import contextlib
import hashlib
import importlib.util
import io
import json
import re
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from gate import rpc, rules
from intel import __main__ as intel_main
from intel import issuers, labels, registries, seed

RAW_DIR = seed.RAW_DIR
ROOT = Path(__file__).resolve().parent.parent
DOCUMENT = {"url": "https://example.org/page", "content_sha256": "a" * 64, "fetched_at": "2026-10-29T09:00:00Z"}
ONCHAIN = {"rpc": "https://free.rpc.fastnear.com", "method": "view_account",
           "args": {"account_id": "x.near"}, "block": 10}
INFERRED = {"rule_id": "fake_token_shared_hash", "inputs": ["b" * 64]}
SOURCES = {"official_source": DOCUMENT, "third_party": DOCUMENT, "onchain": ONCHAIN,
           "inferred": INFERRED, "unknown": DOCUMENT}


def raw(fields):
    """A row dict built without validation, with its id computed."""
    data = {"id": None, "superseded_by": None, **fields}
    data["id"] = labels.assertion_id(data)
    return data


def base_fields(**changes):
    fields = {"entity": "Example", "subject": "x.near", "chain": "near", "label": "example",
              "category": "treasury", "provenance": "official_source", "source": DOCUMENT,
              "observed_at": "2026-10-29T09:00:00Z", "confidence": 1, "rule_id": None, "note": "n"}
    fields.update(changes)
    return fields


def no_network(*args, **kwargs):
    raise AssertionError("the test opened the network")


class CommittedStoreTest(unittest.TestCase):
    def setUp(self):
        self.store = labels.load()
        self.rows = [json.loads(line) for line in labels.STORE_PATH.read_text(encoding="utf-8").splitlines()]

    def test_every_row_validates_and_names_a_source(self):
        self.assertTrue(self.store.assertions)
        for item in self.rows:
            with self.subTest(id=item["id"]):
                self.assertIsNotNone(item["source"])
                labels.validate(item)

    def test_counts_match_a_recount(self):
        counts = self.store.counts()
        self.assertEqual(counts["assertions"], len(self.rows))
        self.assertEqual(counts["subjects"], len({(r["chain"], r["subject"]) for r in self.rows}))
        for provenance in labels.PROVENANCES:
            self.assertEqual(counts["provenance"][provenance],
                             sum(r["provenance"] == provenance for r in self.rows))
        for category in labels.CATEGORIES:
            self.assertEqual(counts["category"][category], sum(r["category"] == category for r in self.rows))

    def test_counts_command_prints_the_line(self):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            self.assertEqual(intel_main.main(["counts"]), 0)
        counts = self.store.counts()
        p = counts["provenance"]
        self.assertEqual(output.getvalue(),
                         f"{counts['assertions']} assertions over {counts['subjects']} subjects: "
                         f"onchain {p['onchain']}, official_source {p['official_source']}, "
                         f"third_party {p['third_party']}, inferred {p['inferred']}, unknown {p['unknown']}\n")

    def test_snapshot_sha256_equals_the_rows_content_sha256(self):
        snapshots = {hashlib.sha256(path.read_bytes()).hexdigest() for path in RAW_DIR.iterdir()}
        for item in self.rows:
            if "content_sha256" in item["source"]:
                with self.subTest(id=item["id"]):
                    self.assertIn(item["source"]["content_sha256"], snapshots)

    def test_catalog_count_is_the_snapshot_count(self):
        [snapshot] = RAW_DIR.glob("1click-tokens-*.json")
        assets = json.loads(snapshot.read_bytes())
        catalog = [a for a in self.store.assertions if a.category == "catalog_token" and a.provenance == "official_source"]
        self.assertEqual(len(catalog), len(assets))
        bridged = {a["assetId"].rsplit(":", 1)[-1] for a in assets if a["assetId"].endswith(".omft.near")}
        catalog_bridges = [a for a in self.store.assertions if a.category == "bridge" and a.provenance == "official_source"]
        self.assertEqual(len(catalog_bridges), len(bridged))

    def test_reference_accounts_have_no_current_refusal_label(self):
        references = {(assertion.subject, assertion.chain) for assertion in self.store.assertions
                      if assertion.category in ("catalog_token", "infrastructure", "bridge", "treasury")
                      and assertion.superseded_by is None}
        refusals = {(assertion.subject, assertion.chain) for assertion in self.store.assertions
                    if assertion.category in ("denylist", "incident") and assertion.superseded_by is None}
        self.assertEqual(references & refusals, set())

    def test_readme_lists_every_snapshot(self):
        readme = seed.README_PATH.read_text(encoding="utf-8")
        self.assertEqual(readme, seed.readme_text())
        self.assertNotIn("`raw/NOTICE.md`", readme)
        for path in RAW_DIR.iterdir():
            if path.name == "NOTICE.md":
                continue
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            with self.subTest(snapshot=path.name):
                self.assertIn(f"`raw/{path.name}`", readme)
                self.assertIn(f"{digest[:4]}...{digest[-4:]}", readme)

    def test_seeds_rebuild_the_committed_rows_offline(self):
        with mock.patch("urllib.request.urlopen", side_effect=no_network):
            by_registry = registries.rows()
            rows = (seed.treasuries("replay") + seed.catalog("replay") + seed.infrastructure("replay")
                    + seed.rhea("replay") + seed.sweat_hot("replay")
                    + [row for name in registries.REGISTRY_ORDER for row in by_registry[name]]
                    + issuers.history_rows() + issuers.rows() + issuers.blacklist_rows())
        manual_rows = [r for r in self.rows if r["entity"] == "issuer blacklist correction"]
        replaced_rows = [r for r in self.rows if r["entity"] == "issuer of usdt.tether-token.near"
                         and r["subject"] == "usdt.tether-token.near" and r["category"] == "denylist"]
        self.assertEqual(len(manual_rows), 1)
        self.assertEqual(len(replaced_rows), 1)
        generated_rows = [r for r in self.rows if r not in manual_rows and r not in replaced_rows]
        self.assertEqual([r["id"] for r in rows], [r["id"] for r in generated_rows])
        self.assertEqual([labels.row_line(r) for r in rows], [labels.row_line(r) for r in generated_rows])

    def test_the_rules_read_the_store(self):
        client = rpc.Client(rpc.MAINNET_RPC_URL, "replay")
        nearblocks = rpc.Client(rpc.NEARBLOCKS_URL, "replay", kind="http")
        request = {"kind": "ft", "token_id": "wrap.near", "receiver_id": "intents.near", "amount": "1",
                   "memo": None}
        with mock.patch("urllib.request.urlopen", side_effect=no_network):
            judgement = rules.judge(request, {"allowlist": ["intents.near"], "caps": []}, self.store, client,
                                    nearblocks, now_ms=1793275200000, injection_scanner=None)
        details = {c["id"]: c["detail"] for c in judgement.checks}
        self.assertEqual(details["token_in_catalog"], "wrap.near is a catalog_token in the label store")
        self.assertEqual(judgement.verdict, "ALLOW")
        self.assertTrue(set(judgement.labels) <= {a.id for a in self.store.assertions})
        self.assertIn("intents.near", [a.subject for a in self.store.assertions if a.id in judgement.labels])
        self.assertEqual(judgement.checks[1]["source"]["request_sha256"], self.store.sha256)

    def test_authored_text_never_holds_the_barred_word(self):
        # Source strings sit inside double quotes as data; everything outside them is our own text.
        for item in self.rows:
            for field in ("entity", "label", "category", "provenance", "note"):
                with self.subTest(id=item["id"], field=field):
                    self.assertNotIn("safe", re.sub(r'"[^"]*"', "", item[field]).lower())
        quoted = [item for item in self.rows if 'symbol "SAFE"' in item["label"]]
        assets = [a for a in json.loads(seed.snapshot("catalog", "replay")[0]) if a["symbol"] == "SAFE"]
        bridged = {a["assetId"].rsplit(":", 1)[-1] for a in assets if a["assetId"].endswith(".omft.near")}
        self.assertTrue(assets)
        self.assertEqual(len(quoted), len(assets) + len(bridged))

    def test_the_hygiene_rule_covers_authored_text_only(self):
        spec = importlib.util.spec_from_file_location("check_hygiene", ROOT / "scripts" / "check_hygiene.py")
        hygiene = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(hygiene)
        line = "detail = 'the account is safe'"
        for path in ("gate/rules.py", "intel/seed.py", "docs/VERDICT-RECORD.md"):
            with self.subTest(path=path):
                self.assertTrue(hygiene.check_text(path, line))
        for path in ("intel/labels.jsonl", "intel/seeds/raw/x.json", "fixtures/rpc/x.json", "tests/test_x.py"):
            with self.subTest(path=path):
                self.assertFalse(hygiene.check_text(path, line))


class ValidationTest(unittest.TestCase):
    def test_every_category_and_provenance(self):
        for category in labels.CATEGORIES:
            with self.subTest(category=category):
                seed.make_row(**base_fields(category=category))
        for provenance, source in SOURCES.items():
            with self.subTest(provenance=provenance):
                rule = "fake_token_shared_hash" if provenance == "inferred" else None
                seed.make_row(**base_fields(provenance=provenance, source=source, rule_id=rule))
        for field, value in (("category", "scam"), ("provenance", "rumour"), ("category", "Treasury")):
            with self.subTest(field=field, value=value), self.assertRaises(labels.LabelError):
                labels.validate(raw(base_fields(**{field: value})))

    def test_no_row_without_a_source(self):
        for source in (None, {}, {"url": "https://example.org"}, "https://example.org",
                       dict(DOCUMENT, url="http://example.org"), dict(DOCUMENT, content_sha256="A" * 64),
                       dict(DOCUMENT, fetched_at="29 Oct 2026")):
            with self.subTest(source=source), self.assertRaises(labels.LabelError):
                labels.validate(raw(base_fields(source=source)))
        fields = base_fields()
        del fields["source"]
        with self.assertRaises(labels.LabelError):
            labels.validate(raw(fields))

    def test_source_shape_follows_provenance(self):
        for provenance, source in (("official_source", ONCHAIN), ("onchain", DOCUMENT),
                                   ("third_party", INFERRED)):
            with self.subTest(provenance=provenance), self.assertRaises(labels.LabelError):
                labels.validate(raw(base_fields(provenance=provenance, source=source)))

    def test_onchain_row_needs_a_block(self):
        for block in (None, 0, True, "10"):
            with self.subTest(block=block), self.assertRaises(labels.LabelError):
                labels.validate(raw(base_fields(provenance="onchain", source=dict(ONCHAIN, block=block))))
        source = {key: value for key, value in ONCHAIN.items() if key != "block"}
        with self.assertRaises(labels.LabelError):
            labels.validate(raw(base_fields(provenance="onchain", source=source)))

    def test_rule_id_only_on_inferred_rows(self):
        with self.assertRaises(labels.LabelError):
            labels.validate(raw(base_fields(rule_id="r")))
        with self.assertRaises(labels.LabelError):
            labels.validate(raw(base_fields(provenance="inferred", source=INFERRED, rule_id="other")))

    def test_other_field_rules(self):
        for field, value in (("confidence", 1.5), ("confidence", True), ("chain", "Near"), ("chain", ""),
                             ("subject", ""), ("observed_at", "2026-10-29"), ("note", None)):
            with self.subTest(field=field, value=value), self.assertRaises(labels.LabelError):
                labels.validate(raw(base_fields(**{field: value})))

    def test_id_rule_and_supersession(self):
        item = seed.make_row(**base_fields())
        body = {k: v for k, v in item.items() if k not in ("id", "superseded_by")}
        text = json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        self.assertEqual(item["id"], hashlib.sha256(text.encode()).hexdigest())
        self.assertEqual(labels.validate(dict(item, superseded_by="c" * 64)).id, item["id"])
        with self.assertRaises(labels.LabelError):
            labels.validate(dict(item, label="changed"))
        self.assertNotEqual(seed.make_row(**base_fields(label="changed"))["id"], item["id"])


class StoreFileTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = Path(tmp.name) / "labels.jsonl"
        self.first = seed.make_row(**base_fields())
        self.second = seed.make_row(**base_fields(subject="y.near"))

    def write(self, *rows):
        self.path.write_text("".join(labels.row_line(r) + "\n" for r in rows), encoding="utf-8")

    def test_duplicate_id_is_rejected(self):
        self.write(self.first, self.first)
        with self.assertRaises(labels.LabelError):
            labels.load(self.path)

    def test_superseded_by_must_name_a_row(self):
        self.write(dict(self.first, superseded_by=self.second["id"]))
        with self.assertRaises(labels.LabelError):
            labels.load(self.path)
        self.write(dict(self.first, superseded_by=self.second["id"]), self.second)
        store = labels.load(self.path)
        self.assertEqual(store.by_subject("x.near", "near")[0].superseded_by, self.second["id"])
        self.assertEqual(store.sha256, hashlib.sha256(self.path.read_bytes()).hexdigest())

    def test_append_adds_only_new_ids(self):
        self.assertEqual(seed.append([self.first], self.path), 1)
        self.assertEqual(seed.append([self.first, self.second], self.path), 1)
        self.assertEqual(seed.append([self.first, self.second], self.path), 0)
        self.assertEqual(len(labels.load(self.path).assertions), 2)


class CategoryAndRailTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = Path(tmp.name) / "labels.jsonl"
        self.input = seed.make_row(**base_fields(provenance="onchain", source=ONCHAIN, category="incident"))

    def write(self, *rows):
        self.path.write_text("".join(labels.row_line(r) + "\n" for r in rows), encoding="utf-8")

    def inferred(self, category="denylist"):
        source = {"rule_id": "fake_token_shared_hash", "inputs": [self.input["id"]]}
        return seed.make_row(**base_fields(category=category, provenance="inferred", source=source,
                                           rule_id="fake_token_shared_hash", confidence=0.9))

    def incident(self, provenance, **changes):
        return seed.make_row(**base_fields(provenance=provenance, source=SOURCES[provenance], category="incident",
                                           label=provenance, **changes))

    def test_organisation_is_a_category_outside_the_reference_set(self):
        self.assertIn("organisation", labels.CATEGORIES)
        self.assertNotIn("organisation", rules.REFERENCE_CATEGORIES)
        self.assertNotIn("organisation", rules.DENY_CATEGORIES)
        self.write(seed.make_row(**base_fields(category="organisation")))
        self.assertEqual(labels.load(self.path).counts()["category"]["organisation"], 1)

    def test_an_inferred_deny_row_needs_a_cited_incident_row_on_the_subject(self):
        watch = seed.make_row(**base_fields(provenance="onchain", source=ONCHAIN, category="watch"))
        for category in ("denylist", "incident"):
            row = self.inferred(category)
            with self.subTest(category=category):
                self.write(row)
                with self.assertRaises(labels.LabelError) as caught:
                    labels.load(self.path)
                self.assertIn(f"{row['id']}: an inferred row may not add {category} to a subject no cited source "
                              "or chain read names", str(caught.exception))
                for provenance in ("third_party", "onchain"):
                    self.write(self.incident(provenance), row)
                    self.assertEqual(len(labels.load(self.path).assertions), 2)
            refused = {
                "official source": [self.incident("official_source"), row],
                "another category": [watch, row],
                "another subject": [self.incident("third_party", subject="y.near"), row],
                "another chain": [self.incident("third_party", chain="eth"), row],
                "superseded": [dict(self.incident("third_party"), superseded_by=watch["id"]), watch, row],
            }
            for case, rows in refused.items():
                with self.subTest(category=category, case=case):
                    self.write(*rows)
                    with self.assertRaises(labels.LabelError):
                        labels.load(self.path)
            with self.subTest(category=category, case="superseded inferred row"):
                self.write(dict(row, superseded_by=watch["id"]), watch)
                self.assertEqual(len(labels.load(self.path).assertions), 2)
        self.write(self.inferred("watch"))
        self.assertEqual(len(labels.load(self.path).assertions), 1)

    def test_the_committed_store_passes_the_rail(self):
        store = labels.load()
        inferred = [a for a in store.assertions if a.provenance == "inferred" and a.category in rules.DENY_CATEGORIES]
        self.assertEqual(len(inferred), 4)
        for assertion in inferred:
            with self.subTest(subject=assertion.subject):
                self.assertTrue(any(a.category == "incident" and a.provenance in ("third_party", "onchain")
                                    and a.superseded_by is None
                                    for a in store.by_subject(assertion.subject, assertion.chain)))


class SeedTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.raw_dir = Path(tmp.name) / "raw"
        self.treasury = next(RAW_DIR.glob("near-intents-treasury-addresses-*.md")).read_bytes()

    def test_record_stores_the_body_byte_for_byte_and_names_it_by_time(self):
        reply = mock.MagicMock()
        reply.__enter__.return_value.read.return_value = self.treasury
        with mock.patch("urllib.request.urlopen", return_value=reply) as opened:
            data, fetched_at, path = seed.snapshot("treasuries", "record", self.raw_dir,
                                                   clock=lambda: 1793265603)
        self.assertEqual(opened.call_args.args[0].full_url, seed.TREASURY_URL)
        self.assertEqual(opened.call_args.kwargs["timeout"], 30)
        self.assertEqual(path.name, "near-intents-treasury-addresses-20261029T092003Z.md")
        self.assertEqual((path.read_bytes(), data, fetched_at), (self.treasury, self.treasury,
                                                                 "2026-10-29T09:20:03Z"))
        with mock.patch("urllib.request.urlopen", side_effect=no_network):
            self.assertEqual(seed.snapshot("treasuries", "replay", self.raw_dir)[1], fetched_at)

    def test_replay_without_a_snapshot_and_off_mode_fail(self):
        with self.assertRaises(seed.SeedError):
            seed.snapshot("catalog", "replay", self.raw_dir)
        with self.assertRaises(seed.SeedError):
            seed.snapshot("catalog", "off", self.raw_dir)

    def test_treasury_rows_follow_the_page(self):
        rows = seed.treasuries("replay")
        evm = [r for r in rows if r["entity"] == "NEAR Intents" and r["subject"].startswith("0x2CfF")]
        self.assertEqual(len(evm), 12)
        self.assertEqual({r["chain"] for r in evm}, {"arb", "avax", "adi", "aurora", "base", "bera", "bsc",
                                                     "eth", "gnosis", "op", "plasma", "pol"})
        self.assertIn(("intents.near", "near"), {(r["subject"], r["chain"]) for r in rows})
        self.assertEqual({r["chain"] for r in rows if r["label"] == "refill address"}, {"evm", "sol", "ton"})
        self.assertTrue(all(r["category"] == "treasury" and r["provenance"] == "official_source" for r in rows))

    def test_a_page_without_an_expected_row_fails(self):
        self.raw_dir.mkdir()
        page = self.treasury.decode().replace("| **TON** |", "| **Tonne** |").encode()
        (self.raw_dir / "near-intents-treasury-addresses-20261029T092003Z.md").write_bytes(page)
        with self.assertRaises(seed.SeedError):
            seed.treasuries("replay", self.raw_dir)

    def test_documented_accounts_need_view_account(self):
        client = mock.MagicMock()
        client.url = "https://free.rpc.fastnear.com"
        client.last_source = {"fetched_at": "2026-10-29T09:21:00Z"}
        client.last_block_height = 5
        client.call_function.return_value = {"version": "1", "link": "https://example.org", "standards": []}
        missing = seed.rpc.RpcError({"name": "HANDLER_ERROR", "cause": {"name": "UNKNOWN_ACCOUNT"}})
        client.view_account.side_effect = missing
        rows = seed.infrastructure("replay", client=client)
        self.assertFalse([r for r in rows if r["entity"] in ("OutLayer", "NEAR MPC")])
        client.view_account.side_effect = seed.rpc.RpcError({"name": "HANDLER_ERROR", "cause": {"name": "TIMEOUT"}})
        with self.assertRaises(seed.rpc.RpcError):
            seed.infrastructure("replay", client=client)


if __name__ == "__main__":
    unittest.main()
