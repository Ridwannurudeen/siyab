import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from intel import labels
from intel.reconcile import ReconcileError, read_rows, reconcile, to_label_row


CONTRACT = "reports.synthetic.testnet"
FETCHED_AT = "2026-11-06T10:00:00Z"
ROOT = Path(__file__).resolve().parents[1]


def event_line(name, data, block_height, fetched_at=FETCHED_AT):
    payload = {
        "standard": "shieldbot-near",
        "version": "1.0.0",
        "event": name,
        "data": [data],
    }
    return {
        "line": "EVENT_JSON:" + json.dumps(payload, separators=(",", ":")),
        "block_height": block_height,
        "fetched_at": fetched_at,
    }


def accepted(report_id, block_height, subject="subject.synthetic.testnet", fetched_at=FETCHED_AT):
    return event_line(
        "report_moderated",
        {
            "report_id": report_id,
            "subject": subject,
            "state": "accepted",
            "body_sha256": f"{report_id:064x}",
            "moderator": "moderator.synthetic.testnet",
            "reason_sha256": f"{report_id + 100:064x}",
        },
        block_height,
        fetched_at,
    )


def overturned(report_id, block_height, subject="subject.synthetic.testnet"):
    return event_line(
        "dispute_resolved",
        {
            "report_id": report_id,
            "dispute_id": report_id + 1000,
            "subject": subject,
            "state": "overturned",
            "body_sha256": f"{report_id:064x}",
            "moderator": "moderator.synthetic.testnet",
            "reason_sha256": f"{report_id + 200:064x}",
        },
        block_height,
    )


def page_report(report_id, subject="subject.synthetic.testnet", state="accepted"):
    return {
        "id": report_id,
        "body_sha256": f"{report_id:064x}",
        "submitter": "submitter.synthetic.testnet",
        "subject": subject,
        "kind": "scam",
        "outcome": "loss",
        "evidence_sha256": f"{report_id + 300:064x}",
        "evidence_url": "https://evidence.example/report",
        "submitted_ms": 1793959200000,
        "state": state,
        "moderator": "moderator.synthetic.testnet",
        "reason_sha256": f"{report_id + 100:064x}",
        "moderated_ms": 1793959200000,
        "dispute_ids": [],
    }


class ReconcileTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.rows_path = Path(temporary.name) / "rows.jsonl"

    def run_reconcile(self, events, **kwargs):
        return reconcile(events, self.rows_path, CONTRACT, now=FETCHED_AT, **kwargs)

    def test_replay_is_idempotent(self):
        fixture = ROOT / "fixtures" / "records" / "events" / "replay.jsonl"
        events = [json.loads(line) for line in fixture.read_text(encoding="utf-8").splitlines()]
        first = self.run_reconcile(events)
        before = self.rows_path.read_bytes()

        replay = [dict(event, fetched_at="2026-11-06T11:00:00Z") for event in events]
        second = self.run_reconcile(replay)

        self.assertEqual(len(first), 2)
        self.assertEqual(second, first)
        self.assertEqual(self.rows_path.read_bytes(), before)

    def test_out_of_order_events_are_applied_in_block_order(self):
        rows = self.run_reconcile([overturned(0, 20), accepted(0, 10)])

        self.assertEqual([row["event"] for row in rows], ["report_moderated", "dispute_resolved"])
        self.assertEqual(rows[1]["supersedes"], {
            "contract": CONTRACT,
            "report_id": 0,
            "event": "report_moderated",
            "source_block": 10,
        })

    def test_since_block_recovers_from_subject_pages(self):
        calls = []

        def page_reader(subject, start, limit):
            calls.append((subject, start, limit))
            reports = [page_report(1), page_report(2), page_report(3)] if start == 0 else []
            return {
                "source_block": 31,
                "fetched_at": FETCHED_AT,
                "reports": reports,
            }

        rows = self.run_reconcile(
            [accepted(1, 10), accepted(3, 30)], page_reader=page_reader, since_block=1
        )

        recovered = [row for row in rows if row["report_id"] == 2]
        self.assertEqual(len(recovered), 1)
        self.assertEqual(recovered[0]["source_method"], "reports_by_subject")
        self.assertEqual(calls, [("subject.synthetic.testnet", 0, 50)])

    def test_recovery_page_supersedes_an_accepted_row_when_an_overturn_event_was_missed(self):
        accepted_row = self.run_reconcile([accepted(1, 10)])[0]

        def page_reader(subject, start, limit):
            return {
                "source_block": 31,
                "fetched_at": FETCHED_AT,
                "reports": [page_report(1, state="overturned")],
            }

        rows = self.run_reconcile([], page_reader=page_reader, since_block=20)

        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[1]["event"], "dispute_resolved")
        self.assertEqual(rows[1]["state"], "overturned")
        self.assertEqual(rows[1]["supersedes"], {
            "contract": CONTRACT,
            "report_id": accepted_row["report_id"],
            "event": accepted_row["event"],
            "source_block": accepted_row["source_block"],
        })

    def test_explicit_subjects_start_page_recovery_without_events_or_stored_rows(self):
        calls = []

        def page_reader(subject, start, limit):
            calls.append((subject, start, limit))
            return {"source_block": 31, "fetched_at": FETCHED_AT, "reports": [page_report(2)]}

        rows = self.run_reconcile([], subjects=["subject.synthetic.testnet"], page_reader=page_reader)

        self.assertEqual([row["report_id"] for row in rows], [2])
        self.assertEqual(calls, [("subject.synthetic.testnet", 0, 50)])

    def test_report_id_scan_recovers_reports_without_known_subjects(self):
        calls = []

        def get_report(report_id):
            calls.append(report_id)
            return {
                "source_block": 31,
                "fetched_at": FETCHED_AT,
                "report": page_report(report_id),
            }

        rows = self.run_reconcile([], report_total=2, get_report=get_report)

        self.assertEqual([row["report_id"] for row in rows], [0, 1])
        self.assertEqual([row["source_method"] for row in rows], ["get_report", "get_report"])
        self.assertEqual(calls, [0, 1])

    def test_page_recovery_label_source_uses_page_arguments_and_block(self):
        subject = "subject.synthetic.testnet"

        def page_reader(requested_subject, start, limit):
            self.assertEqual((requested_subject, start, limit), (subject, 0, 50))
            return {
                "source_block": 31,
                "fetched_at": FETCHED_AT,
                "reports": [page_report(2, subject=subject)],
            }

        row = self.run_reconcile([], subjects=[subject], page_reader=page_reader)[0]
        label_row = to_label_row(row, rpc="https://rpc.example.test")

        self.assertEqual(label_row["source"], {
            "rpc": "https://rpc.example.test",
            "method": "reports_by_subject",
            "args": {"subject": subject, "from": 0, "limit": 50},
            "block": 31,
        })
        labels.validate(label_row)

    def test_id_walk_label_source_uses_get_report_arguments_and_block(self):
        row = self.run_reconcile(
            [],
            report_total=1,
            get_report=lambda report_id: {
                "source_block": 31,
                "fetched_at": FETCHED_AT,
                "report": page_report(report_id),
            },
        )[0]
        label_row = to_label_row(row, rpc="https://rpc.example.test")

        self.assertEqual(label_row["source"], {
            "rpc": "https://rpc.example.test",
            "method": "get_report",
            "args": {"id": 0},
            "block": 31,
        })
        labels.validate(label_row)

    def test_page_recovered_accepted_label_can_be_superseded(self):
        subject = "subject.synthetic.testnet"

        def page_reader(requested_subject, start, limit):
            self.assertEqual((requested_subject, start, limit), (subject, 0, 50))
            return {
                "source_block": 31,
                "fetched_at": FETCHED_AT,
                "reports": [page_report(2, subject=subject)],
            }

        accepted_row = self.run_reconcile([], subjects=[subject], page_reader=page_reader)[0]
        accepted_label = to_label_row(accepted_row, rpc="https://rpc.example.test")
        overturned_row = self.run_reconcile([overturned(2, 40)])[-1]

        correction_label = to_label_row(
            overturned_row,
            rpc="https://rpc.example.test",
            superseded_row=accepted_label,
        )

        self.assertEqual(accepted_label["superseded_by"], correction_label["id"])
        self.assertEqual(accepted_label["source"]["method"], "reports_by_subject")
        self.assertEqual(correction_label["source"]["method"], "get_report")

    def test_nonconsecutive_report_ids_reconcile_without_a_page_reader(self):
        rows = self.run_reconcile([accepted(0, 10), accepted(3, 30), accepted(7, 70)])

        self.assertEqual([row["report_id"] for row in rows], [0, 3, 7])

    def test_an_overturn_appends_a_superseding_row(self):
        original = self.run_reconcile([accepted(1, 10)])[0]
        original_line = self.rows_path.read_text(encoding="utf-8").splitlines()[0]

        rows = self.run_reconcile([overturned(1, 20)])

        self.assertEqual(len(rows), 2)
        self.assertEqual(self.rows_path.read_text(encoding="utf-8").splitlines()[0], original_line)
        self.assertEqual(rows[1]["supersedes"], {
            "contract": CONTRACT,
            "report_id": original["report_id"],
            "event": original["event"],
            "source_block": original["source_block"],
        })

    def test_page_scan_adds_reports_not_named_by_events(self):
        def page_reader(subject, start, limit):
            return {
                "source_block": 31,
                "fetched_at": FETCHED_AT,
                "reports": [page_report(1), page_report(2), page_report(3), page_report(4)],
            }

        rows = self.run_reconcile(
            [accepted(1, 10), accepted(3, 30)], page_reader=page_reader, since_block=1
        )

        self.assertIn(4, [row["report_id"] for row in rows])

    def test_expired_rows_are_returned_with_their_age(self):
        fetched_at = "2026-11-01T10:00:00Z"
        self.run_reconcile([accepted(1, 10, fetched_at=fetched_at)])

        rows = read_rows(self.rows_path, now="2026-11-10T10:00:00Z")

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["age_seconds"], 9 * 24 * 60 * 60)
        self.assertTrue(rows[0]["revalidation_due"])
        self.assertEqual(len(self.rows_path.read_text(encoding="utf-8").splitlines()), 1)

    def test_malformed_event_line_raises_the_declared_error(self):
        malformed = {"line": "EVENT_JSON:{", "block_height": 10, "fetched_at": FETCHED_AT}

        with self.assertRaises(ReconcileError):
            self.run_reconcile([malformed])

    def test_to_label_row_marks_report_provenance(self):
        row = self.run_reconcile([accepted(1, 10)])[0]

        label_row = to_label_row(row, rpc="https://rpc.example.test")

        self.assertEqual(label_row["provenance"], "onchain")
        self.assertEqual(label_row["subject"], "subject.synthetic.testnet")
        self.assertEqual(label_row["source"]["rpc"], "https://rpc.example.test")
        self.assertEqual(label_row["source"]["method"], "get_report")
        self.assertEqual(label_row["source"]["args"], {"id": 1})
        self.assertEqual(label_row["source"]["block"], 10)
        labels.validate(label_row)
        canonical = {key: value for key, value in label_row.items() if key not in ("id", "superseded_by")}
        payload = json.dumps(canonical, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        self.assertEqual(label_row["id"], hashlib.sha256(payload).hexdigest())

    def test_overturn_conversion_supersedes_the_accepted_label_in_store(self):
        rpc = "https://rpc.example.test"
        original = self.run_reconcile([accepted(1, 10)])[0]
        accepted_label = to_label_row(original, rpc=rpc)

        rows = self.run_reconcile([overturned(1, 20)])
        correction_label = to_label_row(rows[1], rpc=rpc, superseded_row=accepted_label)

        self.assertEqual(accepted_label["superseded_by"], correction_label["id"])
        labels.validate(accepted_label)
        labels.validate(correction_label)
        label_store_path = self.rows_path.parent / "labels.jsonl"
        label_store_path.write_text(
            labels.row_line(accepted_label) + "\n" + labels.row_line(correction_label) + "\n",
            encoding="utf-8",
        )
        store = labels.load(label_store_path)
        current = [
            row for row in store.by_subject("subject.synthetic.testnet", "near")
            if row.source["args"] == {"id": 1} and row.superseded_by is None
        ]

        self.assertEqual(len(current), 1)
        self.assertEqual(current[0].label, "overturned report")
        self.assertFalse(any(row.category in ("incident", "denylist") for row in current))
        self.assertEqual(current[0].category, "watch")
        self.assertIn('Report 1 on "reports.synthetic.testnet" was overturned at block 20',
                      correction_label["note"])
        self.assertIn("this row supersedes the accepted row", correction_label["note"])


if __name__ == "__main__":
    unittest.main()
