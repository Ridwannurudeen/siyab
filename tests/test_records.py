import contextlib
import copy
import hashlib
import io
import json
import tempfile
import unittest
from pathlib import Path

from gate.evidence import canonical_bytes
from intel import __main__ as intel_main
from intel import records

ROOT = Path(__file__).resolve().parent.parent
RECORDS_DIR = ROOT / "fixtures" / "records"
SYNTHETIC_URL = "https://github.com/Ridwannurudeen/shieldbot-near/blob/main/docs/demo/synthetic-report.md"


def identified(document):
    document = copy.deepcopy(document)
    document.pop("record_id", None)
    document["record_id"] = records.canonical_record_id(document)
    return document


def base_record(**changes):
    document = {
        "version": 1,
        "kind": "social",
        "outcome": "unknown",
        "subjects": [{"chain": "near", "account": "kestrelridge-mallory.testnet"}],
        "evidence": [{
            "url": SYNTHETIC_URL,
            "content_sha256": "a" * 64,
            "fetched_at": "2026-11-07T05:12:01Z",
        }],
        "reports": [],
        "reasons": [],
        "assertion_ids": [],
        "timeline": [{
            "at": "2026-11-07T05:12:02Z",
            "event": "recorded",
            "source": {"type": "record"},
        }],
        "supersedes": None,
        "publisher": "kestrelridge-owner.testnet",
    }
    document.update(changes)
    return identified(document)


def reason_entry(report_id, dispute_id, text):
    return {
        "report_id": report_id,
        "dispute_id": dispute_id,
        "reason_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
        "text": text,
    }


def reports_event(event, report_id, reason_sha256, dispute_id=None):
    payload = {
        "report_id": report_id,
        "subject": "kestrelridge-mallory.testnet",
        "state": "accepted",
        "body_sha256": "b" * 64,
        "moderator": "kestrelridge-owner.testnet",
        "reason_sha256": reason_sha256,
    }
    if event == "dispute_resolved":
        payload["dispute_id"] = dispute_id
    return {
        "standard": "shieldbot-near",
        "version": "1.0.0",
        "event": event,
        "data": [payload],
    }


def timeline_decision(event, transaction):
    return {
        "at": "2026-11-07T05:12:01Z",
        "event": event,
        "source": {
            "type": "chain",
            "network": "testnet",
            "block": 2,
            "transaction": transaction,
        },
    }


class RecordValidationTest(unittest.TestCase):
    def rejects(self, document, message=None):
        with self.assertRaises(records.RecordError) as caught:
            records.validate(identified(document))
        if message is not None:
            self.assertIn(message, str(caught.exception))

    def test_record_has_exact_field_set(self):
        document = base_record()
        del document["publisher"]
        with self.assertRaisesRegex(records.RecordError, "exactly the fields"):
            records.validate(identified(document))
        document = base_record()
        document["extra"] = "value"
        with self.assertRaisesRegex(records.RecordError, "exactly the fields"):
            records.validate(identified(document))

    def test_version_must_be_integer_one(self):
        for version in (2, "1", True):
            with self.subTest(version=version):
                document = base_record()
                document["version"] = version
                self.rejects(document, "version: integer 1 is required")

    def test_record_id_must_match_canonical_content(self):
        document = base_record()
        document["record_id"] = "0" * 64
        with self.assertRaisesRegex(records.RecordError, "record_id: expected"):
            records.validate(document)

    def test_kind_and_outcome_come_from_record_enums(self):
        for field, value in (("kind", "fraud"), ("outcome", "blocked")):
            with self.subTest(field=field):
                document = base_record()
                document[field] = value
                self.rejects(document, f"{field}:")

    def test_subject_entries_have_exact_shape_and_valid_chain_codes(self):
        for subject in ({"chain": "near", "account": "kestrelridge-mallory.testnet", "extra": 1},
                        {"chain": "Near", "account": "kestrelridge-mallory.testnet"},
                        {"chain": "eth-mainnet", "account": "0x123"}):
            with self.subTest(subject=subject):
                document = base_record(subjects=[subject])
                self.rejects(document, "subjects[0]")

    def test_subjects_must_be_sorted_by_chain_then_account(self):
        document = base_record(subjects=[{"chain": "near", "account": "kestrelridge-mallory.testnet"},
                                         {"chain": "eth", "account": "0x123"}])
        self.rejects(document, "sorted by chain then account")

    def test_subjects_must_not_repeat_a_chain_and_account_pair(self):
        subject = {"chain": "near", "account": "kestrelridge-mallory.testnet"}
        self.rejects(base_record(subjects=[subject, subject]), "duplicate subject")

    def test_evidence_requires_at_least_one_source(self):
        self.rejects(base_record(evidence=[]), "evidence: at least one source is required")

    def test_evidence_source_has_exact_shape(self):
        source = {"url": SYNTHETIC_URL, "content_sha256": "a" * 64, "fetched_at": "2026-11-07T05:12:01Z",
                  "note": "synthetic"}
        self.rejects(base_record(evidence=[source]), "exactly the fields")

    def test_evidence_url_must_be_https(self):
        evidence = copy.deepcopy(base_record()["evidence"])
        evidence[0]["url"] = "http://example.test/report"
        self.rejects(base_record(evidence=evidence), "evidence[0].url: an https URL is required")

    def test_evidence_url_requires_a_parseable_non_empty_hostname(self):
        for url, message in (("https://", "hostname"), ("https://@", "hostname"),
                             ("https://[", "could not be parsed")):
            with self.subTest(url=url):
                evidence = copy.deepcopy(base_record()["evidence"])
                evidence[0]["url"] = url
                self.rejects(base_record(evidence=evidence), message)

    def test_evidence_url_must_fit_in_512_utf8_bytes(self):
        evidence = copy.deepcopy(base_record()["evidence"])
        evidence[0]["url"] = "https://" + "x" * 505
        self.rejects(base_record(evidence=evidence), "at most 512 bytes")

    def test_evidence_hash_and_fetch_time_must_be_valid(self):
        evidence = copy.deepcopy(base_record()["evidence"])
        evidence[0]["content_sha256"] = "A" * 64
        self.rejects(base_record(evidence=evidence), "content_sha256: 64 lowercase hex")
        evidence = copy.deepcopy(base_record()["evidence"])
        evidence[0]["fetched_at"] = "2026-02-30T05:12:01Z"
        self.rejects(base_record(evidence=evidence), "valid UTC calendar time")

    def test_report_citation_shape_is_checked_without_a_reader(self):
        citation = {"network": "testnet", "contract": "reports.kestrelridge-main.testnet", "report_id": 0,
                    "body_sha256": "b" * 64}
        records.validate(base_record(reports=[citation]))
        for changes, message in (({"network": "devnet"}, ".network:"), ({"report_id": True}, ".report_id:"),
                                 ({"body_sha256": "B" * 64}, ".body_sha256:"),
                                 ({"contract": "not an account"}, ".contract:")):
            with self.subTest(changes=changes):
                bad = dict(citation, **changes)
                self.rejects(base_record(reports=[bad]), message)

    def test_report_reader_is_called_with_citation_identity_and_checks_hash(self):
        citation = {"network": "testnet", "contract": "reports.kestrelridge-main.testnet", "report_id": 3,
                    "body_sha256": "b" * 64}
        calls = []

        def reader(*args):
            calls.append(args)
            return {"body_sha256": "b" * 64, "state": "pending", "reason_sha256": None,
                    "dispute_ids": []}

        records.validate(base_record(reports=[citation]), report_reader=reader)
        self.assertEqual(calls, [("testnet", "reports.kestrelridge-main.testnet", 3)])
        with self.assertRaisesRegex(records.RecordError, "does not match report 3"):
            records.validate(base_record(reports=[citation]),
                             report_reader=lambda *args: {"body_sha256": "c" * 64, "state": "pending",
                                                          "reason_sha256": None, "dispute_ids": []})
        with self.assertRaisesRegex(records.RecordError, "report 3 is missing"):
            records.validate(base_record(reports=[citation]), report_reader=lambda *args: None)

    def test_reasons_must_cite_a_report_and_have_a_matching_utf8_hash(self):
        text = "Synthetic review reason: caf?."
        reason = {"report_id": 0, "dispute_id": None,
                  "reason_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(), "text": text}
        citation = {"network": "testnet", "contract": "reports.kestrelridge-main.testnet", "report_id": 0,
                    "body_sha256": "b" * 64}
        records.validate(base_record(reports=[citation], reasons=[reason]))
        self.rejects(base_record(reasons=[reason]), "does not cite a report")
        self.rejects(base_record(reports=[citation], reasons=[dict(reason, reason_sha256="c" * 64)]),
                     "reason_sha256: does not match")
        self.rejects(base_record(reports=[citation], reasons=[dict(reason, dispute_id=True)]),
                     "dispute_id: null or")

    def test_report_reader_requires_each_decision_reason_and_rejects_duplicates(self):
        text = "Synthetic current moderation reason."
        reason = reason_entry(0, None, text)
        citation = {"network": "testnet", "contract": "reports.kestrelridge-main.testnet", "report_id": 0,
                    "body_sha256": "b" * 64}
        report = {"body_sha256": "b" * 64, "state": "accepted", "reason_sha256": reason["reason_sha256"],
                  "dispute_ids": []}
        with self.assertRaisesRegex(records.RecordError, "exactly one reason is required for decision"):
            records.validate(base_record(reports=[citation]), report_reader=lambda *args: report)
        with self.assertRaisesRegex(records.RecordError, "more than one reason for decision"):
            records.validate(base_record(reports=[citation], reasons=[reason, reason]),
                             report_reader=lambda *args: report)

    def test_report_reader_needs_no_reason_for_an_undecided_report_and_refuses_one(self):
        text = "Synthetic reason for an undecided report."
        reason_hash = hashlib.sha256(text.encode("utf-8")).hexdigest()
        citation = {"network": "testnet", "contract": "reports.kestrelridge-main.testnet", "report_id": 0,
                    "body_sha256": "b" * 64}
        reason = {"report_id": 0, "dispute_id": None, "reason_sha256": reason_hash, "text": text}
        for state in ("pending", "withdrawn"):
            with self.subTest(state=state):
                report = {"body_sha256": "b" * 64, "state": state, "reason_sha256": None, "dispute_ids": []}
                records.validate(base_record(reports=[citation]), report_reader=lambda *args: report)
                with self.assertRaisesRegex(records.RecordError, "has no decision"):
                    records.validate(base_record(reports=[citation], reasons=[reason]),
                                     report_reader=lambda *args: report)

    def test_report_reader_checks_the_current_reason_after_earlier_decisions(self):
        moderation = reason_entry(0, None, "Synthetic earlier moderation reason.")
        resolution = reason_entry(0, 4, "Synthetic later resolution reason.")
        citation = {"network": "testnet", "contract": "reports.kestrelridge-main.testnet", "report_id": 0,
                    "body_sha256": "b" * 64}
        report = {"body_sha256": "b" * 64, "state": "accepted",
                  "reason_sha256": resolution["reason_sha256"], "dispute_ids": [4]}
        document = base_record(reports=[citation], reasons=[moderation, resolution])
        records.validate(document, report_reader=lambda *args: report)
        wrong_latest = dict(resolution, reason_sha256=moderation["reason_sha256"])
        wrong_latest["text"] = moderation["text"]
        with self.assertRaisesRegex(records.RecordError, "does not match report 0"):
            records.validate(base_record(reports=[citation], reasons=[moderation, wrong_latest]),
                             report_reader=lambda *args: report)

    def test_report_reader_accepts_the_current_reason_after_resolution(self):
        moderation = reason_entry(0, None, "Synthetic moderation reason.")
        resolution = reason_entry(0, 4, "Synthetic current resolution reason.")
        citation = {"network": "testnet", "contract": "reports.kestrelridge-main.testnet", "report_id": 0,
                    "body_sha256": "b" * 64}
        report = {"body_sha256": "b" * 64, "state": "accepted",
                  "reason_sha256": resolution["reason_sha256"], "dispute_ids": [4]}
        records.validate(base_record(reports=[citation], reasons=[moderation, resolution]),
                         report_reader=lambda *args: report)

    def test_report_reader_checks_every_resolved_dispute_id(self):
        citation = {"network": "testnet", "contract": "reports.kestrelridge-main.testnet", "report_id": 0,
                    "body_sha256": "b" * 64}
        cases = (
            ("accepted", [], [None]),
            ("contested", [4], [None]),
            ("contested", [2, 4], [None, 2]),
            ("accepted", [2, 4], [None, 2, 4]),
        )
        for state, dispute_ids, expected_decisions in cases:
            with self.subTest(state=state, dispute_ids=dispute_ids):
                reasons = [reason_entry(0, dispute_id, f"Synthetic decision reason {dispute_id}.")
                           for dispute_id in expected_decisions]
                report = {"body_sha256": "b" * 64, "state": state,
                          "reason_sha256": reasons[-1]["reason_sha256"], "dispute_ids": dispute_ids}
                records.validate(base_record(reports=[citation], reasons=reasons),
                                 report_reader=lambda *args: report)
                wrong_reasons = copy.deepcopy(reasons)
                wrong_reasons[-1]["dispute_id"] = 99
                with self.assertRaisesRegex(records.RecordError, "exactly one reason is required for decision"):
                    records.validate(base_record(reports=[citation], reasons=wrong_reasons),
                                     report_reader=lambda *args: report)

    def test_event_reader_requires_a_reason_for_each_timeline_decision(self):
        text = "Synthetic moderation decision."
        reason_hash = hashlib.sha256(text.encode("utf-8")).hexdigest()
        citation = {"network": "testnet", "contract": "reports.kestrelridge-main.testnet", "report_id": 0,
                    "body_sha256": "b" * 64}
        timeline = [timeline_decision("moderated", "synthetic-moderation-tx"), base_record()["timeline"][0]]
        events = [reports_event("report_moderated", 0, reason_hash)]
        with self.assertRaisesRegex(records.RecordError, "has no reason for decision"):
            records.validate(base_record(reports=[citation], timeline=timeline),
                             event_reader=lambda network, transaction: events)

    def test_event_reader_rejects_two_reasons_for_one_decision(self):
        text = "Synthetic duplicate moderation reason."
        reason = reason_entry(0, None, text)
        citation = {"network": "testnet", "contract": "reports.kestrelridge-main.testnet", "report_id": 0,
                    "body_sha256": "b" * 64}
        timeline = [timeline_decision("moderated", "synthetic-moderation-tx"), base_record()["timeline"][0]]
        with self.assertRaisesRegex(records.RecordError, "more than one reason for decision"):
            records.validate(base_record(reports=[citation], reasons=[reason, reason], timeline=timeline),
                             event_reader=lambda network, transaction: [
                                 reports_event("report_moderated", 0, reason["reason_sha256"])
                             ])

    def test_event_reader_requires_each_reason_to_be_found_through_the_timeline(self):
        citation = {"network": "testnet", "contract": "reports.kestrelridge-main.testnet", "report_id": 0,
                    "body_sha256": "b" * 64}
        reason = reason_entry(0, None, "Synthetic unlinked moderation reason.")
        with self.assertRaisesRegex(records.RecordError, "event cannot be found through the timeline"):
            records.validate(base_record(reports=[citation], reasons=[reason]),
                             event_reader=lambda network, transaction: self.fail("reader should not be called"))

    def test_event_reader_compares_reason_hash_with_the_event(self):
        citation = {"network": "testnet", "contract": "reports.kestrelridge-main.testnet", "report_id": 0,
                    "body_sha256": "b" * 64}
        reason = reason_entry(0, None, "Synthetic moderation reason.")
        timeline = [timeline_decision("moderated", "synthetic-moderation-tx"), base_record()["timeline"][0]]
        with self.assertRaisesRegex(records.RecordError, "does not match event"):
            records.validate(base_record(reports=[citation], reasons=[reason], timeline=timeline),
                             event_reader=lambda network, transaction: [
                                 reports_event("report_moderated", 0, "c" * 64)
                             ])

    def test_event_reader_requires_event_report_and_dispute_ids_to_match(self):
        citation = {"network": "testnet", "contract": "reports.kestrelridge-main.testnet", "report_id": 0,
                    "body_sha256": "b" * 64}
        moderation = reason_entry(0, None, "Synthetic moderation reason.")
        resolution = reason_entry(0, 4, "Synthetic dispute reason.")
        moderation_timeline = [timeline_decision("moderated", "synthetic-moderation-tx"),
                               base_record()["timeline"][0]]
        with self.assertRaisesRegex(records.RecordError, "event report_id 1 does not match reason report_id 0"):
            records.validate(base_record(reports=[citation], reasons=[moderation], timeline=moderation_timeline),
                             event_reader=lambda network, transaction: [
                                 reports_event("report_moderated", 1, moderation["reason_sha256"])
                             ])
        resolution_timeline = [timeline_decision("dispute_resolved", "synthetic-resolution-tx"),
                               base_record()["timeline"][0]]
        with self.assertRaisesRegex(records.RecordError, "event dispute_id 5 does not match reason dispute_id 4"):
            records.validate(base_record(reports=[citation], reasons=[resolution], timeline=resolution_timeline),
                             event_reader=lambda network, transaction: [
                                 reports_event("dispute_resolved", 0, resolution["reason_sha256"], 5)
                             ])

    def test_event_reader_checks_earlier_decisions_through_their_timeline_transactions(self):
        moderation = reason_entry(0, None, "Synthetic moderation reason.")
        resolution = reason_entry(0, 4, "Synthetic resolution reason.")
        citation = {"network": "testnet", "contract": "reports.kestrelridge-main.testnet", "report_id": 0,
                    "body_sha256": "b" * 64}
        timeline = [
            timeline_decision("moderated", "synthetic-moderation-tx"),
            dict(timeline_decision("dispute_resolved", "synthetic-resolution-tx"), at="2026-11-07T05:12:02Z"),
            base_record()["timeline"][0],
        ]
        events_by_transaction = {
            "synthetic-moderation-tx": [reports_event("report_moderated", 0, moderation["reason_sha256"])],
            "synthetic-resolution-tx": [reports_event("dispute_resolved", 0, resolution["reason_sha256"], 4)],
        }
        calls = []

        def event_reader(network, transaction):
            calls.append((network, transaction))
            return events_by_transaction[transaction]

        report = {"body_sha256": "b" * 64, "state": "accepted",
                  "reason_sha256": resolution["reason_sha256"], "dispute_ids": [4]}
        records.validate(base_record(reports=[citation], reasons=[moderation, resolution], timeline=timeline),
                         report_reader=lambda *args: report, event_reader=event_reader)
        self.assertEqual(calls, [("testnet", "synthetic-moderation-tx"),
                                 ("testnet", "synthetic-resolution-tx")])

    def test_assertion_ids_must_be_sha256_ids(self):
        self.rejects(base_record(assertion_ids=["not-an-id"]), "assertion_ids[0]")

    def test_timeline_utc_form_and_calendar_date_are_required(self):
        timeline = copy.deepcopy(base_record()["timeline"])
        timeline[0]["at"] = "2026-11-07T05:12:02+00:00"
        self.rejects(base_record(timeline=timeline), "UTC time YYYY-MM-DDTHH:MM:SSZ")
        timeline[0]["at"] = "2026-02-30T05:12:02Z"
        self.rejects(base_record(timeline=timeline), "valid UTC calendar time")

    def test_timeline_times_must_not_decrease(self):
        timeline = [
            {"at": "2026-11-07T05:12:03Z", "event": "first_public_report",
             "source": {"type": "page", "url": SYNTHETIC_URL, "content_sha256": "a" * 64}},
            {"at": "2026-11-07T05:12:02Z", "event": "recorded", "source": {"type": "record"}},
        ]
        self.rejects(base_record(timeline=timeline), "non decreasing")

    def test_timeline_has_exactly_one_recorded_event_and_it_is_last(self):
        self.rejects(base_record(timeline=[]), "exactly one recorded event")
        timeline = copy.deepcopy(base_record()["timeline"])
        timeline.append({"at": "2026-11-07T05:12:03Z", "event": "recorded", "source": {"type": "record"}})
        self.rejects(base_record(timeline=timeline), "exactly one recorded event")
        timeline = [
            {"at": "2026-11-07T05:12:02Z", "event": "recorded", "source": {"type": "record"}},
            {"at": "2026-11-07T05:12:03Z", "event": "first_public_report",
             "source": {"type": "page", "url": SYNTHETIC_URL, "content_sha256": "a" * 64}},
        ]
        self.rejects(base_record(timeline=timeline), "recorded event must be last")

    def test_timeline_event_must_be_allowed(self):
        timeline = copy.deepcopy(base_record()["timeline"])
        timeline[0]["event"] = "updated"
        self.rejects(base_record(timeline=timeline), "not an allowed timeline event")
        timeline[0]["event"] = ["recorded"]
        self.rejects(base_record(timeline=timeline), "not an allowed timeline event")

    def test_timeline_source_shapes_and_event_types_are_enforced(self):
        timeline = copy.deepcopy(base_record()["timeline"])
        timeline[0]["source"]["extra"] = True
        self.rejects(base_record(timeline=timeline), "exactly the fields")
        timeline = copy.deepcopy(base_record()["timeline"])
        timeline[0]["event"] = "first_chain_signal"
        self.rejects(base_record(timeline=timeline), "record source is allowed only for recorded")
        timeline = [
            {"at": "2026-11-07T05:12:01Z", "event": "first_chain_signal",
             "source": {"type": "chain", "network": "testnet", "block": 1, "transaction": None}},
            {"at": "2026-11-07T05:12:02Z", "event": "recorded", "source": {"type": "record"}},
        ]
        records.validate(base_record(timeline=timeline))
        timeline[0]["source"]["network"] = "localnet"
        self.rejects(base_record(timeline=timeline), "network: testnet or mainnet")

    def test_timeline_report_events_require_chain_sources_and_public_reports_require_pages(self):
        for event in ("report_submitted", "moderated", "disputed", "dispute_resolved"):
            with self.subTest(event=event):
                timeline = [
                    {"at": "2026-11-07T05:12:01Z", "event": event,
                     "source": {"type": "chain", "network": "testnet", "block": 2,
                                "transaction": "synthetic-transaction-hash"}},
                    {"at": "2026-11-07T05:12:02Z", "event": "recorded", "source": {"type": "record"}},
                ]
                records.validate(base_record(timeline=timeline))
        timeline = [
            {"at": "2026-11-07T05:12:01Z", "event": "report_submitted",
             "source": {"type": "page", "url": SYNTHETIC_URL, "content_sha256": "a" * 64}},
            {"at": "2026-11-07T05:12:02Z", "event": "recorded", "source": {"type": "record"}},
        ]
        self.rejects(base_record(timeline=timeline), "page source is not allowed for report_submitted")
        timeline[0]["event"] = "first_public_report"
        records.validate(base_record(timeline=timeline))

    def test_supersedes_is_null_or_64_lowercase_hex(self):
        records.validate(base_record(supersedes="a" * 64))
        self.rejects(base_record(supersedes="A" * 64), "supersedes: null or 64 hex")
        self.rejects(base_record(supersedes="g" * 64), "supersedes: null or 64 hex")

    def test_publisher_must_be_a_near_account(self):
        self.rejects(base_record(publisher="INVALID ACCOUNT"), "publisher: not a valid NEAR account id")

    def test_canonical_record_id_uses_evidence_canonical_bytes_and_rejects_floats(self):
        document = base_record()
        body = {key: value for key, value in document.items() if key != "record_id"}
        expected = hashlib.sha256(canonical_bytes(body)).hexdigest()
        self.assertEqual(records.canonical_record_id(document), expected)
        self.assertEqual(expected, document["record_id"])
        with self.assertRaises(records.RecordError):
            records.canonical_record_id({"value": 1.5})


class RecordFilesAndCliTest(unittest.TestCase):
    def test_write_fills_id_and_read_round_trips_canonical_bytes(self):
        with tempfile.TemporaryDirectory() as directory:
            source = base_record()
            source.pop("record_id")
            path = records.write(source, directory)
            data = path.read_bytes()
            self.assertEqual(path.name, f"{records.canonical_record_id(source)}.json")
            self.assertEqual(data, records.canonical_bytes(json.loads(data)))
            self.assertEqual(records.read(path), json.loads(data))
            self.assertEqual(records.write(source, directory), path)

    def test_read_refuses_pretty_printed_json(self):
        with tempfile.TemporaryDirectory() as directory:
            path = records.write(base_record(), directory)
            path.write_text(json.dumps(json.loads(path.read_text(encoding="utf-8")), indent=2), encoding="utf-8")
            with self.assertRaisesRegex(records.RecordError, "canonical_bytes"):
                records.read(path)

    def test_read_refuses_duplicate_json_object_keys(self):
        with tempfile.TemporaryDirectory() as directory:
            path = records.write(base_record(), directory)
            path.write_text('{"version":1,"version":1}', encoding="utf-8")
            with self.assertRaisesRegex(records.RecordError, "duplicate JSON object key"):
                records.read(path)

    def test_write_refuses_to_overwrite_a_different_file_with_same_name(self):
        with tempfile.TemporaryDirectory() as directory:
            source = base_record()
            path = records.write(source, directory)
            path.write_bytes(b"different file")
            with self.assertRaisesRegex(records.RecordError, "refuses to overwrite a different file"):
                records.write(source, directory)

    def test_read_refuses_a_filename_that_is_not_the_record_id(self):
        with tempfile.TemporaryDirectory() as directory:
            path = records.write(base_record(), directory)
            wrong_path = path.with_name("wrong.json")
            wrong_path.write_bytes(path.read_bytes())
            with self.assertRaisesRegex(records.RecordError, "filename must be"):
                records.read(wrong_path)

    def test_both_synthetic_fixtures_pass_record_check(self):
        paths = sorted(path for path in RECORDS_DIR.glob("*.json") if path.name != "body-vector.json")
        self.assertEqual(len(paths), 2)
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            result = intel_main.main(["record-check", *(str(path) for path in paths)])
        self.assertEqual(result, 0)
        self.assertEqual(len(output.getvalue().splitlines()), 2)
        documents = []
        for path in paths:
            self.assertIn(f"valid {path}", output.getvalue())
            record = records.read(path)
            self.assertEqual(record["subjects"], [{"chain": "near", "account": "kestrelridge-mallory.testnet"}])
            self.assertIn("synthetic-report.md", record["evidence"][0]["url"])
            self.assertIn("synthetic", path.read_text(encoding="utf-8").lower())
            documents.append(record)
        superseding = [record for record in documents if record["supersedes"] is not None]
        self.assertEqual(len(superseding), 1)
        self.assertIn(superseding[0]["supersedes"], {record["record_id"] for record in documents})

    def test_record_check_prints_each_refusal_and_returns_one(self):
        with tempfile.TemporaryDirectory() as directory:
            invalid = Path(directory) / "invalid.json"
            invalid.write_text("{}", encoding="utf-8")
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                result = intel_main.main(["record-check", str(invalid)])
            self.assertEqual(result, 1)
            self.assertEqual(len(output.getvalue().splitlines()), 1)
            self.assertIn("refused", output.getvalue())


if __name__ == "__main__":
    unittest.main()
