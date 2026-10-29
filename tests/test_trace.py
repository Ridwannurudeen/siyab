import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from gate import rpc
from intel import __main__ as intel_main
from intel import api, labels, trace

ROOT = Path(__file__).resolve().parent.parent
RHEA = json.loads((ROOT / "intel" / "incidents" / "rhea.json").read_text(encoding="utf-8"))
COLLECTOR = RHEA["accounts"][0]["subject"]
FLOW_PATH = trace.FLOWS_DIR / "rhea-collector.json"


def no_network(*args, **kwargs):
    raise AssertionError("the test opened the network")


def row(sender, receiver, n, outbound=True):
    return {"account_id": sender, "other_account_id": receiver, "asset_id": "native:near",
            "amount": ("-" if outbound else "") + str(n), "transaction_id": f"tx{n}", "block_height": str(100 + n),
            "method_name": None, "receipt_id": f"r{n}"}


class FakeTransfers:
    """Pages of transfers per account; a page list per account, chained by resume tokens."""

    def __init__(self, pages):
        self.pages = pages
        self.calls = []
        self.last_source = {"fetched_at": "2026-10-29T00:00:00Z"}

    def post(self, path, body):
        self.calls.append(body)
        account_pages = self.pages.get(body["account_id"], [[]])
        index = int(body.get("resume_token") or 0)
        token = str(index + 1) if index + 1 < len(account_pages) else None
        return {"transfers": account_pages[index], "resume_token": token}


class Store:
    def __init__(self, *assertions):
        self.assertions = list(assertions)


def assertion(subject, category, chain="near"):
    return SimpleNamespace(id=f"id-{subject}-{category}", subject=subject, category=category, chain=chain,
                           superseded_by=None)


STORE = Store(assertion("start.near", "incident"), assertion("intents.near", "infrastructure"),
              assertion("zec.omft.near", "bridge"), assertion("listed.near", "watch"))


class RefusalTest(unittest.TestCase):
    def test_a_labelled_subject_without_an_incident_label_is_refused_before_any_read(self):
        store = labels.load()
        self.assertTrue(store.by_subject("intents.near", "near"))
        for subject in ("intents.near", "nobody-here.near"):
            with self.subTest(subject=subject):
                client = FakeTransfers({})
                with self.assertRaises(trace.Refused) as caught:
                    trace.trace(subject, store, client)
                self.assertEqual(str(caught.exception), "subject carries no incident label")
                self.assertEqual(client.calls, [])

    def test_the_command_refuses_with_the_reason_and_exit_2(self):
        for subject in ("intents.near", "nobody-here.near"):
            with self.subTest(subject=subject):
                output = io.StringIO()
                with contextlib.redirect_stdout(output), mock.patch("urllib.request.urlopen", side_effect=no_network):
                    self.assertEqual(intel_main.main(["flow", subject]), 2)
                self.assertEqual(output.getvalue(), "refused: subject carries no incident label\n")


class WalkTest(unittest.TestCase):
    def test_inbound_rows_are_skipped_and_terminal_accounts_end_the_walk(self):
        client = FakeTransfers({"start.near": [[row("start.near", "a.near", 1), row("start.near", "intents.near", 2),
                                                row("start.near", "b.near", 3, outbound=False)]],
                                "a.near": [[row("a.near", "zec.omft.near", 4), row("a.near", "listed.near", 5)]]})
        document = trace.trace("start.near", STORE, client)
        self.assertEqual([(h["from"], h["to"]) for h in document["hops"]],
                         [("start.near", "a.near"), ("start.near", "intents.near"), ("a.near", "zec.omft.near"),
                          ("a.near", "listed.near")])
        self.assertEqual(document["terminal_accounts"], ["intents.near", "zec.omft.near"])
        self.assertEqual([c["account_id"] for c in client.calls], ["start.near", "a.near", "listed.near"])
        self.assertFalse(document["truncated"])
        self.assertEqual(document["hops"][0]["amount"], "1")
        self.assertEqual(document["assertion_ids"], ["id-start.near-incident"])

    def test_the_depth_bound(self):
        chain = {f"n{i}.near": [[row(f"n{i}.near", f"n{i + 1}.near", i)]] for i in range(6)}
        chain["start.near"] = [[row("start.near", "n0.near", 99)]]
        document = trace.trace("start.near", STORE, FakeTransfers(chain), depth=3)
        self.assertEqual([h["from"] for h in document["hops"]], ["start.near", "n0.near", "n1.near"])
        self.assertFalse(document["truncated"])

    def test_the_transfer_cap_truncates(self):
        pages = {"start.near": [[row("start.near", f"x{i}.near", i) for i in range(n, n + 3)] for n in (0, 3, 6)]}
        document = trace.trace("start.near", STORE, FakeTransfers(pages), cap=5)
        self.assertEqual(len(document["hops"]), 5)
        self.assertTrue(document["truncated"])

    def test_resume_tokens_are_followed(self):
        pages = {"start.near": [[row("start.near", "intents.near", 1)], [row("start.near", "intents.near", 2)]]}
        client = FakeTransfers(pages)
        document = trace.trace("start.near", STORE, client)
        self.assertEqual([h["transaction_hash"] for h in document["hops"]], ["tx1", "tx2"])
        self.assertEqual(client.calls[1], {"account_id": "start.near", "limit": 100, "resume_token": "1"})
        self.assertEqual(client.calls[0], {"account_id": "start.near", "limit": 100})


class CommittedTraceTest(unittest.TestCase):
    def setUp(self):
        self.document = json.loads(FLOW_PATH.read_text(encoding="utf-8"))

    def test_the_rhea_collector_trace_has_the_shape_and_every_hop_a_hash_and_a_block(self):
        self.assertEqual(sorted(self.document), sorted(["subject", "assertion_ids", "depth", "hops", "terminal_accounts",
                                                        "truncated", "fetched_at"]))
        self.assertEqual(self.document["subject"], COLLECTOR)
        self.assertLessEqual(len(self.document["hops"]), trace.DEFAULT_MAX)
        self.assertTrue(self.document["hops"])
        for hop in self.document["hops"]:
            with self.subTest(hop=hop["receipt_id"]):
                self.assertEqual(sorted(hop), sorted(["from", "to", "asset_id", "amount", "transaction_hash", "block",
                                                      "method_name", "receipt_id"]))
                self.assertTrue(hop["transaction_hash"])
                self.assertIsInstance(hop["block"], int)
                self.assertGreater(hop["block"], 0)
        store = labels.load()
        ids = {a.id for a in store.assertions if a.subject == COLLECTOR and a.category == "incident"}
        self.assertEqual(set(self.document["assertion_ids"]), ids)

    def test_a_replay_rewrites_the_committed_trace(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch("urllib.request.urlopen", side_effect=no_network):
            path = Path(tmp) / "rhea-collector.json"
            path.write_bytes(FLOW_PATH.read_bytes())
            self.assertEqual(trace.main(COLLECTOR, trace.DEFAULT_DEPTH, trace.DEFAULT_MAX, None, False,
                                        out=lambda line: None, flows_dir=Path(tmp)), 0)
            self.assertEqual(path.read_bytes(), FLOW_PATH.read_bytes())

    def test_the_api_serves_the_trace_by_its_subject_and_refuses_the_rest(self):
        store = labels.load()
        self.assertEqual(api.route(f"/flow/{COLLECTOR}", store), (200, self.document))
        self.assertEqual(api.route("/flow/intents.near", store), (403, {"error": "subject carries no incident label"}))
        worker = RHEA["accounts"][2]["subject"]
        self.assertEqual(api.route(f"/flow/{worker}", store), (404, {"error": f"no exit trace for {worker}"}))

    def test_a_missing_page_in_replay_stops(self):
        client = rpc.Client(trace.TRANSFERS_URL, "replay", kind="http", fixtures_dir=Path(tempfile.gettempdir()) / "none")
        with self.assertRaises(rpc.FixtureMiss):
            trace.trace(COLLECTOR, labels.load(), client)

    def test_a_trace_name_cannot_leave_the_flows_directory(self):
        with self.assertRaises(ValueError):
            trace.flow_path(COLLECTOR, "../labels")


if __name__ == "__main__":
    unittest.main()
