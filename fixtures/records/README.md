# Record fixtures

Every file here is a synthetic example, and every name in one is a placeholder.

The subjects are placeholder account ids: `example-subject.testnet`, `example-collector.testnet`, and a
64 character implicit account that nobody holds a key for. The evidence URLs point at `evidence.example`,
the reserved domain for documentation, which cannot name a real page. The chain heights and times in a
timeline are illustrative and are not blocks this repository observed. No fixture describes a real person,
a real account or a real event, and none should be read as an accusation: the point of these files is the
shape of a record, not a claim about anyone. A reader should not look here for the provenance of a record,
because these records have none to find.

The publisher and submitter names in the fixtures are placeholders for the same reason.

If you need a fixture that names a real account, it needs a primary source behind it, and it belongs in a
test with that source recorded, not in this directory.

`body-vector.json` is a vector rather than a record. It pins the canonical bytes of a report body, which
the contract lane hashes in Rust and this lane hashes in Python, so a change on either side breaks both.
