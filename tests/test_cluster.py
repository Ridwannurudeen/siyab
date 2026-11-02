import json
import re
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from gate import rpc
from intel import cluster, labels

ROOT = Path(__file__).resolve().parent.parent
INCIDENT = json.loads((ROOT / "intel" / "incidents" / "rhea.json").read_text(encoding="utf-8"))
SEEDS = [row["subject"] for row in INCIDENT["accounts"] if row["chain"] == "near"]


class FakeRPC:
    url = "https://free.rpc.fastnear.com"

    def __init__(self, keys=None, account_blocks=None, missing_keys=()):
        self.keys = keys or {}
        self.account_blocks = account_blocks or {}
        self.missing_keys = set(missing_keys)
        self.calls = []
        self.last_source = {"fetched_at": "2026-11-02T00:00:00Z"}
        self.last_block_height = None

    def call(self, method, params):
        self.calls.append((method, params))
        request = params["request_type"]
        account = params["account_id"]
        block = self.account_blocks.get(account, 100)
        self.last_block_height = block
        if request == "view_access_key_list":
            return {"keys": self.keys.get(account, []), "block_height": block}
        if request == "view_access_key":
            public_key = params["public_key"]
            if account in self.missing_keys:
                raise rpc.RpcError({"name": "HANDLER_ERROR", "cause": {"name": "UNKNOWN_ACCESS_KEY"}})
            found = next((key for key in self.keys.get(account, []) if key["public_key"] == public_key), None)
            if found is None:
                raise rpc.RpcError({"name": "HANDLER_ERROR", "cause": {"name": "UNKNOWN_ACCESS_KEY"}})
            return {"nonce": 1, "permission": found["access_key"]["permission"], "block_height": block}
        if request == "view_account":
            return {"code_hash": rpc.EMPTY_CODE_HASH, "block_height": block}
        raise AssertionError(request)

    def view_access_key_list(self, account_id):
        return self.call("query", {"request_type": "view_access_key_list", "finality": "final",
                                    "account_id": account_id})

    def view_account(self, account_id):
        return self.call("query", {"request_type": "view_account", "finality": "final",
                                   "account_id": account_id})


class FakeHTTP:
    url = "https://api.nearblocks.io"

    def __init__(self, responses=None):
        self.responses = responses or {}
        self.calls = []
        self.last_source = {"provider": "fixture.test", "request_sha256": "fixture", "fetched_at": "2026-11-02T00:00:00Z"}

    def get(self, path):
        self.calls.append(("GET", path))
        return self.responses.get(("GET", path), {})

    def post(self, path, body):
        self.calls.append(("POST", path, body))
        return self.responses.get(("POST", path), {"transfers": [], "resume_token": None})


class ClusterTest(unittest.TestCase):
    def test_a_seed_without_an_incident_label_is_refused_before_any_read(self):
        rpc_client = FakeRPC()
        fastnear = FakeHTTP()
        nearblocks = FakeHTTP()
        with self.assertRaises(cluster.Refused) as caught:
            cluster.build_cluster(["intents.near"], labels.load(), rpc_client, fastnear, nearblocks)
        self.assertEqual(str(caught.exception), "seed carries no incident label")
        self.assertEqual(rpc_client.calls, [])
        self.assertEqual(fastnear.calls, [])
        self.assertEqual(nearblocks.calls, [])

    def test_full_access_key_edges_are_confirmed_by_rpc_and_carry_both_blocks(self):
        seeds = ["alpha.near", "beta.near"]
        pk = "ed25519:public"
        key = {"public_key": pk, "access_key": {"permission": "FullAccess"}}
        store = SimpleNamespace(assertions=[SimpleNamespace(id=f"incident-{s}", subject=s, chain="near",
                                                            category="incident", superseded_by=None)
                                            for s in seeds])
        rpc_client = FakeRPC({s: [key] for s in seeds}, {seeds[0]: 21, seeds[1]: 22})
        fastnear = FakeHTTP({("GET", f"/v0/public_key/{pk}/all"): {"account_ids": seeds}})
        document = cluster.build_cluster(seeds, store, rpc_client, fastnear, FakeHTTP(), include_creation=False)
        edge = next(edge for edge in document["edges"] if edge["rule"] == "shares_full_access_key")
        self.assertEqual((edge["from"], edge["to"]), tuple(seeds))
        self.assertEqual(edge["provenance"], "onchain")
        self.assertEqual(edge["source"]["from"]["block"], 21)
        self.assertEqual(edge["source"]["to"]["block"], 22)
        self.assertEqual({(call[1]["account_id"], call[1]["public_key"]) for call in rpc_client.calls
                          if call[1]["request_type"] == "view_access_key"},
                         {("alpha.near", pk), ("beta.near", pk)})

    def test_unknown_access_key_from_one_account_skips_the_candidate_pair(self):
        seeds = ["alpha.near", "beta.near"]
        pk = "ed25519:public"
        key = {"public_key": pk, "access_key": {"permission": "FullAccess"}}
        store = SimpleNamespace(assertions=[SimpleNamespace(id=f"incident-{s}", subject=s, chain="near",
                                                            category="incident", superseded_by=None)
                                            for s in seeds])
        rpc_client = FakeRPC({s: [key] for s in seeds}, missing_keys={"beta.near"})
        fastnear = FakeHTTP({("GET", f"/v0/public_key/{pk}/all"): {"account_ids": seeds}})
        document = cluster.build_cluster(seeds, store, rpc_client, fastnear, FakeHTTP(), include_creation=False)
        self.assertFalse(document["edges"])

    def test_function_call_keys_never_make_an_edge(self):
        seeds = ["alpha.near", "beta.near"]
        pk = "ed25519:function"
        key = {"public_key": pk, "access_key": {"permission": {"FunctionCall": {
            "receiver_id": "app.near", "method_names": ["login"], "allowance": None}}}}
        store = SimpleNamespace(assertions=[SimpleNamespace(id=f"incident-{s}", subject=s, chain="near",
                                                            category="incident", superseded_by=None)
                                            for s in seeds])
        rpc_client = FakeRPC({s: [key] for s in seeds})
        fastnear = FakeHTTP({("GET", f"/v0/public_key/{pk}/all"): {"account_ids": seeds}})
        document = cluster.build_cluster(seeds, store, rpc_client, fastnear, FakeHTTP(), include_creation=False)
        self.assertFalse(document["edges"])
        self.assertFalse(any(call[1]["request_type"] == "view_access_key" for call in rpc_client.calls))

    def test_sub_account_edges_skip_top_level_registrars(self):
        seeds = ["child.parent.near", "child.near"]
        store = SimpleNamespace(assertions=[SimpleNamespace(id=f"incident-{s}", subject=s, chain="near",
                                                            category="incident", superseded_by=None)
                                            for s in seeds])
        rpc_client = FakeRPC(account_blocks={"child.parent.near": 31, "parent.near": 32, "child.near": 33})
        document = cluster.build_cluster(seeds, store, rpc_client, FakeHTTP(), FakeHTTP(), include_creation=False)
        edge = next(edge for edge in document["edges"] if edge["rule"] == "sub_account_of")
        self.assertEqual((edge["from"], edge["to"]), ("child.parent.near", "parent.near"))
        self.assertEqual(edge["source"]["from"]["block"], 31)
        self.assertEqual(edge["source"]["to"]["block"], 32)
        self.assertFalse(any(edge["to"] in {"near", "tg", "testnet"} for edge in document["edges"]))

    def test_excluded_funders_become_facts_not_edges(self):
        seeds = ["alpha.near", "beta.near"]
        store = SimpleNamespace(assertions=[SimpleNamespace(id=f"incident-{s}", subject=s, chain="near",
                                                            category="incident", superseded_by=None)
                                            for s in seeds])
        nearblocks = FakeHTTP({
            ("GET", f"/v1/account/{s}/keys"): {"keys": [{"created": {"transaction_hash": f"tx-{s}",
                                                                       "block_timestamp": "100"}}]}
            for s in seeds
        })
        nearblocks.responses.update({("GET", f"/v1/txns/tx-{s}"): {"txns": [{"signer_account_id": "intents.near",
                                                                               "block": {"block_height": 10}}]}
                                     for s in seeds})
        document = cluster.build_cluster(seeds, store, FakeRPC(), FakeHTTP(), nearblocks)
        self.assertEqual(document["edges"], [])
        facts = [fact for fact in document["facts"] if fact["kind"] == "created_by_funder"]
        self.assertEqual(len(facts), 2)
        self.assertTrue(all(fact["excluded"] for fact in facts))

    def test_funder_edges_are_inferred_with_inputs_and_confidence(self):
        seeds = ["alpha.near", "beta.near"]
        store = SimpleNamespace(assertions=[SimpleNamespace(id=f"incident-{s}", subject=s, chain="near",
                                                            category="incident", superseded_by=None)
                                            for s in seeds])
        nearblocks = FakeHTTP({
            ("GET", f"/v1/account/{s}/keys"): {"keys": [{"created": {"transaction_hash": f"tx-{s}",
                                                                       "block_timestamp": "100"}}]}
            for s in seeds
        })
        nearblocks.responses.update({("GET", f"/v1/txns/tx-{s}"): {"txns": [{"signer_account_id": "factory.near",
                                                                               "block": {"block_height": 10}}]}
                                     for s in seeds})
        document = cluster.build_cluster(seeds, store, FakeRPC(), FakeHTTP(), nearblocks)
        edge = next(edge for edge in document["edges"] if edge["rule"] == "created_by_funder")
        self.assertEqual(edge["provenance"], "inferred")
        self.assertEqual(edge["confidence"], 0.5)
        self.assertEqual(len(edge["inputs"]), 2)

    def test_the_oldest_key_creation_transaction_supplies_the_funder(self):
        client = FakeHTTP({
            ("GET", "/v1/account/alpha.near/keys"): {"keys": [
                {"created": {"transaction_hash": "newer", "block_timestamp": "200"}},
                {"created": {"transaction_hash": "older", "block_timestamp": "100"}},
            ]},
            ("GET", "/v1/txns/older"): {"txns": [{"signer_account_id": "first-funder.near",
                                                    "block": {"block_height": 10}}]},
        })
        facts, _ = cluster._creation_facts(["alpha.near"], client)
        self.assertEqual(facts[0]["value"], "first-funder.near")

    def test_depositors_are_counted_never_listed(self):
        transfers = FakeHTTP({("POST", "/v0/transfers"): {"transfers": [
            {"other_account_id": "a.near", "block_timestamp": "1776297600000000000"},
            {"other_account_id": "a.near", "block_timestamp": "1776297600000000000"},
            {"other_account_id": "b" * 64, "block_timestamp": "1776297600000000000"},
        ], "resume_token": None}})
        result = cluster.deposit_pattern("collector.near", transfers, "2026-04-16")
        self.assertEqual(result["count"], 2)
        self.assertEqual(result["implicit_count"], 1)
        self.assertNotIn("depositors", result)
        self.assertNotIn("a.near", json.dumps(result))

    def test_the_committed_rhea_cluster_rebuilds_from_the_fixtures(self):
        expected = json.loads((ROOT / "intel" / "clusters" / "rhea.json").read_text(encoding="utf-8"))
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch("urllib.request.urlopen", side_effect=AssertionError("network opened")):
                actual = cluster.rebuild("rhea", record=False, output_dir=Path(tmp), out=lambda _line: None)
            self.assertEqual(actual, expected)

    def test_the_subject_form_writes_no_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture_dir = Path(tmp) / "fixtures"
            output_dir = Path(tmp) / "clusters"
            created = []

            def fake_client(url, mode, *, kind="rpc", fixtures_dir):
                created.append((mode, Path(fixtures_dir)))
                if kind == "rpc":
                    return FakeRPC()
                return FakeHTTP()

            with mock.patch.object(cluster, "FIXTURES_DIR", fixture_dir), \
                    mock.patch.object(rpc, "Client", side_effect=fake_client):
                code = cluster.main(["--subject", "caller.near", "--record"], output_dir=output_dir,
                                    out=lambda _line: None)
            self.assertEqual(code, 0)
            self.assertEqual(created, [("off", fixture_dir)] * 4)
            self.assertEqual(list(fixture_dir.iterdir()) if fixture_dir.exists() else [], [])
            self.assertEqual(list(output_dir.iterdir()) if output_dir.exists() else [], [])

    def test_no_authored_word_claims_ownership(self):
        forbidden = ("own" + "er", "own" + "s", "control" + "s", "same " + "person", "attack" + "er")
        paths = [ROOT / "intel" / "cluster.py", ROOT / "docs" / "CLUSTER.md",
                 ROOT / "intel" / "clusters" / "rhea.json", ROOT / "tests" / "test_cluster.py"]
        text = "\n".join(path.read_text(encoding="utf-8") for path in paths if path.exists()).lower()
        pattern = re.compile(r"\b(" + "|".join(re.escape(word) for word in forbidden) + r")\b")
        self.assertIsNone(pattern.search(text))

    def test_precision_lines_in_the_cluster_document_appear_verbatim_in_docs(self):
        document = json.loads((ROOT / "intel" / "clusters" / "rhea.json").read_text(encoding="utf-8"))
        docs = (ROOT / "docs" / "CLUSTER.md").read_text(encoding="utf-8")
        for line in document["precision"].values():
            self.assertIn(line, docs)


if __name__ == "__main__":
    unittest.main()
