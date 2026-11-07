# Registry record

Version: 1 (events `shieldbot-near` `1.0.0`)

## Purpose

The registry contract (`contracts/registry`) keeps the latest verdict for each NEAR account and, separately, label assertions keyed by their id. Anyone can read a record and check it offline against its evidence document (`evidence-v1.md`), because the record carries the evidence document's sha256. A record is not signed; the link to the verifier is the evidence document and, for an ALLOW, the Verdict object whose signature covers the same evidence hash (`verdict-v1.md`). `docs/REGISTRY.md` lists the methods and refusal codes; this page fixes the shapes.

## The verdict code

`contracts/registry/src/lib.rs:12`. In JSON the verdict is one of the strings `Unknown`, `Allow`, `Deny`, written exactly so (the variant names, with no renaming). In borsh, as the contract stores it, the discriminants are `Unknown` 0, `Allow` 1, `Deny` 2. Unknown is the zero value, so no record reads as allowed by default.

## The record input

`record(subject, verdict, evidence_hash, observed_block, verdict_id)` takes one record; `record_batch({"records": [...]})` takes at most 29, each an object with these five keys (`contracts/registry/src/lib.rs:20`, `:141`). Only the recorder account may call either.

| Key | JSON type | Constraint |
|---|---|---|
| `subject` | string | a valid NEAR account id (the SDK refuses any other); the payee, never the agent |
| `verdict` | string | `Allow`, `Deny` or `Unknown` |
| `evidence_hash` | string | exactly 64 lowercase hex characters, else `REFUSED:bad_hash` (`contracts/registry/src/lib.rs:293`) |
| `observed_block` | number | a u64: the block height the checks read |
| `verdict_id` | string | at most 64 bytes, else `REFUSED:bad_verdict_id` (`contracts/registry/src/lib.rs:261`) |

The contract checks nothing else; in particular it does not check that the evidence document exists or that the verdict id matches it. ShieldBot's recorder builds the input from a verdict file and its evidence document (`gate/registry.py:27`): `subject` is the evidence subject's `receiver`, `observed_block` is `observed.block_height` unless given, and `verdict_id` is the Verdict's id for an ALLOW, or the first 32 hex characters of the evidence hash for an unsigned DENY or UNKNOWN (`gate/registry.py:64`). It refuses an intents document.

A second record for the same subject replaces the first. `total_records` counts every record written; `total_subjects` counts distinct subjects.

## The `latest` view

`latest(subject)` returns null for an unrecorded subject, else this object, with the keys in this order (`contracts/registry/src/lib.rs:39`, `:206`):

| Key | JSON type | Content |
|---|---|---|
| `verdict` | string | `Allow`, `Deny` or `Unknown` |
| `evidence_hash` | string | 64 lowercase hex (stored as 32 bytes and written back in lowercase hex, `:310`) |
| `observed_block` | number | as recorded |
| `recorded_ms` | number | the block time in milliseconds when the record was written |
| `verdict_id` | string | as recorded |

`is_allowed(subject, max_age_ms)` returns a two element array `[allowed, reason]`: `[false, "unrecorded"]`, `[false, "stale"]` when the block time minus `recorded_ms` is above `max_age_ms`, then `[false, "deny"]`, `[false, "unknown"]` or `[true, "allow"]` (`contracts/registry/src/lib.rs:210`).

## Label records

`record_label(subject, chain, label, category, provenance, assertion_hash)` stores one label assertion (`contracts/registry/src/lib.rs:152`). `assertion_hash` is the assertion's `id` (`label-assertion-v1.md`): 64 lowercase hex, else `REFUSED:bad_hash`, and each hash is recorded once, else `REFUSED:label_exists`. The five other arguments are strings the contract does not check. `get_label(assertion_hash)` returns null or `{"subject", "chain", "label", "category", "provenance", "assertion_hash", "recorded_ms"}` in that order, with `assertion_hash` in lowercase hex (`contracts/registry/src/lib.rs:72`).

## Events

Every event is one log line: the ASCII prefix `EVENT_JSON:` followed by compact JSON with the keys in the order `standard`, `version`, `event`, `data`; `standard` is `shieldbot-near`, `version` is `1.0.0`, and `data` is a one element array (`contracts/registry/src/events.rs`). Keys are written in the order the contract lists them, not sorted: the contract's `serde_json` is built with insertion order kept (its `Cargo.lock` pulls `indexmap` into `serde_json`), and the testnet fixtures below show that order.

| Event | `data[0]` keys, in order | Emitted by |
|---|---|---|
| `verdict_recorded` | `subject`, `verdict`, `evidence_hash`, `observed_block`, `verdict_id` | each record (`contracts/registry/src/lib.rs:280`) |
| `label_recorded` | `subject`, `chain`, `label`, `category`, `provenance`, `assertion_hash` | `record_label` (`:178`) |
| `recorder_updated` | `previous`, `current` | `set_recorder` (`:200`) |

`evidence_hash` and `assertion_hash` in an event are the strings the call passed, which the contract has checked are 64 lowercase hex, so they equal the view's.

## Checking a record

A record conforms to its evidence document when the sha256 of the document's file bytes equals `evidence_hash` and the document's `verdict` equals the record's verdict read in upper case (`ALLOW`, `DENY`, `UNKNOWN`). For an ALLOW with its Verdict object, the Verdict's `evidence_hash` must equal the same sha256, its `verdict_id` the record's, and its signature must verify over the message rebuilt from the evidence subject and the Verdict fields, with the evidence's `memo_sha256` as message field 8 (`gate/verify.py:74`, `:93`).

`gate.verify.load_record` (`gate/verify.py:51`) reads the view, an event's `data[0]` or a whole event. As a reader it is more lenient than the contract writes: it also accepts the verdict in upper case or as its code 0, 1, 2 and the hash as a list of 32 byte values. A writer must use the forms above.

## Worked example

`fixtures/registry/testnet-vendor.json` is the `latest` view for `kestrelridge-shop.testnet` read from testnet:

```
{"verdict":"Allow","evidence_hash":"7e77187d...","observed_block":275799179,"recorded_ms":1793565200130,"verdict_id":"1b29052381ecf3fc1937b36db2539678"}
```

The file `fixtures/evidence/7e77187d...json` hashes to `evidence_hash`, its `verdict` is `ALLOW` and its `verdict_id` is the record's. For a DENY such as `fixtures/registry/testnet-lookalike.json`, `verdict_id` is the first 32 hex characters of `evidence_hash`.

## Vectors

| Path | What it pins | Reproduced by |
|---|---|---|
| `fixtures/registry/sample-allow.json`, `fixtures/registry/sample-allow-event.json`, `fixtures/registry/sample-allow-evidence.json`, `fixtures/registry/sample-allow-verdict.json` | a view and an event for one ALLOW, its evidence document and its Verdict, signed with the TEST verifier key in `fixtures/keys/test-verifier.pub`; every comparison holds | `gate.verify.verify` |
| `fixtures/registry/testnet-vendor.json`, `fixtures/registry/testnet-lookalike.json`, `fixtures/registry/testnet-aged-payee.json` and the five `fixtures/registry/testnet-latest-*.json` views | `latest` views read from testnet, three ALLOW and five DENY; each checks against its evidence document in `fixtures/evidence/` | `gate.verify.load_record`, `gate.verify.verify` |
| `fixtures/registry/event-087cff-20261103.json` | a `verdict_recorded` event as logged on testnet, key order included | `gate.verify.verify` |
| `fixtures/registry/labels-20261103.json` | 43 `label_recorded` events as logged on testnet; each `assertion_hash` is a store row's id | `intel.labels.load` |

The contract side is pinned by its unit tests: `record_upserts_without_inflating_subject_count`, `batch_limit_is_enforced`, `is_allowed_returns_each_reason`, `invalid_hash_is_refused`, `long_verdict_id_is_refused`, `record_label_is_once_per_assertion_hash` and `emits_all_nep_297_events` in `contracts/registry/src/lib.rs`. No fixture pins a `recorder_updated` event.

## Source of truth

- `contracts/registry/src/lib.rs:12` the verdict code; `:20` the input; `:39` the view; `:72` the label view; `:260` `record_one`; `:293` `parse_hash`; `contracts/registry/src/events.rs` the event envelope.
- `gate/registry.py:27` how ShieldBot builds a record; `gate/verify.py:51` and `:93` how a record is checked.
