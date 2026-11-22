# Attestation registry

22 November 2026. T1 implements the frozen v1 registration interface above base c4899e190. Changes remain uncommitted in `feat/attest-registry`. No public transaction, deployment, key access, model call, spending, push, remote change or submission tag change occurred. The frozen spec, shared vectors and all protected files are unchanged.

## Contract

The contract exports `new`, `register_key`, `get_key`, `get_config` and private `on_quote_verified`, plus the SDK source metadata view. T0 reference types, hashes, strict Ed25519 verification and all vector tests are retained. No `submit_vote` entry point, owner, setter, upgrade or revoke method was added. T4b owns vote submission.

Initialization requires a self call, zero deposit and the exact five config fields. It stores immutable verifier and signer accounts, the Ed25519 domain, maximum lease and the sorted unique recipient allowlist. The contract does not claim that synthetic domain 7 is a deployed Ed25519 domain. That check belongs before deployment. JSON arguments reject extra fields and positional arrays. Events require JSON objects with exactly their five fields, including when `imr` is not 3. A new regression first reproduced Serde's default acceptance of positional event arrays, then passed after map-only event decoding was added.

Registration requires exactly 0.01 NEAR and exactly 300 Tgas. Budget mismatches panic before dispatch. Normally handled ingress refusals return JSON null and schedule a full refund to the captured predecessor. The method completely decodes the upstream Borsh `(QuoteBytes, Collateral)` tuple, refuses trailing bytes and forwards the original decoded bytes, zero deposit and exactly 200 Tgas to the configured verifier. Its private callback receives 60 Tgas. Both promises have unused gas weight zero.

The callback checks the bounded Borsh result, exactly `Verified`, exactly TD10, `UpToDate`, the complete independent TD attribute predicate, the registry's own report data binding, all RTMR3 event digests and extensions, exactly one compose hash event and exactly one key provider event. Measurement fields and PPID come from the verified report. Advisory ids retain their exact order. The issue time is inside the inclusive two hour interval ending at the callback's floored millisecond block time. Expiry is the checked minimum of issue time plus the configured lease and collateral expiration in checked milliseconds. No late relay extends it.

The callback reads the current record immediately before replacement, including expired records. A renewal requires a strictly newer issue time and unchanged measurement id, PPID and auxiliary value. Refusal preserves the previous bytes. A delayed old callback cannot overwrite a newer record. The record uses compact Borsh fields; views retain exactly the frozen eleven fields. Only positive actual storage growth is charged. Insufficient storage budget restores the previous bytes or removes the candidate's new slot before a full scheduled refund. Shrinking a record never returns an earlier relayer's storage payment. Refund receipts name the original relayer, not the callback predecessor. Receipt failure still has the spec's runtime and refund limitations.

## Key squatting

Registration proves the quote's binding to a public key, not possession of that key's private half. If another enclave learns a public key before its first accepted registration, it can obtain its own otherwise valid quote over that public key and race to register it under a different measurement, PPID or auxiliary value. The immutable renewal identity then prevents the intended enclave from replacing that record, including after expiry. This is an availability attack. It does not reveal the private key, create its signatures or make a Gate accept a measurement or platform outside its pins.

The enclave lane keeps the public key private during enrollment: both key bearing routes require the private relay's per boot bearer token, and public health contains no key or quote. Authentication remains required after registration. The relay must confirm that the accepted record matches its own identity before publishing the key to other callers. Its private signing seed always stays inside the enclave. The enrollment relayer necessarily sees the registration material, so this publication rule is a mitigation, not proof of possession or a guarantee against transaction observation. A key squatted under a conflicting identity requires a fresh enclave key. T1 adds no proof of possession field or exception to the frozen renewal rule.

## SDK host imports

The exact near-sdk 5.29.1 feature manifest enables only `wee_alloc` by default. Its `global-contracts` feature is empty, and it has no gas key feature. The production dependency graph already excludes `global-contracts`. The Promise action variants, their central dispatcher and the corresponding environment wrappers and near-sys declarations are unconditional. Turning off default features changes allocator selection and cannot remove these imports, so the SDK configuration is unchanged.

The optimized artifact imports these seven functions from `env`:

```text
promise_batch_action_transfer_to_gas_key
promise_batch_action_add_gas_key_with_full_access
promise_batch_action_add_gas_key_with_function_call
promise_batch_action_deploy_global_contract
promise_batch_action_deploy_global_contract_by_account_id
promise_batch_action_use_global_contract
promise_batch_action_use_global_contract_by_account_id
```

The owner must complete one successful testnet contract call with the reviewed artifact before any access key removal. Use the separate `register_key` command under Owner steps with a fresh bound quote, require successful verifier and callback receipts, and confirm the resulting record with `get_key`. Sandbox success does not establish public testnet runtime compatibility. This lane made no network transaction and removed no key.

## Evidence bounds

These are implementation admission limits, not amendments to the frozen wire format. Anything beyond them is refused. Bounds apply again during replay in the callback.

| Input | Maximum |
|---|---:|
| Raw registration JSON | 262144 bytes |
| Decoded verifier tuple | 65536 bytes |
| Exact compose UTF-8 | 65536 bytes |
| Event count, including non RTMR3 | 64 |
| Event name UTF-8 | 128 bytes |
| Decoded payload per event | 4096 bytes |
| Sum of digest, event name and payload string bytes | 32768 bytes |
| Verifier callback result | 16384 bytes |

The retained fixtures fit these limits. Their largest verifier tuple is 24178 bytes, largest exact compose is 31810 bytes, largest event count is 33 and largest selected RTMR3 count is 13. The test saturates the decoded tuple, compose, event count and aggregate event string limits together, then sends the same evidence as compact JSON, at the raw byte limit with whitespace, and at that limit with Unicode escapes. It does not saturate the individual name, payload or verifier response ceilings in that combined benchmark. One extra raw byte is refused before dispatch. An initial 524288 byte limit failed the ingress gas test and was reduced before the final runs. No gas success is inferred from a skipped case.

The 200 Tgas verifier allocation is enforced for every request. This does not claim that every arbitrary tuple within the size ceiling verifies within that allocation. A failed or exhausted verifier produces no record. The maximum replay benchmark uses the explicitly test-only responder, not a claim of DCAP verification for synthetic evidence.

## Gas snapshot

Observed in the final include ignored run with the optimized artifact in the build record. Values are Tgas. The full chain includes transaction and refund receipt costs. The tests require successful completion and keep ingress below 39 Tgas and callback below 30 Tgas for the three largest admitted request cases, inside the frozen 200 plus 60 Tgas promise allocations and 300 Tgas ingress budget.

| Case | Registry ingress | Registry callback | Full chain |
|---|---:|---:|---:|
| Small synthetic registration through TEST ONLY responder | 9.913955371476 | 2.268580687433 | 14.691837619405 |
| Combined tuple, compose, event count and aggregate limits | 35.556807050798 | 23.909063831857 | 62.715135123397 |
| Same evidence, 262144 raw bytes with whitespace | 37.399546107952 | 24.040695833283 | 64.751510867731 |
| Same evidence, 262144 raw bytes with Unicode escapes | 37.879225197292 | 24.040695833283 | 65.231189957071 |

The genuine vendored verifier followed by registry binding refusal used 140.622329502579 Tgas for the complete chain after supplying its retained compose and event log. It is a real verification and refusal measurement, not successful enrollment. These measurements describe the tested sandbox and artifact, not future protocol pricing or arbitrary evidence.

## Storage rows for the orchestrator

Add these exact rows to the prefix table before deployment. Prefixes are per registry account. No existing account migration is claimed.

| Contract | Prefix | Collection |
|---|---|---|
| Attestation registry | `STATE` | immutable five field config in the SDK root state |
| Attestation registry | `ak:` | compact key records, keyed by the raw 32 byte enclave public key; `KEYS_PREFIX` in `contracts/attest/src/lib.rs` |

The `ak:` slot is the three prefix bytes followed directly by the key bytes. It contains the five 48 byte measurement fields, the 32 byte compose hash, PPID, auxiliary value, ordered advisory ids and four u64 times in the field order of `StoredKey`. Measurement id and `UpToDate` are derived in the view. There is no expiry pruning, so renewal comparisons survive expiry.

## Validation

Rust runs use Ubuntu WSL, Rust 1.99.1, the locked dependency graph and external Cargo targets. Production registry sandbox tests load the optimized pinned-container artifact. The test responder and T0 replay probe are separate test contracts. The frozen shared JSON contains 137 cases, still independently reproduced by both references.

| Command or check | Result |
|---|---|
| Ordinary Cargo test | 33 library tests, 7 registry integration tests and 3 T0 integration tests passed; 43 passed, 2 explicitly ignored, 0 failures; 0 doctests |
| Cargo test with `--include-ignored` | 33 library tests, 7 registry integration tests and all 5 T0 integration tests passed; 45 passed, 0 ignored, 0 failures; 0 doctests |
| Python shared vector module | 47 passed, 0 skipped |
| Deployment script module in WSL | 16 passed, 0 skipped |
| Hygiene, citations, rustfmt and diff whitespace | Exit 0 |
| Two isolated snapshot builds | Identical optimized wasm bytes; build inputs checked against `res/attest.source.sha256` |

There are 108 distinct passing tests: 45 Rust and 63 Python. Ordinary, explicit and focused runs repeat those tests; they are not counted twice. The full repository suite was not run. The registry cases include debug TD, wrong registry report data, stale and future issue times, exact TD10 refusal of TD15, malformed and failed verifier results, unsupported status, PPID width, replay and compose changes, callback privacy, storage rollback and refunds, unchanged renewal identity, and asynchronous races in both callback orders. Unit tests reproduce the upstream MPC event log and require unique events even when the supplied final register matches duplicates. They check full refunds for failed, malformed, trailing and oversized results, and rollback both absent and existing records when storage exceeds the deposit.

The genuine test now inspects the actual verifier receipt from the registry promise chain. It requires bounded Borsh Verified output, exact TD10, UpToDate, valid TD attributes, a mismatched registry report data binding, a matching replay of the retained compose and event log, and 16 PPID bytes. All receipts must succeed, the registry callback must return exact JSON null bytes, and no key may be stored. A unit test calls the same production `verified_key` evaluator with bad binding and expired collateral and requires the exact error `report data mismatch`. Removing the binding check would fail that regression. Together these checks pin binding as the first refusal, without mistaking later expired collateral for the observed cause. The test does not claim that binding is the quote's only invalid field or that the quote proves current enrollment. No release log, view or export was added.

All six frozen reregistration vectors also call `valid_renewal`, the same internal production predicate that `on_quote_verified` applies to the current stored record. The complete candidate views are validated and converted to compact records before comparison. The T0 reference vector test remains, and the existing production callback race and rollback tests still run.

T0's unchanged real mainnet wasm also verifies all retained NEAR AI quotes in the explicit dated sandbox test. Its collateral deadline is 21 December 2026 at 14:22:06 UTC. Selecting that test after the deadline fails rather than returning early as passed. Successful registry registration, leases, storage and races use a clearly named TEST ONLY responder. The responder performs no cryptography and is not a deployment candidate.

None of the retained signed quotes binds the Siyab registry, enclave key, issue time and auxiliary value. This prevents a successful registration using the genuine verifier in this lane. A fresh owner CVM quote is required to finish that live proof. No fabricated signed quote or production bypass was added, and no frozen-spec defect was found.

`res/BUILD-attest.md` records the snapshot recipe, artifact hash, image digest and the committed-release steps still owned by the orchestrator. Source commit remains TBD and no release wasm is placed in res while the implementation is uncommitted. The two verified wasm outputs remain outside the repository. cargo-near's committed-source build discards working tree changes, so running it at the T0 base would not build T1. This Windows worktree's Git pointer is also unreadable by Linux Git; use a compatible checkout for the later committed-source build.

Run from the repository root after the snapshot build in the build record:

```sh
export CARGO_TARGET_DIR="$HOME/target/siyab-attest-t1"
mkdir -p "$CARGO_TARGET_DIR/near"
cp "$HOME/target/siyab-attest-t1-fix1-repro-clean-c/near/attest.wasm" "$CARGO_TARGET_DIR/near/attest.wasm"
cargo +1.99.1 build --manifest-path contracts/attest/tests/mock/Cargo.toml --locked --offline --target wasm32-unknown-unknown --release -j 2
cargo +1.99.1 build --manifest-path contracts/attest/tests/probe/Cargo.toml --locked --offline --target wasm32-unknown-unknown --release -j 2
cargo +1.99.1 test --manifest-path contracts/attest/Cargo.toml --locked --offline -j 2 -- --test-threads=1
cargo +1.99.1 test --manifest-path contracts/attest/Cargo.toml --locked --offline -j 2 -- --include-ignored --test-threads=1
python3 -m unittest tests.test_attest_vectors -v
python3 -m unittest discover -s contracts/attest/tests -p test_deploy_script.py -v
python scripts/check_hygiene.py --all
python scripts/check_citations.py
```

The final two checks ran with Windows Python and Windows Git, which understand this worktree's pointer. The deployment tests replace near with a marker executable and assert that it is never invoked. They exercise the script from an unrelated working directory and check its full printed command lines.

## Owner steps

Independent review and any commit stay with the orchestrator. Before deployment, fold the prefix rows above into `docs/UPGRADES.md`, add `attest` to `CRATES` and `CONTRACT_EXPORTS` in `tests/test_res.py`, add it to the rust-unit, sandbox and reproducible matrices in `.github/workflows/ci.yml`, and promote the reviewed committed build as described in the build record. Its exact function exports are `contract_source_metadata`, `get_config`, `get_key`, `new`, `on_quote_verified` and `register_key`; this build has no ABI export. The sandbox CI command currently selects only the integration target named sandbox. For attest, it must also build the optimized production artifact, test responder and replay probe with the prerequisites above and invoke registry_sandbox as well, or run the whole crate test suite. No shared file was changed by this lane.

The testnet pilot script defaults to dry run even if DRY_RUN is zero. It validates the exact public config and refuses mainnet, malformed accounts and contradictory flags. Execution requires NEAR_KEYS_DIR; keeping that directory outside the repository is an owner requirement, not a path-location check implemented by this script. It leaves access keys unchanged. A keyed pilot is not an immutable ownerless deployment. No mainnet deploy or key removal is included here.

With an existing funded registry account, an owner-reviewed public config, an actually verified Ed25519 signer domain and an explicit wasm path, the dry command is:

```sh
NETWORK=testnet bash scripts/deploy_attest.sh --dry-run registry.example.testnet /external/public-config.json /external/attest.wasm
```

Only after the owner's approval, the matching transaction command is:

```sh
NETWORK=testnet NEAR_KEYS_DIR=/external/keys bash scripts/deploy_attest.sh --execute registry.example.testnet /external/public-config.json /external/attest.wasm
```

The owner must then obtain a fresh Siyab-bound quote from its CVM using T2. This lane has no CVM or executable quote-generation path. Once the exact six field registration JSON is available, the owner can approve and run this separate relayer transaction with the frozen deposit and gas:

```sh
near contract call-function as-transaction registry.example.testnet register_key file-args /external/register.json prepaid-gas '300 Tgas' attached-deposit '0.01 NEAR' sign-as relayer.testnet network-config testnet sign-with-access-key-file /external/keys/relayer.testnet.json send
```

Account names and external paths in these examples are placeholders for owner-controlled inputs, not existing deployment assertions. Config domain 7 from the synthetic vectors is not approved deployment policy. CLI grammar was checked against installed near-cli-rs 0.30.1 help without reading a key or sending a transaction. No transaction or paid call is needed for local review and the tests above.

## Files

Fix round 1 changes only `contracts/attest/src/registry.rs`, `contracts/attest/tests/registry_sandbox.rs`, `docs/lanes/attest-registry.md`, `res/BUILD-attest.md` and `res/attest.source.sha256`. The manifest and lockfile retain the reviewed T1 configuration. The following inventory includes the earlier T1 implementation still awaiting the orchestrator's commit.

Changed existing files: `contracts/attest/Cargo.toml`, `contracts/attest/Cargo.lock` and `contracts/attest/src/lib.rs`.

New files: `contracts/attest/src/registry.rs`, `contracts/attest/src/replay.rs`, `contracts/attest/src/types.rs`, `contracts/attest/tests/registry_sandbox.rs`, `contracts/attest/tests/mock/Cargo.toml`, `contracts/attest/tests/mock/Cargo.lock`, `contracts/attest/tests/mock/src/lib.rs`, `contracts/attest/tests/test_deploy_script.py`, `scripts/deploy_attest.sh`, `res/BUILD-attest.md`, `res/attest.source.sha256` and this report. Existing verifier binaries, exact-byte fixtures and `.gitattributes` are unchanged. No new binary fixture needs an attribute rule.

## Verified sources

The immutable MPC interface and verifier and the pinned dcap-qvl quote and verification source files returned HTTP 200 and matched installed source bytes. Their actual DTOs and code, rather than a copied verifier schema or remembered API, were used.

| Fact | Primary source |
|---|---|
| Exact Borsh tuple, verified report variants, PPID and collateral date units | https://raw.githubusercontent.com/near/mpc/e691bdb98d68dcf74653da58d043453a6318765f/crates/tee-verifier-interface/src/lib.rs |
| Verifier method, claims and block clock | https://raw.githubusercontent.com/near/mpc/e691bdb98d68dcf74653da58d043453a6318765f/crates/tee-verifier/src/lib.rs |
| Event digest formula, selected RTMR3 replay, compose and provider binding | https://raw.githubusercontent.com/near/mpc/e691bdb98d68dcf74653da58d043453a6318765f/crates/attestation/src/attestation.rs |
| Event JSON fields | https://github.com/near/mpc/blob/3.18.0/crates/attestation-types/src/tcb_info.rs |
| Exact TD10 fields and attributes | https://raw.githubusercontent.com/Phala-Network/dcap-qvl/94208e2753a854191759e493ca5892be482ad453/src/quote.rs |
| Complete production attribute predicate | https://raw.githubusercontent.com/Phala-Network/dcap-qvl/94208e2753a854191759e493ca5892be482ad453/src/verify.rs |
| SDK deposit, storage, timestamp and bounded result functions | https://github.com/near/near-sdk-rs/blob/near-sdk-v5.29.1/near-sdk/src/environment/env.rs |
| SDK feature defaults and unconditional Promise host actions | https://raw.githubusercontent.com/near/near-sdk-rs/421ffb4265c2845c4cc1fb2dfdd638dcf67c712c/near-sdk/Cargo.toml and https://raw.githubusercontent.com/near/near-sdk-rs/421ffb4265c2845c4cc1fb2dfdd638dcf67c712c/near-sdk/src/promise.rs |
| Unconditional SDK host wrappers | https://raw.githubusercontent.com/near/near-sdk-rs/421ffb4265c2845c4cc1fb2dfdd638dcf67c712c/near-sdk/src/environment/env.rs |
| Direct near-sys declarations and features | https://raw.githubusercontent.com/near/near-sdk-rs/01c2bb002de4d4c3b379a39c92daa2886fc0e267/near-sys/src/lib.rs and https://raw.githubusercontent.com/near/near-sdk-rs/01c2bb002de4d4c3b379a39c92daa2886fc0e267/near-sys/Cargo.toml |
| Transitive near-sys feature manifest | https://raw.githubusercontent.com/near/near-sdk-rs/e19629c8e6381f67a77301379e850ad7b057dc32/near-sys/Cargo.toml |
| Sandbox receipt executors, raw result bytes and Borsh result decoding | https://raw.githubusercontent.com/near/near-workspaces-rs/534eebad02f7c78137b8a8ad8632ecd8be3b7a79/workspaces/src/result.rs |
| SDK self-call, deposit, initialization and no-argument wrappers | https://raw.githubusercontent.com/near/near-sdk-rs/421ffb4265c2845c4cc1fb2dfdd638dcf67c712c/near-sdk-macros/src/core_impl/code_generator/impl_item_method_info.rs |
| SDK extra argument refusal | https://raw.githubusercontent.com/near/near-sdk-rs/421ffb4265c2845c4cc1fb2dfdd638dcf67c712c/near-sdk-macros/src/core_impl/code_generator/attr_sig_info.rs |
| On-chain panic implementation and target selection require the near cfg | https://raw.githubusercontent.com/near/near-sdk-rs/e19629c8e6381f67a77301379e850ad7b057dc32/near-sdk-env/src/lib.rs and https://raw.githubusercontent.com/near/near-sdk-rs/e19629c8e6381f67a77301379e850ad7b057dc32/near-sdk-env/src/macros.rs |
| Independent receipts, callback failures and gas weights | https://docs.near.org/smart-contracts/anatomy/crosscontract |
| Retained strict Ed25519 verification | https://docs.rs/ed25519-dalek/2.2.0/src/ed25519_dalek/verifying.rs.html |
| Host ordinary verification is insufficient for strict policy | https://raw.githubusercontent.com/near/nearcore/bc4e9e8cfb03d9e82f027883055624e18576993e/runtime/near-vm-runner/src/logic/host.rs |
| Remote derive supports generated map decoding without duplicating event fields | https://serde.rs/remote-derive.html |
| Reproducible builds use committed source and pinned build configuration | https://github.com/near/cargo-near/blob/main/README.md |
| CLI deployment and transaction command grammar | https://docs.near.org/tools/cli |

Wire fields, report binding, time window, lease, gas and deposit values, renewal policy and admission limits are Siyab policy. They are not presented as upstream registry behavior.
