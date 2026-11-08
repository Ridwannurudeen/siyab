# Format specifications

These pages specify the byte formats ShieldBot for NEAR writes and reads, so that a party outside this repository can produce and check them without reading the code. Each page gives the fields, the canonical serialization byte for byte, the hashing, a worked example, the vectors that pin the format and the code that is the source of truth.

| Page | Format | Version |
|---|---|---|
| [label-assertion-v1.md](label-assertion-v1.md) | a label assertion: one row of `intel/labels.jsonl` and its id | 1 |
| [evidence-v1.md](evidence-v1.md) | the evidence document behind a verdict, its checks and its sha256 | 1 |
| [verdict-v1.md](verdict-v1.md) | the canonical verdict message, its Ed25519 signature and the Verdict object | 1 (prefix `GATEv1`) |
| [registry-record-v1.md](registry-record-v1.md) | the registry's verdict record, label record and their NEP-297 events | 1 (events `1.0.0`) |
| [report-v1.md](report-v1.md) | a community report, its body hash, its exact deposit, its state changes and moderation refusals, its events and the `subject_summary` view | 1 (events `1.1.0`) |
| [snapshot-v1.md](snapshot-v1.md) | a label snapshot: the current label assertions a scope selects, with the store's sha256 and a content hash, for offline use, and what a consumer may infer from it | 1 (format `label-snapshot`) |

## Versioning

Every format carries a version string in its own bytes: the evidence document's `version` key (`"1"`), the `GATEv1` prefix of the canonical message, the registry NEP-297 `version` (`1.0.0`) and the Reports NEP-297 `version` (`1.1.0`). The reports contract's `version()` and `state_version()` views name its crate and its stored state layout, not a format. A label assertion carries no version field; its version is this page set, and a change to its fields or its id rule is a new version. A label snapshot carries `"format": "label-snapshot"` and `"version": "1"`.

Each page states its version on a line that begins `Version: <n>`, such as `Version: 1`, optionally followed by the version string the bytes carry in parentheses. A change that would make a conformant implementation of the old page produce or accept different bytes is a breaking change: it gets a new version string and a new page (`evidence-v2.md` beside `evidence-v1.md`), and the old page stays, unchanged, for as long as any stored record uses it. A change that only clarifies wording, adds a vector or corrects a line reference keeps the version.

Where a page and the code disagree, the code is the source of truth and the page is wrong; each page names the file and line it follows.

## Version 1 freeze

As of this commit, every format listed above is version 1. A change to any byte-level rule requires a new v2 page beside the v1 page; do not edit a v1 page to change the bytes a conforming writer emits or reader accepts. Clarifications, added vectors and corrected references that leave the byte rules unchanged do not change the version.

`tests/test_spec.py` pins the page vectors with these tests:

| Page | Vector tests |
|---|---|
| [label-assertion-v1.md](label-assertion-v1.md) | `tests/test_spec.py::LabelAssertionTest.test_vectors_reproduce`, `tests/test_spec.py::LabelAssertionTest.test_registry_label_events_name_store_rows` |
| [evidence-v1.md](evidence-v1.md) | `tests/test_spec.py::EvidenceTest.test_stored_documents_check`, `tests/test_spec.py::EvidenceTest.test_worked_example_is_vector_04` |
| [verdict-v1.md](verdict-v1.md) | `tests/test_spec.py::VerdictTest.test_vectors_regenerate_and_verify`, `tests/test_spec.py::VerdictTest.test_worked_example_is_vector_04`, `tests/test_spec.py::VerdictTest.test_test_verifier_key_file` |
| [registry-record-v1.md](registry-record-v1.md) | `tests/test_spec.py::RegistryRecordTest.test_sample_allow_verifies`, `tests/test_spec.py::RegistryRecordTest.test_testnet_records_check_against_their_evidence` |
| [report-v1.md](report-v1.md) | `tests/test_spec.py::ReportTest.test_body_vector_reproduces`, `tests/test_spec.py::ReportTest.test_enumerations_match_the_canonical_json` |
| [snapshot-v1.md](snapshot-v1.md) | `tests/test_spec.py::SnapshotTest.test_vectors_reproduce`, `tests/test_spec.py::SnapshotTest.test_the_vector_store_copies_store_lines_and_marks_the_rest_synthetic`, `tests/test_spec.py::SnapshotTest.test_worked_example_is_vector_1` |

## Conformance

An implementation conforms to a page when it reproduces every vector that page names byte for byte: the same serialized bytes, the same hashes and the same signatures, and the same acceptance or refusal for every vector that says what is expected. Reproducing a hash by another route (for example by reading it from the fixture) is not conformance; the bytes must be built from the vector's inputs.

The vectors are files under `fixtures/`. `tests/test_spec.py` checks, for every page, that each fixture path the page cites exists, that each vector reproduces with this repository's own functions, and that the page has its version line.

## Shared conventions

- Text is UTF-8 with no byte order mark. No serialized form ends with a newline unless the page says so.
- Hashes are SHA-256 (FIPS 180-4), written as 64 lowercase hex characters. NEAR records never use keccak256; ShieldBot's EVM registries do, and the two are never compared.
- Signatures are pure Ed25519 (RFC 8032), not Ed25519ph, written as 128 lowercase hex characters.
- A NEAR account id is 2 to 64 characters and matches `^(([a-z\d]+[-_])*[a-z\d]+\.)*([a-z\d]+[-_])*[a-z\d]+$` (`gate/canon.py:29`).
- Canonical JSON, where a page uses it, is the form `json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)` encoded as UTF-8: object keys sorted by Unicode code point (not by UTF-16 code unit as RFC 8785 does), no whitespace outside strings, non ASCII characters written as themselves, `"` and `\` escaped as `\"` and `\\`, U+0008, U+0009, U+000A, U+000C and U+000D escaped as `\b`, `\t`, `\n`, `\f` and `\r`, every other character below U+0020 escaped as `\u00xx` with lowercase hex, and U+007F and above written as themselves. Each page says whether floats are allowed.
- 64 hex values (hashes, keys, ids) are not printed in full on these pages; the repository's hygiene check refuses them in documentation. The pages print the first 8 hex characters followed by `...` and name the fixture field that holds the full value.
