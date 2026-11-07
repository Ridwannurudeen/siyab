# Incident record, format v1

This is a format specification. The validator, reader and writer are in `intel/records.py` and `python -m intel record-check`; no store or renderer exists, and the two committed records are synthetic. The incident pages that exist today are a different file, described in `docs/INCIDENTS.md`.

An incident record is the off chain account of one event: what happened, what it cost, which accounts agents should not pay because of it, the evidence, the on chain reports behind it, and a timeline whose every timestamp names where it came from. It carries the readable text behind the reason hashes the reports contract stores.

## Content addressing

A record is immutable and named by its content. `record_id` is the sha256 hex of the canonical bytes of the record with its `record_id` field omitted. Those bytes use `canonical_bytes` in `gate/evidence.py`: sorted keys, compact JSON, and UTF-8 encoding, with non ASCII characters unescaped, no trailing newline, and floats refused. This is the same form as the label store's id rule (`docs/LABEL-RECORD.md`) and the evidence documents' hash (`evidence_hash` in `gate/evidence.py`); there is no third canonical form.

The stored file, `<record_id>.json`, is the complete object including `record_id`, serialized again with that same canonical rule. Its sha256 covers the complete object including `record_id`, so the stored file's sha256 is not its `record_id`. A reader recomputes the id from the parsed object after removing `record_id` and refuses a file whose id does not match.

## Fields

A v1 record has exactly these fields.

| Field | Content |
|---|---|
| `version` | the integer 1 |
| `record_id` | the sha256 hex of the canonical bytes of the other fields |
| `kind` | one value of `kind` in `intel/record_enums.json` |
| `outcome` | one value of `outcome` in `intel/record_enums.json` |
| `subjects` | the accounts agents should not pay because of this event: a list of `{chain, account}`, sorted by chain then account, no duplicates; `chain` is `near` or a 1Click chain code as in `docs/LABEL-RECORD.md`. It may be empty when no cited source and no chain read names an account. A subject is never a victim. |
| `evidence` | at least one `{url, content_sha256, fetched_at}`, the shape of a `third_party` source in `docs/LABEL-RECORD.md`; `url` is https and at most 512 bytes |
| `reports` | the on chain reports this record rests on, each a citation (below); an empty list when it rests on none |
| `reasons` | one readable `{report_id, dispute_id, reason_sha256, text}` entry for each moderation or resolved-dispute decision of the cited reports (below) |
| `assertion_ids` | the ids of the label store rows (`intel/labels.jsonl`) this record rests on; an empty list when it rests on none |
| `timeline` | the entries below, with exactly one `recorded` entry, which is the last |
| `supersedes` | null, or the `record_id` of the record this one replaces |
| `publisher` | the NEAR account that publishes the record |

A field outside this list, a missing field, or a value outside the enums is a record a reader refuses.

## Kind and outcome

The two axes are kept apart. `kind` says what the event is: `scam`, `theft`, `phishing`, `exploit`, `key_compromise` or `social`. `outcome` says what it cost: `loss`, `attempt`, `near_miss` or `unknown`. A phishing attempt that was noticed in time is `kind` `phishing`, `outcome` `near_miss`. Both lists are read from `intel/record_enums.json`, the file the reports contract's enums are pinned to (`rust_enum_values_match_the_canonical_json`, `docs/REPORTS.md`), so a record and a report use one vocabulary.

A record does not say what its kind does to a payment. `docs/LADDER.md` decides that from the on chain summary, not from records.

## Citing an on chain report

A citation is `{network, contract, report_id, body_sha256}`: the NEAR network (`testnet` or `mainnet`), the reports contract account, the report id that contract returned from `submit_report`, and the `body_sha256` it stored. A report id is a counter of one contract, so the network and the contract are part of the citation.

To check a citation, a reader calls `get_report(report_id)` on that contract and compares the returned `body_sha256` with the citation's. The body hash is the sha256 of the six field canonical body described in `docs/REPORTS.md` (`fixtures/records/body-vector.json` is its test vector), so a match proves the record cites the report with exactly that subject, kind, outcome, evidence and submitter. A citation whose report is missing, or whose hash differs, is refused.

A record does not copy a report's state. The state changes on chain (pending, accepted, contested and the rest) and a content addressed file cannot follow it, so a reader reads the state from `get_report`, and the record stays valid through every transition.

## Reasons

The reports contract stores only the current `reason_sha256` on each report and keeps no reason text. A record carries one entry per decision of each cited report: the moderation decision with `dispute_id` null, then one entry for each resolved dispute with its `dispute_id`. Each entry's `text` is the reason as written, and its `reason_sha256` is the sha256 of the UTF-8 bytes of that text. A pending or withdrawn report has no decision and carries no reason. Two entries for the same report and decision are refused.

With a report reader, every cited report that has a decision (any state other than `pending` and `withdrawn`; a report can be withdrawn only while pending) must have one reason for its moderation decision and each resolved dispute. A missing or extra decision reason is refused. The reason for the latest decision (the latest resolved dispute, or moderation when none was resolved) must match the report view's current `reason_sha256`. The report view lists dispute ids in append order; while the report is `contested`, its final id is still open, so it is not included among the resolved disputes.

With an event reader, each reason is checked against the `report_moderated` or `dispute_resolved` NEP-297 event emitted by the transaction cited in the matching `moderated` or `dispute_resolved` timeline entry. The reader receives that entry's `network` and `transaction`; its event must carry the same `report_id`, the same `dispute_id` for a resolved dispute, and the same `reason_sha256`. A reason whose event is absent from the timeline's cited transactions is refused. This checks earlier decisions against events when an event reader is supplied. Without readers, reasons are checked for shape, unique report-and-decision pairs, and the hash of their text only.

## The timeline

Each entry is `{at, event, source}`. `at` is a UTC time in the label store's form, `YYYY-MM-DDTHH:MM:SSZ`. Entries are in order of `at`, never decreasing. No time is inferred: an entry exists only when its source states its time.

`event` is one of:

| Event | Meaning |
|---|---|
| `first_chain_signal` | the earliest on chain fact of the event that the publisher read |
| `first_public_report` | the earliest public writing about the event that the publisher found; absent when none was found |
| `report_submitted` | an on chain report about the event was filed |
| `moderated` | a cited report was accepted or rejected |
| `disputed` | a cited report was disputed |
| `dispute_resolved` | a dispute on a cited report was decided |
| `recorded` | the publisher wrote this record; it is a claim about the record, never about when the event happened |

`source` names where `at` comes from, in one of three shapes:

| Source | Shape | Where the time is read |
|---|---|---|
| chain | `{"type": "chain", "network", "block", "transaction"}` | the timestamp of that block; `transaction` is the transaction hash, or null for a fact read at a block with no transaction |
| page | `{"type": "page", "url", "content_sha256"}` | the date the page states, in a snapshot whose sha256 is `content_sha256` |
| record | `{"type": "record"}` | this record itself; allowed only for `recorded` |

`report_submitted`, `moderated`, `disputed` and `dispute_resolved` take a chain source: the transaction that emitted the contract's `report_submitted`, `report_moderated`, `report_disputed` or `dispute_resolved` event.

## Supersession

Nothing is edited. A correction is a new record whose `supersedes` names the `record_id` it replaces, and whose `recorded` entry gives its own time. The old record keeps its bytes and its id. A record is current when no record names it in `supersedes`; a reader that holds two records naming the same one in `supersedes` refuses both.

The label store marks a replaced row with `superseded_by` and leaves that field out of the id (`docs/LABEL-RECORD.md`). A record cannot do that, because its whole content is hashed, so the pointer runs the other way: from the new record to the old.

## An example

Every account, URL and hash below is synthetic. The accounts are placeholders, not claims about any account of that name; the URLs use the reserved `example` domain; each hash is a placeholder in angle brackets, so the example is not a test vector and its `record_id` is not computed.

```json
{
  "version": 1,
  "record_id": "<sha256 of the canonical bytes of the fields below>",
  "kind": "phishing",
  "outcome": "attempt",
  "subjects": [{"chain": "near", "account": "synthetic-subject.testnet"}],
  "evidence": [{"url": "https://evidence.example/synthetic-report", "content_sha256": "<sha256 of the document>", "fetched_at": "2026-11-07T09:00:00Z"}],
  "reports": [{"network": "testnet", "contract": "synthetic-reports.testnet", "report_id": 0, "body_sha256": "<body_sha256 from get_report(0)>"}],
  "reasons": [{"report_id": 0, "dispute_id": null, "reason_sha256": "<sha256 of the text>", "text": "Synthetic example: the evidence shows the subject asking for a key."}],
  "assertion_ids": [],
  "timeline": [
    {"at": "2026-11-06T10:00:00Z", "event": "first_public_report", "source": {"type": "page", "url": "https://evidence.example/synthetic-report", "content_sha256": "<sha256 of the document>"}},
    {"at": "2026-11-06T12:00:00Z", "event": "report_submitted", "source": {"type": "chain", "network": "testnet", "block": 1, "transaction": "<transaction hash>"}},
    {"at": "2026-11-07T08:00:00Z", "event": "moderated", "source": {"type": "chain", "network": "testnet", "block": 2, "transaction": "<transaction hash>"}},
    {"at": "2026-11-07T09:00:00Z", "event": "recorded", "source": {"type": "record"}}
  ],
  "supersedes": null,
  "publisher": "synthetic-publisher.testnet"
}
```

## What this format does not do

It names no person and gives no motive. It does not decide a verdict: `docs/LADDER.md` does, from the contract's `subject_summary`. It does not hold a report's state, which is read from the chain. A change to the format is a version 2 with its own page section, not an edit of v1.
