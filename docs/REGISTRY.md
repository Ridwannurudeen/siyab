# Registry interface

The registry records the latest verdict for a NEAR account and label assertions keyed by assertion hash. The recorder account submits records; anyone can call the views. UNKNOWN is the zero value, so a subject without a record does not read as allowed.

`new(owner_id, recorder_id)` sets the owner and recorder accounts and initializes the empty registry. The registry account pays storage for its records. With 3 NEAR available for storage, expect about 700 to 1000 records; actual capacity varies with subject and verdict ID lengths.

## Verdict records

`record` takes the same fields as one `record_batch` entry: `subject` is a NEAR account ID, `verdict` is `Allow`, `Deny` or `Unknown`, `evidence_hash` is 64 lowercase hexadecimal characters, `observed_block` is a u64, and `verdict_id` is at most 64 bytes. Longer verdict IDs are refused with `REFUSED:bad_verdict_id`. System verdict IDs are 32 hexadecimal characters. `record_batch` takes `{"records": [...]}` and accepts at most 29 entries. A repeated subject replaces its latest record, increments `total_records`, and leaves `total_subjects` unchanged.

`latest(subject)` returns `null` or an object with `verdict`, lowercase hexadecimal `evidence_hash`, `observed_block`, `recorded_ms` and `verdict_id`. `is_allowed(subject, max_age_ms)` returns a two item array containing a boolean and one of `unrecorded`, `stale`, `deny`, `unknown` or `allow`. An unrecorded subject, a stale record, `Deny` and `Unknown` all return false.

`record_label` takes `subject`, `chain`, `label`, `category`, `provenance` and a 64 character lowercase hexadecimal `assertion_hash`. Each assertion hash can be recorded once. `get_label(assertion_hash)` returns the stored label fields with a lowercase hexadecimal assertion hash, or null. `get_recorder`, `total_records`, `total_subjects`, `total_labels`, `version` and `contract_source_metadata` are views. `set_recorder` is owner only and requires exactly 1 yoctoNEAR.

For `record_verdicts.sh`, the labels file can be a bare JSON list of `record_label` argument objects or an object containing a `labels` list.

Refusal codes are `REFUSED:not_recorder`, `REFUSED:batch_too_large`, `REFUSED:label_exists`, `REFUSED:not_owner`, `REFUSED:bad_hash` and `REFUSED:bad_verdict_id`. The owner method also rejects any attached deposit other than 1 yoctoNEAR using the SDK deposit assertion.

Verdict events use NEP-297 with `standard` equal to `shieldbot-near` and `version` equal to `1.0.0`. `verdict_recorded` contains the subject, verdict, evidence hash, observed block and verdict id. `label_recorded` contains the label assertion fields. `recorder_updated` contains the previous and current recorder account IDs.

Registry evidence hashes use sha256. The EVM registry uses keccak256; the two hash functions are distinct and records from the EVM leg are not written to this NEAR registry.

To verify a record against its evidence file offline, run `python -m gate verify <record.json> <evidence.json>`. Without flags it recomputes the sha256 of the evidence file's bytes and compares it with the record's `evidence_hash`, checks that the evidence's verdict is the record's, and prints the NEP-297 event fields to compare with the explorer; it checks no signature. With `--verdict <verdict.json> --key ed25519:<base58>`, which go together, it also checks that the Verdict's literal is ALLOW, that its evidence hash equals the recomputed sha256 and its verdict id the record's, rebuilds the canonical message from the evidence and the Verdict, and checks the signature against that key with `gate.ed25519.verify` (`docs/VERDICT-RECORD.md`).

A stranger can read a record without a key with:

```sh
near contract call-function as-read-only registry.<gate> latest json-args '{"subject":"<account>"}' network-config testnet now
```

The registry accepts NEAR account subjects only. The recorder is a server held account. No third party consumer is connected yet.
