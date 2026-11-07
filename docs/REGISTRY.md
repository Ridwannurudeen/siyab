# Registry interface

The registry records the latest verdict for a NEAR account and label assertions keyed by assertion hash. The recorder account submits records; anyone can call the views. UNKNOWN is the zero value, so a subject without a record does not read as allowed.

`new(owner_id, recorder_id)` sets the owner and recorder accounts and initializes the empty registry. The registry account pays storage for its records. With 3 NEAR available for storage, expect about 700 to 1000 records; actual capacity varies with subject and verdict ID lengths.

## Verdict records

`record` takes the same fields as one `record_batch` entry: `subject` is a NEAR account ID, `verdict` is `Allow`, `Deny` or `Unknown`, `evidence_hash` is 64 lowercase hexadecimal characters, `observed_block` is a u64, and `verdict_id` is at most 64 bytes. Longer verdict IDs are refused with `REFUSED:bad_verdict_id`. System verdict IDs are 32 hexadecimal characters. ShieldBot's recorder refuses a plain unsigned ALLOW passed to a human, which has no `verdict_id`, and checks an ALLOW's id against the evidence (`gate/registry.py:60-65`); the contract itself checks neither the evidence nor a signature. `record_batch` takes `{"records": [...]}` and accepts at most 29 entries. A repeated subject replaces its latest record, increments `total_records`, and leaves `total_subjects` unchanged.

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

## Mainnet

The registry is not on mainnet. Every registry record and label is on testnet (`docs/TESTNET.md`, Registry).

`scripts/deploy_registry_mainnet.sh <registry_account> <owner_account> <recorder_account> <wasm>` is the mainnet deploy script. It must not be run until all six gates below are met and the owner approves. It has never been run against mainnet; offline tests cover dry-run plans and live confirmation and account-code guards with fake `near` and RPC responses (`tests/test_registry_mainnet.py`).

| Gate | Status | Evidence |
|---|---|---|
| 1. The mainnet deploy script is reviewed. | met | Line-by-line review: `docs/reviews/registry-mainnet-script-2026-11-07.md`. All documented refusals and post-deploy assertions pass source review; the offline tests pass, with nine POSIX pty cases skipped on Windows. No mainnet call was made. |
| 2. The recorder key is separate from the verifier key and off the build machine. | not met | The testnet recorder key is held by the team's operator (README, Trust model); no record moves it off the build machine. |
| 3. Recorder rotation and recovery are written and rehearsed. | not met | Rotation is scripted: `scripts/rotate_recorder.sh <new_recorder_account>` calls `set_recorder` on the registry with the owner key, testnet only (`require_testnet`), tested in dry run only (`tests/test_ops_scripts.py`). `set_recorder` is tested in the sandbox (`contracts/registry/tests/sandbox.rs::registry_records_over_rpc_and_rotates_recorder`). It has not been rehearsed on testnet: `docs/TESTNET.md` records no rotation. The gate stays not met until a testnet rehearsal is recorded there. |
| 4. A correction and supersession policy is written. | met | `docs/CORRECTIONS.md` records the supported replacement and supersession paths, the 72-hour publication commitment, and the limits the contract enforces. |
| 5. Records are limited to our own verdicts and primary sourced incident labels. | met on testnet | `docs/TESTNET.md` (Registry) lists every write: seven verdicts from our own runs and 43 `incident` labels, provenance `onchain` 28 and `third_party` 15, each with its stored source. The single recorder account is the control; no contract rule enforces the limit. |
| 6. One outside party has run `python -m gate verify` on a testnet record. | not met | The verify runs on record (`fixtures/runs/verify-*.txt`) are the team's own. |

The script refuses (exit 2) unless `NETWORK=mainnet` and `MAINNET_DEPLOY_OK=1` are both set (`require_mainnet_deploy_ok` in `scripts/lib.sh`; it never calls `require_testnet`). The three accounts must be named accounts ending in `.near`, so an implicit account is refused, and the recorder must differ from the owner. Before any near call it computes the wasm's sha256 and refuses unless it equals the first field of `res/registry.wasm.sha256` and `res/BUILD-registry.md` has a 40 hex `Source commit:` line; it prints the source commit, the sha256 and the expected code_hash from `python -m gate codehash`. Outside a dry run it prints a banner and reads the registry account name from the terminal, and stops unless it matches.

The registry account must already exist. The script reads its code_hash with `view_account` and stops (exit 2) with `registry account <id> not found on mainnet` when it does not exist (tested offline against a fake RPC client, not live); it refuses an account that holds code unless `REDEPLOY=1`. A first deploy calls `deploy_with_init` with `new` and `{"owner_id":<owner_account>,"recorder_id":<recorder_account>}`, the same initializer and arguments `scripts/setup_testnet.sh` uses on testnet, signed with the registry account's key file `$NEAR_KEYS_DIR/<registry_account>.json`. `REDEPLOY=1` requires `EXPECT_VERSION` and `EXPECT_CODE_HASH`, refuses (exit 2) before any near call unless `EXPECT_CODE_HASH` equals the wasm's code_hash, and deploys without an init call, keeping the state; `scripts/redeploy.sh` is testnet only, so the script does this itself. After the deploy it asserts that `get_recorder` returns the recorder account and that the account's code_hash equals the wasm's (or `EXPECT_CODE_HASH`), asserts `version` against `EXPECT_VERSION` on a redeploy, and prints the summary block. The owner cannot be read back: the registry has no owner view. `DRY_RUN=1` or `--dry-run` prints every command and assertion and makes no near call.
