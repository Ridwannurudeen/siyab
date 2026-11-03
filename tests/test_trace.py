import contextlib
import io
import json
import re
import tempfile
import unittest
from decimal import Decimal
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
INCIDENT = {"date": "2026-04-16", "accounts": [{"subject": "start.near"}, {"subject": "worker.near"}]}
ISSUERS = [{"token": "usdt.tether-token.near", "method": "get_blacklist_status", "args_key": "account_id",
            "banned": "Banned", "decimals": 6},
           {"token": "usdc.near", "method": "is_blocklisted", "args_key": "account_id", "banned": True, "decimals": 6}]


def no_network(*args, **kwargs):
    raise AssertionError("the test opened the network")


def row(sender, receiver, n, outbound=True, usd=10.0):
    return {"account_id": sender, "other_account_id": receiver, "asset_id": "native:near",
            "amount": ("-" if outbound else "") + str(n), "transaction_id": f"tx{n}", "block_height": str(100 + n),
            "block_timestamp": str(1776211200000000000 + n), "method_name": None, "receipt_id": f"r{n}",
            "human_amount": (-1 if outbound else 1) * n / 10,
            "usd_amount": None if usd is None else (-usd if outbound else usd)}


class FakeTransfers:
    """Pages of transfers per account; a page list per account, chained by resume tokens. Each
    status in errors is raised as an HttpError, one per request, before any page is answered."""

    def __init__(self, pages, errors=()):
        self.pages = pages
        self.errors = list(errors)
        self.calls = []
        self.slept = []
        self.last_source = {"fetched_at": "2026-10-29T00:00:00Z"}

    def sleep(self, seconds):
        self.slept.append(seconds)

    def post(self, path, body):
        self.calls.append(body)
        if self.errors:
            raise rpc.HttpError(self.errors.pop(0), path)
        account_pages = self.pages.get(body["account_id"], [[]])
        index = int(body.get("resume_token") or 0)
        token = str(index + 1) if index + 1 < len(account_pages) else None
        return {"transfers": account_pages[index], "resume_token": token}


class FakeViews:
    """call_function answers per (token, method), each read at the next block."""

    def __init__(self, answers):
        self.answers = answers
        self.calls = []
        self.last_block_height = None
        self.last_source = {"fetched_at": "2026-11-05T00:00:00Z"}

    def call_function(self, contract, method, args):
        self.calls.append((contract, method, args))
        self.last_block_height = 1000 + len(self.calls)
        return self.answers[(contract, method)]


VIEWS = {("usdt.tether-token.near", "ft_balance_of"): "3291946547460",
         ("usdt.tether-token.near", "get_blacklist_status"): "Banned",
         ("usdc.near", "ft_balance_of"): "0", ("usdc.near", "is_blocklisted"): False}


class Store:
    def __init__(self, *assertions):
        self.assertions = list(assertions)


def assertion(subject, category, chain="near"):
    return SimpleNamespace(id=f"id-{subject}-{category}", subject=subject, category=category, chain=chain,
                           superseded_by=None)


STORE = Store(assertion("start.near", "incident"), assertion("intents.near", "infrastructure"),
              assertion("zec.omft.near", "bridge"), assertion("listed.near", "watch"))


def walk(pages, store=STORE, **options):
    """(document, transfers client, views client) of a walk from start.near over the given pages."""
    client, views = FakeTransfers(pages), FakeViews(VIEWS)
    return trace.trace("start.near", store, client, views, INCIDENT, ISSUERS, **options), client, views


class RefusalTest(unittest.TestCase):
    def test_a_labelled_subject_without_an_incident_label_is_refused_before_any_read(self):
        store = labels.load()
        self.assertTrue(store.by_subject("intents.near", "near"))
        for subject in ("intents.near", "nobody-here.near"):
            with self.subTest(subject=subject):
                client, views = FakeTransfers({}), FakeViews(VIEWS)
                with self.assertRaises(trace.Refused) as caught:
                    trace.trace(subject, store, client, views, INCIDENT, ISSUERS)
                self.assertEqual(str(caught.exception), "subject carries no incident label")
                self.assertEqual(client.calls, [])
                self.assertEqual(views.calls, [])

    def test_a_superseded_incident_label_does_not_open_a_trace(self):
        old = SimpleNamespace(id="id-old", subject="old.near", category="incident", chain="near",
                              superseded_by="id-new")
        client = FakeTransfers({})
        with self.assertRaises(trace.Refused):
            trace.trace("old.near", Store(old), client, FakeViews(VIEWS), INCIDENT, ISSUERS)
        self.assertEqual(client.calls, [])

    def test_an_incident_label_without_an_incident_page_is_refused_before_any_read(self):
        client = FakeTransfers({})
        with self.assertRaises(trace.Refused) as caught:
            trace.trace("start.near", STORE, client, FakeViews(VIEWS), None, ISSUERS)
        self.assertEqual(str(caught.exception), "no incident page names the subject")
        self.assertEqual(client.calls, [])
        self.assertIsNone(trace.incident_for("nobody-here.near"))
        self.assertEqual(trace.incident_for(COLLECTOR)["id"], RHEA["id"])

    def test_the_command_refuses_with_the_reason_and_exit_2(self):
        for subject in ("intents.near", "nobody-here.near"):
            with self.subTest(subject=subject):
                output = io.StringIO()
                with contextlib.redirect_stdout(output), mock.patch("urllib.request.urlopen", side_effect=no_network):
                    self.assertEqual(intel_main.main(["flow", subject]), 2)
                self.assertEqual(output.getvalue(), "refused: subject carries no incident label\n")


class WalkTest(unittest.TestCase):
    def test_inbound_rows_are_skipped_and_terminal_accounts_end_the_walk(self):
        document, client, _ = walk({"start.near": [[row("start.near", "a.near", 1), row("start.near", "intents.near", 2),
                                                    row("start.near", "b.near", 3, outbound=False)]],
                                    "a.near": [[row("a.near", "zec.omft.near", 4), row("a.near", "listed.near", 5)]]})
        self.assertEqual([(h["from"], h["to"]) for h in document["hops"]],
                         [("start.near", "a.near"), ("start.near", "intents.near"), ("a.near", "zec.omft.near"),
                          ("a.near", "listed.near")])
        self.assertEqual(document["terminal_accounts"], ["intents.near", "zec.omft.near"])
        self.assertEqual([c["account_id"] for c in client.calls], ["start.near", "a.near", "listed.near"])
        self.assertFalse(document["truncated"])
        self.assertEqual(document["hops"][0]["amount"], "1")
        self.assertEqual(document["assertion_ids"], ["id-start.near-incident"])

    def test_the_subject_is_a_terminal_when_the_walk_comes_back_to_it(self):
        document, client, _ = walk({"start.near": [[row("start.near", "a.near", 1)]],
                                    "a.near": [[row("a.near", "start.near", 2)]]})
        self.assertEqual(document["terminal_accounts"], ["start.near"])
        self.assertEqual([c["account_id"] for c in client.calls], ["start.near", "a.near"])

    def test_the_depth_bound(self):
        chain = {f"n{i}.near": [[row(f"n{i}.near", f"n{i + 1}.near", i)]] for i in range(6)}
        chain["start.near"] = [[row("start.near", "n0.near", 99)]]
        document, _, _ = walk(chain, depth=3)
        self.assertEqual([h["from"] for h in document["hops"]], ["start.near", "n0.near", "n1.near"])
        self.assertFalse(document["truncated"])
        self.assertEqual(trace.reached_depth(document), 3)
        self.assertEqual(document["depth_reached"], 3)
        self.assertEqual(trace.hop_depths(document), [1, 2, 3])
        self.assertTrue(trace.summary(document).startswith("3 hops over 4 accounts; depth 3 of 3, complete; "))

    def test_the_transfer_cap_truncates(self):
        pages = {"start.near": [[row("start.near", f"x{i}.near", i) for i in range(n, n + 3)] for n in (0, 3, 6)]}
        document, _, _ = walk(pages, cap=5)
        self.assertEqual(len(document["hops"]), 5)
        self.assertTrue(document["truncated"])

    def test_resume_tokens_are_followed(self):
        pages = {"start.near": [[row("start.near", "intents.near", 1)], [row("start.near", "intents.near", 2)]]}
        document, client, _ = walk(pages)
        self.assertEqual([h["transaction_hash"] for h in document["hops"]], ["tx1", "tx2"])
        filters = {"direction": "sender", "ignore_system": True, "from_timestamp_ms": 1776211200000, "limit": 100}
        self.assertEqual(client.calls[1], dict(account_id="start.near", resume_token="1", **filters))
        self.assertEqual(client.calls[0], dict(account_id="start.near", **filters))

    def test_the_body_carries_the_filters_and_the_window_start_and_no_usd_filter(self):
        document, client, _ = walk({"start.near": [[row("start.near", "intents.near", 1)]]})
        start_ms = 1776211200000
        self.assertEqual(start_ms, int(trace.datetime(2026, 4, 15, tzinfo=trace.UTC).timestamp()) * 1000)
        for body in client.calls:
            self.assertEqual(body["direction"], "sender")
            self.assertIs(body["ignore_system"], True)
            self.assertEqual(body["from_timestamp_ms"], start_ms)
            self.assertEqual(body["limit"], 100)
            self.assertFalse(set(body).intersection(("min_usd_amount", "min_amount", "min_human_amount", "asset_id",
                                                     "desc", "to_timestamp_ms")))
        self.assertEqual(document["filters"], {"direction": "sender", "ignore_system": True,
                                               "from_timestamp_ms": start_ms, "limit": 100})
        self.assertEqual(document["window"], {"from": "2026-04-15T00:00:00Z", "from_timestamp_ms": start_ms,
                                              "incident_date": "2026-04-16",
                                              "rule": "00:00 UTC on the day before the incident date"})
        self.assertEqual((trace.DEFAULT_MAX, trace.MAX_REQUESTS, trace.MIN_USD), (15000, 150, 1.0))
        hop = document["hops"][0]
        self.assertEqual((hop["usd_amount"], hop["human_amount"], hop["block_timestamp"]),
                         (10.0, 0.1, 1776211200000000001))

    def test_rows_without_a_usd_amount_are_counted_apart_never_dropped(self):
        pages = {"start.near": [[row("start.near", "intents.near", 1), row("start.near", "intents.near", 2, usd=None),
                                 row("start.near", "intents.near", 3, usd=None), row("start.near", "x.near", 4, usd=None),
                                 row("start.near", "y.near", 5, usd=0.5)]]}
        document, _, _ = walk(pages)
        self.assertEqual(len(document["hops"]), 5)
        self.assertEqual([h["usd_amount"] for h in document["hops"]], [10.0, None, None, None, 0.5])
        shares = document["shares"]
        self.assertEqual(shares["without_usd"], {"count": 3, "base_units": {"native:near": "9"}})
        self.assertEqual(shares["below_min_usd"], {"count": 1, "usd": "0.5"})
        self.assertEqual(shares["rows"], 1)
        self.assertEqual(shares["buckets"], [{"to": "intents.near", "kind": "terminal", "usd": "10.0", "count": 1,
                                              "percent": 100.0}])
        self.assertIn("across 1 rows (3 rows without a USD amount, 1 below 1.0 USD)", trace.summary(document))

    def test_shares_sum_to_the_direct_outbound_total_and_name_every_terminal(self):
        pages = {"start.near": [[row("start.near", "intents.near", 1, usd=60.25),
                                 row("start.near", "zec.omft.near", 2, usd=20.0),
                                 row("start.near", "worker.near", 3, usd=10.0),
                                 row("start.near", "anon1.near", 4, usd=5.5),
                                 row("start.near", "anon2.near", 5, usd=4.25)]],
                 "anon1.near": [[row("anon1.near", "intents.near", 6, usd=5000.0)]]}
        document, _, _ = walk(pages)
        shares = document["shares"]
        self.assertEqual(shares["direct_outbound_usd"], "100.00")
        self.assertEqual(sum(Decimal(b["usd"]) for b in shares["buckets"]), Decimal(shares["direct_outbound_usd"]))
        self.assertEqual(sum(b["count"] for b in shares["buckets"]), shares["rows"])
        self.assertEqual(shares["rows"], 5)
        self.assertAlmostEqual(sum(b["percent"] for b in shares["buckets"]), 100.0, places=6)
        self.assertEqual([(b["to"], b["kind"], b["usd"], b["count"], b["percent"]) for b in shares["buckets"]],
                         [("intents.near", "terminal", "60.25", 1, 60.25),
                          ("zec.omft.near", "terminal", "20.0", 1, 20.0),
                          ("worker.near", "named in the report", "10.0", 1, 10.0),
                          (trace.NOT_NAMED, "not named", "9.75", 2, 9.75)])
        direct = {h["to"] for h in document["hops"] if h["from"] == "start.near"}
        self.assertEqual(direct.intersection(("intents.near", "zec.omft.near")),
                         {b["to"] for b in shares["buckets"] if b["kind"] == "terminal"})
        self.assertIn("shares of the direct outbound value: intents.near 60.25 percent, zec.omft.near 20.0 percent, worker.near 10.0 percent, "
                      f"{trace.NOT_NAMED} 9.75 percent;", trace.summary(document))
        self.assertIn("direct outbound after 2026-04-15 00:00 UTC, read in full: 100.00 USD across 5 rows",
                      trace.summary(document))

    def test_completeness_names_the_account_and_depth_where_the_walk_stopped(self):
        complete, _, _ = walk({"start.near": [[row("start.near", "a.near", 1)]]})
        self.assertEqual(complete["completeness"], "complete")
        pages = {"start.near": [[row("start.near", "a.near", 1), row("start.near", "b.near", 2)]],
                 "a.near": [[row("a.near", f"x{i}.near", 10 + i) for i in range(3)]]}
        capped, _, _ = walk(pages, cap=4)
        self.assertEqual(capped["completeness"], {"account": "a.near", "depth": 1,
                                                  "reason": "the transfer cap of 4 hops"})
        self.assertTrue(capped["truncated"])
        self.assertIn("depth 2 of 3, stopped while reading a.near (1 hop from the subject): the transfer cap of 4 hops;",
                      trace.summary(capped))
        self.assertIn("direct outbound after 2026-04-15 00:00 UTC, read in full: ", trace.summary(capped))
        self.assertEqual(trace.stop_state(capped["completeness"]), "stopped while reading an account 1 hop from the subject")
        self.assertEqual(trace.stop_state({"account": "b.near", "depth": 2, "reason": "r"}),
                         "stopped while reading an account 2 hops from the subject")
        self.assertEqual(trace.stop_state("complete"), "complete")
        many = {"start.near": [[row("start.near", f"x{i}.near", i)] for i in range(trace.MAX_REQUESTS + 5)]}
        budget, client, _ = walk(many, cap=10000)
        self.assertEqual(len(client.calls), trace.MAX_REQUESTS)
        self.assertEqual(budget["completeness"], {"account": "start.near", "depth": 0,
                                                  "reason": "the page budget of 150 requests"})
        self.assertEqual(trace.stop_state(budget["completeness"]), "stopped while reading the subject")
        self.assertIn("depth 1 of 3, stopped while reading the subject: the page budget of 150 requests; direct "
                      "outbound after 2026-04-15 00:00 UTC read in part, to 2026-04-15 00:00 UTC: ", trace.summary(budget))

    def test_state_at_block_reads_each_issuer_once_with_a_block(self):
        document, _, views = walk({"start.near": [[row("start.near", "intents.near", 1)]]})
        self.assertEqual(views.calls, [("usdt.tether-token.near", "ft_balance_of", {"account_id": "start.near"}),
                                       ("usdt.tether-token.near", "get_blacklist_status", {"account_id": "start.near"}),
                                       ("usdc.near", "ft_balance_of", {"account_id": "start.near"}),
                                       ("usdc.near", "is_blocklisted", {"account_id": "start.near"})])
        reads = document["state_at_block"]
        self.assertEqual([(r["token"], r["method"], r["block"]) for r in reads],
                         [("usdt.tether-token.near", "ft_balance_of", 1001),
                          ("usdt.tether-token.near", "get_blacklist_status", 1002),
                          ("usdc.near", "ft_balance_of", 1003), ("usdc.near", "is_blocklisted", 1004)])
        for read in reads:
            self.assertEqual(read["result_sha256"], trace._result_sha256(read["result"]))
            self.assertRegex(read["result_sha256"], "^[0-9a-f]{64}$")
        self.assertEqual([r.get("banned") for r in reads], [None, True, None, False])
        self.assertIn('subject: 3,291,946.54746 of usdt.tether-token.near at block 1001, banned by the issuer, '
                      'get_blacklist_status answers "Banned" at block 1002; 0 of usdc.near at block 1003, issuer '
                      'status is_blocklisted answers false at block 1004', trace.summary(document))
        self.assertEqual(document["fetched_at"], "2026-11-05T00:00:00Z")

    def test_a_429_waits_and_retries_and_the_third_in_a_row_stops(self):
        client = FakeTransfers({"start.near": [[row("start.near", "intents.near", 1)]]}, errors=[429, 429])
        document = trace.trace("start.near", STORE, client, FakeViews(VIEWS), INCIDENT, ISSUERS)
        self.assertEqual(len(document["hops"]), 1)
        self.assertEqual(client.slept, [10, 10])
        stopped = FakeTransfers({}, errors=[429, 429, 429])
        with self.assertRaises(rpc.HttpError):
            trace.trace("start.near", STORE, stopped, FakeViews(VIEWS), INCIDENT, ISSUERS)
        self.assertEqual((len(stopped.calls), stopped.slept), (3, [10, 10]))

    def test_the_summary_line_holds_no_authored_freeze_word(self):
        document, _, _ = walk({"start.near": [[row("start.near", "intents.near", 1)]]})
        committed = json.loads(FLOW_PATH.read_text(encoding="utf-8"))
        for line in (trace.summary(document), trace.summary(committed)):
            with self.subTest(line=line[:40]):
                self.assertIsNone(re.search("froz|freez", line, re.IGNORECASE))


class CommittedTraceTest(unittest.TestCase):
    def setUp(self):
        self.document = json.loads(FLOW_PATH.read_text(encoding="utf-8"))

    def test_the_rhea_collector_trace_has_the_shape_and_every_hop_a_hash_and_a_block(self):
        self.assertEqual(sorted(self.document), sorted(["subject", "assertion_ids", "depth", "filters", "window", "hops",
                                                        "terminal_accounts", "truncated", "completeness",
                                                        "depth_reached", "shares", "state_at_block", "fetched_at"]))
        self.assertEqual(self.document["subject"], COLLECTOR)
        self.assertLessEqual(len(self.document["hops"]), trace.DEFAULT_MAX)
        self.assertTrue(self.document["hops"])
        for hop in self.document["hops"]:
            with self.subTest(hop=hop["receipt_id"]):
                self.assertEqual(sorted(hop), sorted(["from", "to", "asset_id", "amount", "human_amount", "usd_amount",
                                                      "transaction_hash", "block", "block_timestamp", "method_name",
                                                      "receipt_id"]))
                self.assertTrue(hop["transaction_hash"])
                self.assertIsInstance(hop["block"], int)
                self.assertGreater(hop["block"], 0)
                self.assertGreaterEqual(hop["block_timestamp"] // 1000000, self.document["window"]["from_timestamp_ms"])
        store = labels.load()
        ids = {a.id for a in store.assertions if a.subject == COLLECTOR and a.category == "incident"}
        self.assertEqual(set(self.document["assertion_ids"]), ids)
        self.assertEqual(self.document["window"], trace.window(RHEA["date"]))

    def test_the_committed_trace_reads_the_subject_in_full_and_the_budget_stops_it_at_depth_1(self):
        self.assertTrue(self.document["truncated"])
        self.assertEqual(trace.reached_depth(self.document), 2)
        self.assertEqual(self.document["depth_reached"], 2)
        self.assertEqual(self.document["completeness"]["depth"], 1)
        self.assertEqual(self.document["completeness"]["reason"], "the page budget of 150 requests")
        start_ns = self.document["window"]["from_timestamp_ms"] * 1000000
        direct = [h for h in self.document["hops"] if h["from"] == COLLECTOR]
        self.assertTrue(direct)
        for hop in direct:
            self.assertGreaterEqual(hop["block_timestamp"], start_ns)
        self.assertIn(f"depth 2 of 3, stopped while reading {self.document['completeness']['account']} (1 hop from the "
                      "subject): the page budget of 150 requests; direct outbound after 2026-04-15 00:00 UTC, read in "
                      "full: ", trace.summary(self.document))

    def test_the_committed_state_at_block_reads_the_collector_banned_by_the_usdt_issuer(self):
        reads = {(r["token"], r["method"]): r for r in self.document["state_at_block"]}
        status = reads[("usdt.tether-token.near", "get_blacklist_status")]
        self.assertEqual((status["result"], status["banned"]), ("Banned", True))
        self.assertEqual(reads[("usdt.tether-token.near", "ft_balance_of")]["result"], "3291946547460")
        for read in self.document["state_at_block"]:
            self.assertIsInstance(read["block"], int)
        self.assertIn('3,291,946.54746 of usdt.tether-token.near', trace.summary(self.document))

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
            trace.trace(COLLECTOR, labels.load(), client, FakeViews(VIEWS), trace.incident_for(COLLECTOR), ISSUERS)

    def test_a_trace_name_cannot_leave_the_flows_directory(self):
        with self.assertRaises(ValueError):
            trace.flow_path(COLLECTOR, "../labels")


if __name__ == "__main__":
    unittest.main()
