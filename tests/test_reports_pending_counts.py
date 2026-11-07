import contextlib
import io
import json
import os
import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path
from unittest import mock

from gate import rpc
from intel import __main__ as intel_main
from intel import reports


REPORTS_ACCOUNT = "reports.testnet"
NOW = datetime(2026, 11, 7, 9, 30, tzinfo=UTC).timestamp()


def http_reply(payload):
    handle = mock.MagicMock()
    handle.__enter__.return_value.read.return_value = json.dumps(payload).encode("utf-8")
    return handle


class ReportsRPC:
    def __init__(self, total, report_rows, summaries):
        self.total = total
        self.report_rows = report_rows
        self.summaries = summaries
        self.last_block_height = None
        self.calls = []

    def call_function(self, contract, method, args, *, block_id=None):
        self.calls.append((contract, method, args, block_id))
        if method == "totals":
            self.last_block_height = 234
            return {"reports": self.total, "disputes": 2}
        if block_id != 234:
            raise AssertionError("a follow-up view was not pinned to totals height")
        if method == "get_report":
            return self.report_rows.get(args["id"])
        if method == "subject_summary":
            return self.summaries[args["subject"]]
        raise AssertionError(method)


class PendingCountsTest(unittest.TestCase):
    def test_appends_counts_and_cross_checks_summary_at_totals_block(self):
        client = ReportsRPC(3, {
            0: {"id": 0, "subject": "vendor.testnet", "state": "pending"},
            1: {"id": 1, "subject": "vendor.testnet", "state": "accepted"},
            2: {"id": 2, "subject": "other.testnet", "state": "pending"},
        }, {
            "vendor.testnet": {"pending": 1},
            "other.testnet": {"pending": 0},
        })
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "pending-counts.jsonl"
            first = reports.record_pending_counts(client, REPORTS_ACCOUNT, path, clock=lambda: NOW)
            second = reports.record_pending_counts(client, REPORTS_ACCOUNT, path, clock=lambda: NOW + 1)
            rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
        self.assertEqual(first["recorded_at_utc"], "2026-11-07T09:30:00Z")
        self.assertEqual(first["block_height"], 234)
        self.assertEqual(first["totals"], {"reports": 3, "disputes": 2})
        self.assertEqual(first["subjects"], [
            {"subject": "other.testnet", "pending": 1, "summary_pending": 0, "mismatch": True},
            {"subject": "vendor.testnet", "pending": 1, "summary_pending": 1, "mismatch": False},
        ])
        self.assertTrue(first["mismatch"])
        self.assertEqual(first["report_scan"], {"ids_scanned": 3, "id_limit": 500,
                                                  "truncated": False, "missing_ids": []})
        self.assertEqual(len(rows), 2)
        self.assertEqual(second["block_height"], 234)
        self.assertTrue(all(call[3] == 234 for call in client.calls if call[1] != "totals"))

    def test_empty_contract_appends_a_zero_row_without_subject_views(self):
        client = ReportsRPC(0, {}, {})
        with tempfile.TemporaryDirectory() as tmp:
            row = reports.record_pending_counts(client, REPORTS_ACCOUNT, Path(tmp) / "counts.jsonl",
                                                clock=lambda: NOW)
        self.assertEqual(row["totals"], {"reports": 0, "disputes": 2})
        self.assertEqual(row["subjects"], [])
        self.assertFalse(row["mismatch"])
        self.assertEqual([call[1] for call in client.calls], ["totals"])

    def test_report_id_walk_is_capped_at_500_and_marks_truncation(self):
        total = 505
        rows = {report_id: {"id": report_id, "subject": "vendor.testnet", "state": "pending"}
                for report_id in range(500)}
        client = ReportsRPC(total, rows, {"vendor.testnet": {"pending": 500}})
        with tempfile.TemporaryDirectory() as tmp:
            row = reports.record_pending_counts(client, REPORTS_ACCOUNT, Path(tmp) / "counts.jsonl",
                                                clock=lambda: NOW)
        self.assertTrue(row["report_scan"]["truncated"])
        self.assertEqual(row["report_scan"]["ids_scanned"], 500)
        self.assertEqual(sum(call[1] == "get_report" for call in client.calls), 500)

    def test_call_function_can_pin_a_view_without_changing_its_returned_json(self):
        client = rpc.Client(rpc.TESTNET_RPC_URL, "off")
        payload = {"jsonrpc": "2.0", "id": "gate", "result": {
            "result": list(json.dumps({"reports": 0, "disputes": 0}).encode("utf-8")),
            "block_height": 234, "block_hash": "h234"}}
        with mock.patch("urllib.request.urlopen", return_value=http_reply(payload)) as opened:
            result = client.call_function(REPORTS_ACCOUNT, "totals", {}, block_id=234)
        request = json.loads(opened.call_args.args[0].data)
        self.assertEqual(result, {"reports": 0, "disputes": 0})
        self.assertEqual(client.last_block_height, 234)
        self.assertEqual(request["params"]["block_id"], 234)
        self.assertNotIn("finality", request["params"])

    def test_cli_uses_reports_account_and_testnet_client(self):
        client = ReportsRPC(0, {}, {})
        fake_row = {"block_height": 234, "subjects": [], "mismatch": False,
                    "report_scan": {"ids_scanned": 0, "id_limit": 500}}
        with mock.patch.dict(os.environ, {"REPORTS_ACCOUNT_ID": REPORTS_ACCOUNT}, clear=True), \
                mock.patch("intel.__main__.rpc.Client", return_value=client) as client_factory, \
                mock.patch.object(reports, "record_pending_counts", return_value=fake_row) as recorder, \
                contextlib.redirect_stdout(io.StringIO()) as output:
            result = intel_main.main(["reports-pending-counts"])
        self.assertEqual(result, 0)
        client_factory.assert_called_once_with(rpc.TESTNET_RPC_URL,
                                               fixtures_dir=Path(intel_main.__file__).resolve().parent.parent
                                               / "fixtures" / "reports")
        recorder.assert_called_once_with(client, REPORTS_ACCOUNT)
        self.assertIn("scanned 0 report ids (cap 500)", output.getvalue())


if __name__ == "__main__":
    unittest.main()