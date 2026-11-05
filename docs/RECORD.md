# The incident and attempt record

One object describes one event, whether it arrived as a report from another agent or as our own
reading of the chain. A report does not become a second object: it is the same record in an earlier
state, and every state change is an entry in its timeline.

`intel/record_enums.json` holds the three lists, and nothing else: kinds, outcomes and states.
`intel/records.py` validates it and owns the one place a state becomes a verdict, `fixtures/records/`
holds examples and `tests/test_records.py` holds the rules. `intel/records.py` does not import
`gate.rules` or `gate.verdict`, so the record model cannot depend on the rules that read it.

This document is the spec for the contract lane, not a description of it. Nothing in this branch
changes `contracts/gate`: there is no `subject_summary`, no `record_enums.json` read and no Rust
test over the report body vector yet. Those are the contract change that follows this one, listed
under "What the contract lane still has to do" at the end. Until they land, the Python model and
the examples here are the only implementation.

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
| `record_id` | sha256 hex of the record without this field. |
| `kind` | The event, from the six above. |
| `outcome` | `loss`, `attempt`, `near_miss` or `unknown`. |
| `state` | `pending`, `accepted`, `rejected`, `withdrawn`, `contested` or `overturned`. |
| `subjects` | Sorted, unique NEAR account ids that agents should not pay. At least one. A subject is never a victim: a victim's account is the account that lost money, and it belongs in a report's body, not here. |
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
`verdict_for` takes that summary and the allowlist flag. The summary the contract must expose is
`subject_summary(subject)` returning `{accepted, pending, contested, latest_accepted_id, deny}`,
where `deny` is true when an accepted record holds one of the five `DENY_KINDS`, or a contested
record held one when it was accepted. `records.summary` builds the same shape from the records held
off chain, so the replay and the site agree with the contract rather than inventing a second
ordering.

`deny` is one bit because counts alone cannot answer the ladder: a subject whose latest accepted
record is `social` would read as an ask while an earlier accepted `exploit` vanished. The contract
sets the bit when it accepts a report of a DENY kind, or contests one it had accepted, and it
refuses a report whose kind is outside `intel/record_enums.json` at accept time, so a stored record
always has a known kind and `verdict_for` never needs a second call to learn it.

### The allowlist, and why it is here

An UNKNOWN costs a human an answer, so anyone with a storage deposit could file a report and stall
payments to a vendor the owner already trusts. So `verdict_for` gives an UNKNOWN no effect when the
payee is on the owner's allowlist. A DENY from an `accepted` record still applies, because an
allowlisted account can be compromised or named in an incident, and that is exactly when the label
has to win.

A read that fails, a summary that does not match the five fields, a negative count, a `deny` bit
that is not a boolean or that is true with no accepted or contested record, or a
`latest_accepted_id` that is unreadable or that disagrees with the `accepted` count all read
`UNKNOWN`, and the allowlist does not forgive them. The count check is not pedantry: a summary
with `accepted` 1 and no id, or an id with no accepted record, would otherwise fall through the
no-effect branch and pass an allowlisted payee. A pending record that can deny is a censorship
tool with a storage deposit; `verdict_for` is the only place the mapping lives. Where the desk pages
exist they call it, and the tests call it with hand-built rows; no payment or signing path is built,
and when one is it must call this function rather than keep a second copy of the ladder.

## What a record is not

It is not a guarantee that a subject is criminal, and it is not attribution. A record states an
event, its outcome, its evidence, its state and who asserted it. Publisher statistics are counts of
reports and outcomes, never an accuracy rate. Response times and an appeal are outside this model:
`docs/REPORTING.md` on the reporting lane sets them out, and the desk pages on the pages lane render
a record's state beside every subject.

## The report body behind body_sha256

A report is hashed from exactly six fields, and the contract test and the Python test must build the
same bytes from the same file:

    {evidence_sha256, evidence_url, kind, outcome, subject, submitter}

Keys sorted, compact JSON, UTF-8, no nulls, no trailing newline. `subject` is the single account
agents should not pay, and `submitter` is the account that filed it. `evidence_url` is https and at
most 512 bytes, measured in bytes rather than characters so a multibyte url cannot pass a 512
character check and exceed the limit; the report contract stores the hash and the url, and the record
adds `fetched_at` off chain. `fixtures/records/report-body-vector.json` holds one body, its exact
canonical bytes and its sha256. Today `tests/test_records.py` checks it; the Rust test that reads
the same file lands with the contract change, and then a change on either side breaks both.

## What the contract lane still has to do

This branch defines the vocabulary and the off-chain model. The contract change that follows it has
to, in `contracts/gate`:

- read `intel/record_enums.json` and refuse a state or a kind outside it,
- expose `subject_summary(subject)` as exactly `{accepted, pending, contested, latest_accepted_id,
  deny}`, setting `deny` when it accepts a report of one of the five `DENY_KINDS` and when it
  contests a record it had accepted as one,
- build the canonical report body and hash it into `body_sha256`,
- and add a Rust test that reads `fixtures/records/report-body-vector.json` and reproduces its bytes
  and sha256.

Until each of those is merged, this document describes the intended contract, not a shipped one.
