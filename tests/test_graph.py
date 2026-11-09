import copy
import json
import unittest
from collections import Counter
from decimal import Decimal
from pathlib import Path

from intel import graph, trace

ROOT = Path(__file__).resolve().parent.parent


def hop(sender, receiver, asset="native:near", usd="1.25", timestamp=1776237164627685607):
    return {"from": sender, "to": receiver, "asset_id": asset, "amount": "9007199254740993123456789",
            "human_amount": "9.007199254740993123456789", "usd_amount": usd,
            "transaction_hash": "tx", "block": 194076376, "block_timestamp": timestamp,
            "method_name": None, "receipt_id": "receipt"}


def flow(hops):
    return {"subject": "root.near", "hops": hops, "assertion_ids": ["assertion"],
            "depth": 3, "depth_reached": trace.reached_depth({"subject": "root.near", "hops": hops}),
            "window": {"from": "2026-04-15T00:00:00Z", "from_timestamp_ms": 1776211200000},
            "filters": {"direction": "sender", "limit": 100}, "terminal_accounts": [],
            "truncated": True, "completeness": {"account": "root.near", "depth": 0, "reason": "page budget"},
            "shares": trace.shares("root.near", hops, set(), set()), "state_at_block": [],
            "fetched_at": "2026-11-03T08:17:22Z"}


class FlowPayloadTest(unittest.TestCase):
    def test_grouping_keeps_assets_directions_and_every_record_separate(self):
        rows = [hop("root.near", "a.near"), hop("root.near", "a.near", usd=None),
                hop("root.near", "a.near", asset="nep141:token.near"),
                hop("a.near", "root.near"), hop("a.near", "b.near")]
        result = graph.flow_payload(flow(rows), {"root.near": "accounts/root.near.html"})
        self.assertEqual((result["total_nodes"], result["total_edges"], result["total_records"]), (3, 4, 5))
        self.assertEqual(result["edges"][0]["record_ids"], ["record-0", "record-1"])
        records = {record["id"]: record for record in result["records"]}
        ids = []
        for edge in result["edges"]:
            for record_id in edge["record_ids"]:
                record = records[record_id]
                self.assertEqual(tuple(edge[key] for key in ("from", "to", "asset_id")),
                                 tuple(record[key] for key in ("from", "to", "asset_id")))
                ids.append(record_id)
        self.assertEqual(Counter(ids), Counter(records.keys()))
        self.assertEqual(result["nodes"], [
            {"id": "root.near", "label": "root.near", "url": "../accounts/root.near.html", "depth": 0},
            {"id": "a.near", "label": "a.near", "url": None, "depth": 1},
            {"id": "b.near", "label": "b.near", "url": None, "depth": 2}])

    def test_precision_and_unknown_values_survive_json_round_trip(self):
        rows = [hop("root.near", "a.near", usd="1.000000000000000000000001"),
                hop("root.near", "a.near", usd=None, timestamp=1776237164627685608)]
        rows[1]["human_amount"] = None
        result = json.loads(json.dumps(graph.flow_payload(flow(rows), {}), allow_nan=False))
        for original, record in zip(rows, result["records"]):
            for key in original.keys() - {"block_timestamp"}:
                self.assertEqual(record[key], original[key])
            self.assertEqual(record["block_timestamp"], str(original["block_timestamp"]))
            self.assertEqual(record["timestamp_ms"], 1776237164627)
            self.assertIsNone(record["confidence"])
            self.assertEqual(record["provenance"], "onchain")
        self.assertNotEqual(result["records"][0]["block_timestamp"], result["records"][1]["block_timestamp"])

    def test_numeric_api_values_become_strings_without_a_new_valuation(self):
        row = hop("root.near", "a.near", usd=99900.0)
        row["human_amount"] = 74000.0
        result = graph.flow_payload(flow([row]), {})["records"][0]
        self.assertEqual(result["usd_amount"], "99900.0")
        self.assertEqual(result["human_amount"], "74000.0")

    def test_unsafe_milliseconds_are_unavailable_but_nanoseconds_are_retained(self):
        timestamp = (2 ** 53) * 1000000 + 1
        record = graph.flow_payload(flow([hop("root.near", "a.near", timestamp=timestamp)]), {})["records"][0]
        self.assertEqual(record["block_timestamp"], str(timestamp))
        self.assertIsNone(record["timestamp_ms"])

    def test_empty_flow_still_includes_the_root_without_edges(self):
        result = graph.flow_payload(flow([]), {})
        self.assertEqual(result["total_nodes"], 1)
        self.assertEqual(result["total_records"], 0)
        self.assertEqual(result["edges"], [])

    def test_direct_shares_do_not_include_deeper_or_returning_value(self):
        document = flow([hop("root.near", "a.near", usd="10"),
                         hop("root.near", "a.near", usd="0.2"),
                         hop("root.near", "a.near", usd=None),
                         hop("a.near", "root.near", usd="9000")])
        result = graph.flow_payload(document, {})
        self.assertEqual(result["shares"]["direct_outbound_usd"], "10")
        self.assertEqual(result["shares"]["rows"], 1)
        self.assertEqual(result["shares"]["below_min_usd"], {"count": 1, "usd": "0.2"})
        self.assertEqual(result["shares"]["without_usd"]["count"], 1)
        self.assertEqual([record["depth"] for record in result["records"]], [1, 1, 1, 2])
        self.assertEqual(result["coverage"]["completeness"], document["completeness"])


class RecordedPayloadTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.flow = json.loads((ROOT / "intel" / "flows" / "rhea-collector.json").read_text(encoding="utf-8"))
        cls.cluster = json.loads((ROOT / "intel" / "clusters" / "rhea.json").read_text(encoding="utf-8"))

    def test_recorded_flow_counts_coverage_and_usd_reconcile(self):
        result = graph.flow_payload(self.flow, {})
        self.assertEqual((result["total_nodes"], result["total_records"], result["total_edges"]), (479, 1708, 978))
        self.assertEqual(len({(edge["from"], edge["to"]) for edge in result["edges"]}), 754)
        self.assertEqual(sum(len(edge["record_ids"]) for edge in result["edges"]), 1708)
        self.assertEqual(sum(record["usd_amount"] is None for record in result["records"]), 66)
        self.assertEqual((result["bounds"]["requested_depth"], result["bounds"]["reached_depth"]), (3, 2))
        self.assertTrue(result["coverage"]["truncated"])
        self.assertEqual(result["coverage"]["completeness"]["reason"], "the page budget of 150 requests")
        direct = [record for record in result["records"] if record["from"] == result["root"]
                  and record["usd_amount"] is not None and Decimal(record["usd_amount"]) >= 1]
        self.assertEqual(len(direct), 525)
        self.assertEqual(sum((Decimal(record["usd_amount"]) for record in direct), Decimal(0)),
                         Decimal(result["shares"]["direct_outbound_usd"]))
        self.assertEqual(result["shares"]["below_min_usd"]["count"], 422)
        self.assertEqual(result["source"]["fetched_at"], self.flow["fetched_at"])

    def test_cluster_preserves_inference_inputs_and_includes_parent_endpoint(self):
        result = graph.cluster_payload(self.cluster, {})
        self.assertEqual((result["total_nodes"], result["total_edges"], result["total_records"]), (15, 48, 48))
        self.assertEqual(len(result["bounds"]["seeds"]), 14)
        self.assertIn("multica.near", {node["id"] for node in result["nodes"]})
        self.assertTrue(all(node["depth"] is None for node in result["nodes"]))
        self.assertEqual(Counter(edge["provenance"] for edge in result["edges"]), {"onchain": 3, "inferred": 45})
        facts = {fact["id"]: fact for fact in result["facts"]}
        for original, record, edge in zip(self.cluster["edges"], result["records"], result["edges"]):
            for key in original:
                self.assertEqual(record[key], original[key])
            self.assertEqual(edge["record_ids"], [record["id"]])
            self.assertIsNone(edge["asset_id"])
            self.assertIsNone(record["timestamp_ms"])
            if edge["provenance"] == "inferred":
                self.assertEqual(edge["confidence"], 0.5)
                self.assertEqual(len(record["inputs"]), 2)
                self.assertTrue(all(fact_id in facts for fact_id in record["inputs"]))
        self.assertEqual(result["facts"], self.cluster["facts"])
        self.assertEqual(result["coverage"]["depositors"], self.cluster["depositors"])
        self.assertEqual(result["coverage"]["depositors"]["count"], 140)

    def test_distinct_relationship_rules_on_same_pair_are_not_merged(self):
        document = copy.deepcopy(self.cluster)
        extra = copy.deepcopy(document["edges"][0])
        extra.update(rule="created_by_funder", provenance="inferred", confidence=0.5)
        document["edges"].append(extra)
        result = graph.cluster_payload(document, {})
        self.assertEqual(result["total_edges"], 49)
        self.assertEqual(result["edges"][-1]["provenance"], "inferred")
        self.assertEqual(result["edges"][0]["provenance"], "onchain")

    def test_payloads_are_deterministic_json_and_do_not_mutate_or_alias_documents(self):
        for document, build in ((self.flow, graph.flow_payload), (self.cluster, graph.cluster_payload)):
            with self.subTest(kind=build.__name__):
                before = copy.deepcopy(document)
                first, second = build(document, {}), build(document, {})
                self.assertEqual(json.dumps(first, allow_nan=False), json.dumps(second, allow_nan=False))
                self.assertEqual(document, before)
                first["records"][0]["from"] = "changed.near"
                first["coverage"].clear()
                first["bounds"].clear()
                if "facts" in first:
                    first["facts"][0]["source"]["block"] = 0
                else:
                    first["shares"]["buckets"].clear()
                    first["state_at_block"][0]["args"].clear()
                self.assertEqual(document, before)


if __name__ == "__main__":
    unittest.main()
