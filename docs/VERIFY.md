# Verify a NEAR testnet record offline

Run these commands from the repository root with Python 3.11 or later. They read committed files and do not need RPC or key files. Every `sh offline` command below is run by `tests/test_verify_guide.py` with `urllib.request.urlopen` blocked. The output shown is from this checkout; each 64-character hex value is shortened to its first and last eight characters.

## 1. Compare the committed Wasm with its build records

For each committed artifact, run `codehash`. Compare its `sha256` line with the first field in the matching `.sha256` file, and its `code_hash` line with the matching entry in `res/BUILD-*.md`. For the deployed Gate, registry and Reports contracts, `docs/TESTNET.md` records the read-back `code_hash` too.

```sh offline
python -m gate codehash res/gate.wasm
python -m gate codehash res/registry.wasm
python -m gate codehash res/reports.wasm
python -m gate codehash res/previous/gate.wasm
python -m gate codehash res/previous/reports.wasm
```

```text expected
sha256: 6b684655...9d000aa6
code_hash: 8EGqzorvbx5PiPCnLJCbi79p9HWsie93iwgCT6GjStL5
sha256: 2a154191...f0ec0cfa
code_hash: 3qGvVjTHVyvYVGQjJhqQGJMzmkUHwEbTCfWqHhXnW4VX
sha256: ba2551dc...7f8b7705
code_hash: DXdqac6HBiXtZVCrJjdGjDEFfzcFF7EEDza8h8rU5UCC
sha256: f7a09b28...fd14a107
code_hash: Hfdk3jGUFxQyP8wEEoejNUkZ81bE9gDmoUBfGRghR5iE
sha256: e28ac3fc...b4737134
code_hash: GFKs9k5P6raHNxN6suXJNJByjsYdXAZdGdJE5a5duZes
```

This checks that the committed Wasm bytes match the checked-in checksums and recorded hashes. It does not rebuild from source or establish which code is currently deployed. To reproduce a build, use the source commit and recipe in the corresponding `res/BUILD-*.md`, from that contract crate, with `CARGO_TARGET_DIR` unset:

```sh needs-docker
cargo near build reproducible-wasm
```

That build needs the pinned Rust and cargo-near toolchains, Docker, and network access to pull the build image and dependencies. It was not run as part of these offline checks. A network `view_account` read is needed to compare a contract's deployed `code_hash` with the artifact.

## 2. Check a recorded registry view against its evidence

This runs `python -m gate verify` on the vendor record listed in `docs/TESTNET.md`. The short Python expansion finds the evidence filename from the record, so the command works in PowerShell and POSIX shells without a wildcard or a copied full hash.

```sh offline
python -m gate verify fixtures/registry/testnet-vendor.json $(python -c "import json; from pathlib import Path; record=json.loads(Path('fixtures/registry/testnet-vendor.json').read_text(encoding='utf-8')); print((Path('fixtures/evidence')/(record['evidence_hash']+'.json')).as_posix())")
```

```text expected
record shape: view
evidence sha256: 7e77187d...c88bb2cc
record evidence_hash: 7e77187d...c88bb2cc
NEP-297 fields to compare with the explorer:
  standard shieldbot-near, version 1.0.0, event verdict_recorded
  subject kestrelridge-shop.testnet (from the evidence; the view shape has none)
  verdict ALLOW, evidence_hash 7e77187d...c88bb2cc
  observed_block 275799179, verdict_id 1b29052381ecf3fc1937b36db2539678
ok   evidence sha256 equals the record's evidence_hash
ok   evidence verdict equals the record's verdict
verified
```

The command hashes the evidence file bytes and compares that hash and the evidence verdict with the saved registry view. It prints the event fields so a reader can compare them with a `verdict_recorded` event. It does not query the chain, prove the record is still the current view, or prove the checks in the evidence are factually true.

To read the current registry value, query `latest(subject)` on the testnet registry. This read needs `near-cli-rs` and network access; it is not an offline check:

```sh needs-network
near contract call-function as-read-only registry.kestrelridge-main.testnet latest json-args '{"subject":"kestrelridge-shop.testnet"}' network-config testnet now
```

## 3. Check canonical evidence bytes and a signed Verdict

The evidence page defines the stored file as canonical JSON whose sha256 is its filename. This checks the vendor evidence file against both rules:

```sh offline
python -c "import json; from pathlib import Path; from gate import evidence; record=json.loads(Path('fixtures/registry/testnet-vendor.json').read_text(encoding='utf-8')); path=Path('fixtures/evidence')/(record['evidence_hash']+'.json'); assert evidence.check_file(path); print('canonical evidence: ok')"
```

```text expected
canonical evidence: ok
```

For the signature check, use the committed sample ALLOW, sample evidence and sample Verdict from the registry vectors. The command derives the public TEST key from the published test seed, then passes it to `python -m gate verify`:

```sh offline
python -m gate verify fixtures/registry/sample-allow.json fixtures/registry/sample-allow-evidence.json --verdict fixtures/registry/sample-allow-verdict.json --key $(python -c "from gate import base58, ed25519; print(base58.encode_key(ed25519.public_from_seed(ed25519.TEST_VERIFIER_SEED)))")
```

```text expected
record shape: view
evidence sha256: c9afcae5...9b3a2782
record evidence_hash: c9afcae5...9b3a2782
canonical message: GATEv1|gate.testnet|29eb5de0...c0f1878a|ft|token.gate.testnet|vendor.testnet|1000000|55cd62f7...121bd1c5|1793534400000|1793534700000|2ebb8f1dcba43f2861590189bfa9c2fa|c9afcae5...9b3a2782|cur|ALLOW
NEP-297 fields to compare with the explorer:
  standard shieldbot-near, version 1.0.0, event verdict_recorded
  subject vendor.testnet (from the evidence; the view shape has none)
  verdict ALLOW, evidence_hash c9afcae5...9b3a2782
  observed_block 230000000, verdict_id 2ebb8f1dcba43f2861590189bfa9c2fa
ok   evidence sha256 equals the record's evidence_hash
ok   evidence verdict equals the record's verdict
ok   verdict literal is ALLOW
ok   verdict evidence_hash equals the evidence sha256
ok   verdict verdict_id equals the record's verdict_id
ok   signature verifies with ed25519:36ZbCswnc8QBvUxFF1pEwKwj6PZwgRWGe4nqcdy9HCzL
verified
```

This verifies the sample's evidence hash, Verdict fields and Ed25519 signature over the sha256 of the rebuilt canonical message described in `docs/spec/evidence-v1.md` and `docs/spec/verdict-v1.md`. `gate.evidence.check_file` checks canonical JSON bytes and the filename hash; it does not validate every evidence field. The signed sample uses the TEST key for vectors. This does not verify a testnet ALLOW signed by the live verifier key or show that the testnet Gate trusts the TEST key. Neither command evaluates the truth of the evidence sources.

## 4. Validate the synthetic incident records

`docs/RECORD.md` says the two committed incident records are synthetic. This command passes those files to `python -m intel record-check`; it excludes `body-vector.json`, which is a hash vector, not an incident record.

```sh offline
python -m intel record-check $(python -c "from pathlib import Path; records=sorted(path.as_posix() for path in Path('fixtures/records').glob('*.json') if path.name != 'body-vector.json'); print(*records, sep=chr(10))")
```

```text expected
valid fixtures/records/06756e22...851d7427.json
valid fixtures/records/40a5ff25...2eaef25a.json
```

`record-check` validates the record fields, canonical file bytes, content-derived record id and filename, and reports duplicate supersession conflicts among the files passed to it. These fixtures are synthetic: this check does not fetch their cited evidence URLs or reports, and it does not read a report from the chain. A `get_report(report_id)` view call is needed to check a real report citation; it requires network access.
