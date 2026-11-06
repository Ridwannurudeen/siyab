# The incident and attempt record

One object describes one event, whether it arrived as a report from another agent or as our own
reading of the chain. A report does not become a second object: it is the same record in an earlier
state, and every state change is an entry in its timeline.

`intel/record_enums.json` holds the three lists, and nothing else: kinds, outcomes and states.
`intel/records.py` validates it and owns the one place a state becomes a verdict, `fixtures/records/`
holds examples and `tests/test_records.py` holds the rules. `intel/records.py` does not import
`gate.rules` or `gate.verdict`, so the record model cannot depend on the rules that read it.

This document is the spec for the storage and reporting lane, and part of it is already shipped in
the reports contract on `main`: `contracts/reports` holds `subject_summary(subject)`, keeps kinds,
outcomes and states to the values in `intel/record_enums.json` (checked by
`rust_enum_values_match_the_canonical_json`) and hashes the same six-field canonical body into
`body_sha256` (checked by `canonical_body_hash_matches_shared_vector` against
`fixtures/records/body-vector.json`).

That summary already returns five fields, and this lane uses the same five: `accepted`, `pending`,
`contested`, `latest_accepted_id` and `latest_accepted_kind`. `accepted` counts a contested report,
`latest_accepted_id` is the report counter (a `u64`, not the record's hash) and
`latest_accepted_kind` is that report's kind, with both latest fields null when nothing is accepted.
`records.summary` reproduces those five exactly and adds one field, `deny`, which the contract does
not carry yet. So the interface difference between this lane and the contract is the `deny` bit and
nothing else: because of it the ladder below is still ours alone. That bit, and the gate reading it,
are listed under "What the contract lane still has to do" at the end.

## Two axes, not one list

One list holding Scam, Theft, Attempt and Loss mixes what happened with whether it worked. The
record keeps them apart:

- `kind`: what the event is. One of `scam`, `theft`, `phishing`, `exploit`, `key_compromise`, `social`.
- `outcome`: what it cost. One of `loss`, `attempt`, `near_miss`, `unknown`.

A scam that was stopped is `kind: scam`, `outcome: near_miss`. `unknown` is for a report that does
not say, and it never raises the outcome of a record on its own.

## Fields

Every record has exactly these fields, and `record_id` is the sha256 of the compact JSON of the
others with sorted keys.

| Field | Meaning |
|---|---|
| `version` | `"1"`. |
| `record_id` | sha256 hex of the record without this field. The record's own identity, never the `latest_accepted_id` a summary returns. |
| `kind` | The event, from the six above. |
| `outcome` | `loss`, `attempt`, `near_miss` or `unknown`. |
| `state` | `pending`, `accepted`, `rejected`, `withdrawn`, `contested` or `overturned`. |
| `subjects` | Sorted, unique NEAR account ids that agents should not pay. At least one. A subject is never a victim: a victim's account is the account that lost money, and it belongs in the evidence and in the prose, not here or in the six-field report body. |
| `reports` | The reports behind it: `{seq, sha256}`, the contract counter and the sha256 of the canonical report body. Empty when we hold the record alone. |
| `evidence` | At least one `{sha256, url, fetched_at}`. The sha256 is the evidence document's hash, the url is where a reader gets it, and it is https and at most 512 bytes. |
| `moderation` | Decisions behind the current state: `{decision, decided_at, reason_sha256, reason}`. The contract keeps only the hash; the readable text is published here, so it can be corrected or taken down without touching the chain. |
| `timeline` | Append-only entries of `{event, at, source}`. `at` never decreases. |
| `supersedes` | null, or the `record_id` this record replaces. |
| `publisher` | The account that asserted it. |
| `observed` | `{chain, first_block}`: `near` plus the height of the first chain signal, or null when the record rests on a page only. |

### Timeline entries

`event` is one of `first_chain_signal`, `first_public_report`, `our_record`, `report_received`,
`moderation`, `status_change`, `superseded`, `closed`.

Every entry carries its `source`: `{type: chain, ref: <block height>}`, `{type: page, ref: <https url>}`,
`{type: report, ref: <body sha256>}`, `{type: moderation, ref: <decision id>}` or
`{type: internal, ref: <note id>}`.

No timestamp is inferred. `first_public_report` means the earliest public report we found, and its
url is in the same entry; if we found none, the entry is absent, not guessed.

### Corrections

Nothing is edited and no state is rewritten. A correction is a new record whose `supersedes` names
the one it replaces, with a `superseded` entry in its timeline. The old bytes, their hash and their
state stay readable forever, and the store marks the old record superseded so it is read as history
rather than as current.

## States and what a verdict may do

| State | Verdict the rules may return |
|---|---|
| `pending` | `UNKNOWN` only. Never a DENY, whatever the severity. |
| `accepted` | `DENY` for `scam`, `theft`, `phishing`, `exploit` and `key_compromise`, citing the report seq, the report hash and the record id. `UNKNOWN` for `social`. |
| `contested` | The answer it had when it was accepted: `DENY` for the same five kinds, which the allowlist does not lift. A record disputed from `rejected` had no DENY to keep, so it asks. Only a moderator's `overturned` removes a DENY. |
| `rejected`, `withdrawn`, `overturned` | Nothing: the record is not a label any more. |

A `social` report is an ask rather than a block because nobody exploited code: a human was
persuaded, and the next action is a conversation with the payee, not a frozen vendor. That is the
only kind where an accepted report stops short of DENY, and `DENY_KINDS` in `intel/records.py` is
the list, so a new kind cannot start denying by being added to the vocabulary file alone.

A disputed report keeps its DENY. If a subject could lift an accepted report by filing a dispute,
the dispute would be the cheapest way to switch a label off: a contested report that was accepted
keeps denying, the allowlist does not except it, and only a moderator's `overturned` removes it. A
dispute preserves an answer and never creates one, so `rejected` to `contested` is a request to
review the rejection: a rejection held no DENY, and that record asks while the appeal runs.

One ladder runs over a subject with several records: DENY beats UNKNOWN, which beats no effect. The
summary carries one `deny` bit, so the ladder reads every accepted record, and every contested one
that was accepted first, rather than only the latest: an accepted exploit record followed by an
accepted social report still denies. An unreadable read is UNKNOWN, which sits above no effect and
below DENY, and the allowlist never forgives it.

Transitions: `pending` to `accepted`, `rejected` or `withdrawn`; `accepted` to `contested`;
`rejected` to `contested`; `contested` to `accepted`, `rejected` or `overturned`; `withdrawn` and
`overturned` are terminal. A dispute is filed by moving an `accepted` or `rejected` record to
`contested`, and `overturned` is the outcome that says the original decision was wrong.

## The read the payment check makes

The payment path does not reread every record. It reads one bounded summary for the subject, and
`verdict_for` takes that summary and the allowlist flag. The summary the contract returns is
`subject_summary(subject)` with `{accepted, pending, contested, latest_accepted_id,
latest_accepted_kind}`, where `accepted` counts a contested report and the two latest fields are the
report counter and kind of the most recent accepted report, both null when there is none. This lane
returns those five fields plus `deny`, which is true when an accepted record holds one of the five
`DENY_KINDS`, or a contested record held one when it was accepted. `records.summary` builds that
shape from the records held off chain, so the replay and the site agree with the contract rather
than inventing a second ordering, and the single extra field is the one the contract still has to
add.

A summary whose `latest_accepted_id` and `latest_accepted_kind` disagree, one being null while the
other is set, cannot have come from the contract and reads `UNKNOWN`.

`deny` is one bit because counts alone cannot answer the ladder: a subject whose latest accepted
record is `social` would read as an ask while an earlier accepted `exploit` vanished. When the
contract carries the bit it will set it when it accepts a report of a DENY kind, or contests one it
had accepted, and it already refuses a report whose kind is outside `intel/record_enums.json` at
accept time, so a stored record always has a known kind and `verdict_for` never needs a second call
to learn it.

### The allowlist, and why it is here

An UNKNOWN costs a human an answer, so anyone with a storage deposit could file a report and stall
payments to a vendor the owner already trusts. So `verdict_for` gives an ask no effect when the
payee is on the owner's allowlist, in the two cases where nothing is in dispute: a `pending` record,
and an accepted `social` report. A DENY from an `accepted` record still applies, because an
allowlisted account can be compromised or named in an incident, and that is exactly when the label
has to win.

A `contested` record is the exception, and it is deliberate: it asks `UNKNOWN` even on an
allowlisted payee. A dispute is an open moderation question, whether it was filed against an
accepted record or against a rejected one, so a person answers it. Letting the allowlist silence a
contested record would mean a filing, or an appeal against a rejection, could switch a question off
without anyone deciding it. Only a moderator's `accepted`, `rejected` or `overturned` closes it, and
`tests/test_records.py` pins both the contested-from-accepted and the contested-from-rejected cases
with the allowlist set.

A read that fails, a summary that does not match the six fields, a negative count, a `deny` bit
that is not a boolean or that is true with no accepted or contested record, a
`latest_accepted_kind` outside the vocabulary, or a `latest_accepted_id` that is unreadable or that
disagrees with the `accepted` count all read `UNKNOWN`, and the allowlist does not forgive them. The
count check is not pedantry: a summary
with `accepted` 1 and no id, or an id with no accepted record, would otherwise fall through the
no-effect branch and pass an allowlisted payee. A pending record that can deny is a censorship
tool with a storage deposit; `verdict_for` is the only place the mapping lives. Where the desk pages
exist they call it, and the tests call it with hand-built rows; no payment or signing path is built,
and when one is it must call this function rather than keep a second copy of the ladder.

## What a record is not

It is not a guarantee that a subject is criminal, and it is not attribution. A record states an
event, its outcome, its evidence, its state and who asserted it. Publisher statistics are counts of
reports and outcomes, never an accuracy rate. Response times and an appeal are outside this model:
the reporting lane sets them out, and the pages lane renders a record's state beside every subject.

## The report body behind body_sha256

A report is hashed from exactly six fields, and the contract test and the Python test must build the
same bytes from the same file:

    {evidence_sha256, evidence_url, kind, outcome, subject, submitter}

Keys sorted, compact JSON, UTF-8, no nulls, no trailing newline. `subject` is the single account
agents should not pay, and `submitter` is the account that filed it. `evidence_url` is https and at
most 512 bytes, measured in bytes rather than characters so a multibyte url cannot pass a 512
character check and exceed the limit; the report contract stores the hash and the url, and the record
adds `fetched_at` off chain. `fixtures/records/report-body-vector.json` holds one body, its exact
canonical bytes and its sha256, and `tests/test_records.py` checks it. The reports contract has its
own vector in `fixtures/records/body-vector.json`; `test_the_contract_vector_hashes_the_same_here_as_in_rust`
hashes the Rust vector in this module too, so the two implementations are pinned to the same value
and a change on either side breaks both.

## What the contract lane still has to do

The vocabulary, the canonical body and the counts in `subject_summary` are merged in
`contracts/reports`. What is not:

- a `deny` bit on `subject_summary`, set when it accepts a report of one of the five `DENY_KINDS`
  and when it contests a record it had accepted as one, and cleared only by an `overturned`,
- and the `Gate` reading that bit, so a payment to a subject with an accepted deny record refuses on
  chain rather than only in this repository's rules.

Until both are merged, the ladder in `intel/records.py` is the only place a record becomes a
verdict, and `contracts/gate` still decides payments without any report knowledge.
