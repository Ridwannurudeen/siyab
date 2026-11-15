# Corrections and supersession

This policy covers verdicts and labels written to the NEAR registry, rows in `intel/labels.jsonl`, and incident records in format v1. It describes the operations the current code supports; the limits below are part of the policy.

## Who publishes a correction

One operator holds the registry recorder account (`docs/REGISTRY.md:31,42`). Only that account can call `record`, `record_batch` or `record_label` (`contracts/registry/src/lib.rs:123-161,253-257`). The registry owner can rotate the recorder with `set_recorder`, but rotation does not correct a record (`contracts/registry/src/lib.rs:191-204`). This policy assigns that operator to publish corrections to the repository too. The label store is a local file whose loader validates content but does not authenticate an editor (`intel/labels.py:161-190`); the incident writer checks `publisher` as an account ID but does not authenticate it (`intel/records.py:388-416`).

When the operator confirms a material error, they add a dated line to `docs/LOG.md` identifying the issue and the affected subject or old record ID. That confirmation line starts the correction clock. After publishing the correction, they add a dated notice that links the old and corrected IDs and, as applicable, both chain transactions or the repository commit and file paths. A reader finds the correction through this notice and can check the underlying chain event or repository artifact. The log notice is a manual policy step: no code writes it (`docs/REPORTING.md:41,43-53`).

## Wrong verdict record

The operator prepares a replacement input from the corrected verdict and its evidence with the existing registry input builder, then submits `record` or `record_batch` for the same subject (`gate/registry.py:28-82`; `contracts/registry/src/lib.rs:123-149`). The contract replaces that subject's `latest` value, increments `total_records`, and emits a new `verdict_recorded` event (`contracts/registry/src/lib.rs:206-208,260-289`). The earlier transaction and event remain permanently visible in the chain's historical transaction/event log; `latest(subject)` returns only the replacement value.

The contract has no delete, edit, or explicit verdict supersession field. Its historical event does not point to the replacement. The dated log notice must therefore name the old and new transactions, subject, verdicts and evidence hashes so a reader can connect them. The contract checks the input hash format and recorder identity, but does not establish that the evidence is true or that a verdict is correct (`contracts/registry/src/lib.rs:253-257,260-289,293-307`; `docs/REGISTRY.md:9`).

## Wrong label

For a row in `intel/labels.jsonl`, publish a corrected row with its own assertion ID and set the old row's `superseded_by` to that new ID. The assertion ID excludes `superseded_by`, so marking the old row does not change its ID; loading the store checks that the pointer names a row that exists, and verdict rules read rows whose pointer is null (`intel/labels.py:53-60,178-180`; `gate/rules.py:177-178`). Keep both rows in the store. The operator's dated notice names both assertion IDs and the commit containing them.

For a label already written on chain, the recorder submits the corrected fields with a new assertion hash. Each assertion hash is accepted only once, and each write emits a `label_recorded` event (`contracts/registry/src/lib.rs:152-189`). The contract checks hash syntax and uniqueness, not that the hash matches the label fields (`contracts/registry/src/lib.rs:152-189,293-307`). The old hash remains permanently readable through `get_label`, and its original transaction/event remains in chain history; the on-chain label fields have no `superseded_by` pointer (`contracts/registry/src/lib.rs:61-69,72-80,236-241`). The new transaction does not mark the old chain label as superseded, so the dated notice must pair the old and new hashes and transactions. A reader of the off-chain label store follows `superseded_by`; a reader of chain history uses the notice because the contract provides no correction link.

## Wrong incident record

For a format-v1 incident record, write a new record with the corrected facts and evidence, set `supersedes` to the old `record_id`, and give the new record its own `recorded` timeline entry. `intel.records.write` computes the new content-addressed ID, validates the record and writes `<record_id>.json` without replacing different bytes at that path (`intel/records.py:34-42,388-416`). Keep the old file. A reader follows the new record's `supersedes` field; the dated log notice names both IDs, file paths and the publishing commit.

`python -m intel record-check <record-files...>` validates the supplied files and refuses competing records that supersede the same ID (`intel/__main__.py:84-108`). The validator checks that `supersedes` is a 64-character lowercase hash but does not check that the named old record exists; the checker only compares the files supplied to it. The v1 format has no record store or renderer, so readers need the notice and file paths to find the correction (`intel/records.py:388-397`; `docs/RECORD.md:3`).

## Deadline and limits

For every confirmed material error, the single operator commits to publish a correction within 72 hours. This is the same 72-hour window as the `acknowledge` step in `docs/REPORTING.md` Service times (`docs/REPORTING.md:45-54`). The clock runs from the dated `docs/LOG.md` line recording confirmation to the correcting on-chain transaction or repository commit. One operator holds the recorder account, and no faster lane exists.

The code does not remove historical chain transactions or label assertions, automatically create correction notices, enforce the 72-hour deadline, or authorize edits to repository files. Verdicts have no on-chain supersession pointer; on-chain labels have no supersession field; and the incident-record checker cannot establish that an old record exists unless the reader supplies it. The notice and deadline are operator commitments, not contract-enforced behavior (`contracts/registry/src/lib.rs:30-36,61-69,123-204,206-289`; `intel/records.py:388-416`; `intel/__main__.py:84-108`).
