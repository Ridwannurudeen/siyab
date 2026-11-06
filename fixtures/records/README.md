# Record fixtures

Every file here is a synthetic example, and every name in one is a placeholder.

The subjects are `example-subject.testnet` and `example-collector.testnet`, which are not accounts anybody uses and are not meant to resolve. The evidence URLs point at `evidence.example`, which is the reserved domain for documentation and cannot name a real page. No fixture describes a real person, a real account or a real event, and none should be read as an accusation: the point of these files is the shape of a record, not a claim about anyone. A reader should not look here for the provenance of a record, because these records have none to find.

The publisher and submitter names in the fixtures are placeholders for the same reason.

If you need a fixture that names a real account, it needs a primary source behind it, and it belongs in a test with that source recorded, not in this directory.

Two fixtures are vectors rather than records:

- `body-vector.json` pins the canonical bytes of a report body, which the contract lane hashes in Rust and this lane hashes in Python.
- `report-body-vector.json` pins the same rule through `intel/records.report_body`.
