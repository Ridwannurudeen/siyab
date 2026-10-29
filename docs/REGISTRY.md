# Registry interface

The registry records the latest verdict for a NEAR account and label assertions keyed by assertion hash. The recorder account submits records; anyone can call the views. `UNKNOWN is the zero value`, so a subject without a record does not read as allowed.

## Verdict records

`record` takes the same fields as one `record_batch` entry: `subject` is a NEAR account ID, `verdict` is `Allow`, `Deny` or `Unknown`, `evidence_hash` is 64 lowercase hexadecimal characters, `observed_block` is a u64, and `verdict_id` is a string. `record_batch` takes `{"records": [...]}` and accepts at most 50 entries. A repeated subject replaces its latest record, increments `total_records`, and leaves `total_subjects` unchanged.

`latest(subject)` returns `null` or an object with `verdict`, lowercase hexadecimal `evidence_hash`, `observed_block`, `recorded_ms` and `verdict_id`. `is_allowed(subject, max_age_ms)` returns a two item array containing a boolean and one of `unrecorded`, `stale`, `deny`, `unknown` or `allow`. An unrecorded subject, a stale record, `Deny` and `Unknown` all return false.

`record_label` takes `subject`, `chain`, `label`, `category`, `provenance` and a 64 character lowercase hexadecimal `assertion_hash`. Each assertion hash can be recorded once. `get_label(assertion_hash)` returns the stored label fields with a lowercase hexadecimal assertion hash, or null. `get_recorder`, `total_records`, `total_subjects`, `total_labels`, `version` and `contract_source_metadata` are views. `set_recorder` is owner only and requires exactly 1 yoctoNEAR.

Refusal codes are `REFUSED:not_recorder`, `REFUSED:batch_too_large`, `REFUSED:label_exists`, `REFUSED:not_owner` and `REFUSED:bad_hash`. The owner method also rejects any attached deposit other than 1 yoctoNEAR using the SDK deposit assertion.

Verdict events use NEP-297 with `standard` equal to `shieldbot-near` and `version` equal to `1.0.0`. `verdict_recorded` contains the subject, verdict, evidence hash, observed block and verdict id. `label_recorded` contains the label assertion fields. `recorder_updated` contains the previous and current recorder account IDs.

Registry evidence hashes use sha256. The EVM registry uses keccak256; the two hash functions are distinct and records from the EVM leg are not written to this NEAR registry.

To verify a record and its evidence file after RUN-N4 implements the verifier, run `python -m gate verify <record.json> <evidence.json>`. It recomputes the evidence hash, checks the verdict signature against the named key and prints the event fields for comparison.

A stranger can read a record without a key with:

```sh
near contract call-function as-read-only registry.<gate> latest json-args '{"subject":"<account>"}' network-config testnet now
```

The registry accepts NEAR account subjects only. The recorder is a server held account. No third party consumer is connected yet.
