# Gate 0.3.0

22 November 2026, updated for fix round 1. Lane T3, branch `feat/gate-030`, base `c4899e190`. Changes remain in the working tree for independent review. No commit, push, remote change, tag change, public transaction, deployment, private key access or paid call was performed.

The frozen interface in `docs/spec/attest-v1.md` takes precedence over the design. The Gate root struct and every existing stored type remain unchanged. `GATEv1`, `TransferRequest`, `Verdict`, all 13 canonical fixtures and `tests/test_verdict.py` are unchanged. The crate and lockfile version are 0.3.0.

## Changes

The 21 changed files in the complete lane are:

- `contracts/gate/Cargo.toml` and `contracts/gate/Cargo.lock`
- `contracts/gate/src/attested.rs`, `contracts/gate/src/lib.rs`, `contracts/gate/src/verdict.rs`, `contracts/gate/src/policy.rs` and `contracts/gate/src/types.rs`
- `contracts/gate/tests/sandbox.rs`
- `contracts/test-attest-mock/Cargo.toml`, `contracts/test-attest-mock/Cargo.lock` and `contracts/test-attest-mock/src/lib.rs`
- `gate/attest.py` and `tests/test_attest.py`
- `.github/workflows/ci.yml` and `tests/test_citations.py`
- `README.md`, `docs/UPGRADES.md`, `docs/KEYS.md`, `docs/THREAT-MODEL.md` and `docs/RUNBOOK.md`
- `docs/lanes/gate-030.md`

Fix round 1 changes nine of those files: `contracts/gate/src/attested.rs`, the workflow, citation test, five shared documents and this report. The workflow adds `test-attest-mock` to the existing Rust unit matrix. README changes are line citations only. The other shared documents change line citations, plus the five prefix rows in `docs/UPGRADES.md`.

`contracts/gate/src/attested.rs` implements owner approved measurements, required platform pins, permissionless key synchronization and the four key cache. `contracts/gate/src/lib.rs` selects the attested verification path and adds the verifying raw public key as `verifier_key` in `transfer_scheduled`. `contracts/gate/src/verdict.rs` shares the existing field checks and digest construction with `verify_any`. `contracts/gate/src/policy.rs` disables the legacy setter in attested mode. New serialized types are appended to `contracts/gate/src/types.rs`; no existing type is changed.

The owner enables the mode once with exactly one yoctoNEAR. The initial measurement becomes active immediately because enabling the mode is the owner's explicit approval of that measurement and registry. Later measurements wait the configured timelock. Enabling requires 86400000 through 2592000000 milliseconds inclusive, or 24 hours through 30 days, on every network. Thirty days caps the owner's wait and prevents an oversized immutable delay from making every later measurement proposal and platform widening overflow. Both scheduling methods retain checked addition. No account suffix or caller supplied network name can weaken the 24 hour minimum. An existing measurement cannot be proposed again; revoke it first, then its new proposal waits the full timelock.

Platform lists must contain one through eight canonical, sorted, unique 16 byte PPIDs. `null`, an empty input, duplicates and unsorted input are refused. Removing a platform takes effect immediately. Adding one waits the timelock. A change containing both removes immediately to the old and new lists' intersection, then activates the requested list after the timelock. That intersection may be empty, which permits no verifier. A subsequent narrowing cancels a pending widening. Removed platforms lose their cached keys immediately.

`revoke_measurement` and `deny_verifier_key` remove matching cache entries immediately. Each callback and each payment checks current measurement activation, PPID policy, key denial and lease expiry. A measurement revoked while a sync is in flight cannot reenter the cache. `set_platforms` removes a PPID from that Gate; no global registry revocation is claimed.

Enabling the mode writes SHA256 of the exact ASCII text `siyab gate: attested mode, no operator verifier key` into the old current slot and clears the previous slot. `set_verifier_keys` then refuses with `REFUSED:attested_mode`. Attested verdicts require `key_id` to be `cur`. A payment tries at most four keys and records the one that verified. No eligible key gives `REFUSED:no_attested_verifier`; a signature matching none gives `REFUSED:bad_signature`.

Synchronization calls the pinned registry's `get_config` before its `get_key`, both with zero deposit and 10 Tgas. The private config callback has 30 Tgas so it can dispatch the second 10 Tgas view and 10 Tgas key callback. Every promise uses zero unused gas weight. Each response is bounded to 16384 bytes before JSON decoding. Missing, extra, duplicate, malformed or wrongly typed fields are refused. The callback recomputes the measurement and checks the complete issue window and issue based lease formula with checked addition, using immutable `max_lease_ms`. A new cache entry must already be locally eligible.

An identical cached view can be synced again. A renewal replaces that entry in place only with a strictly greater issue time, nondecreasing registration time and unchanged measurement, PPID and auxiliary digest. It evicts nothing. If the registry view instead has a changed auxiliary digest, the same timestamp, measurement and PPID checks apply before dropping the old cache identity and admitting the validated replacement as a new entry in that callback. The replacement remains the timestamp comparison record, so a delayed old callback cannot restore the old auxiliary binding. This is Gate cache synchronization, not registry renewal: the frozen registry `register_key` method and its `aux-mutation` vector still refuse changing that binding. A new public key can replace only an expired entry, including exactly at expiry. With four unexpired entries it returns false without a cache write. The cache contains at most four complete frozen key views.

## Prefix registry

These are raw Borsh values at the exact keys shown, outside `STATE`. There are no added fields in the root and no additional collection subprefixes. Fix round 1 adds these exact rows to `docs/UPGRADES.md`.

| Contract | Prefix | Collection |
|---|---|---|
| Gate | `at:m` | one `AttestedMode` value containing the pinned registry id and measurement timelock, from 0.3.0 |
| Gate | `at:s` | one vector of `ApprovedMeasurement` values containing measurement id, label and activation time, from 0.3.0 |
| Gate | `at:k` | one vector of at most four complete `AttestedKeyView` values, from 0.3.0 |
| Gate | `at:p` | one `PlatformPolicy` value containing current pins and an optional timelocked replacement, from 0.3.0 |
| Gate | `at:d` | one vector of permanently denied raw verifier public keys, from 0.3.0 |

The mode and initial policy are written by `enable_attested_mode`. The cache and denial vectors are absent until their first write; absence means an empty vector. The legacy artifact ignores all five keys.

## Client commands

`gate/attest.py` adds no dependency, signer, secret file reader or transaction path. Its two construction commands are offline:

```sh
python -m gate.attest measure measurement.json
python -m gate.attest sync-args "$ENCLAVE_PUBLIC_KEY_HEX"
```

`measurement.json` must be exactly the frozen six field object. The command hashes those fields; it does not authenticate an arbitrary quote. `sync-args` prints exactly the `public_key` object accepted by both Gate `sync_verifier` and registry `get_key`.

An offline snapshot contains exactly `attestation`, `registry_config`, `registry_keys` and `max_ttl_ms`. The first is the Gate's `get_attestation` result. The registry config has the frozen five fields. The registry keys and cache records each have the frozen eleven fields. The Gate view contains `enabled`, `registry_id`, `measurement_timelock_ms`, `measurements`, effective `platforms`, `pending_platforms`, `denied_keys` and `keys`. Measurement rows contain `measurement_id`, `label` and `active_from_ms`.

```sh
python -m gate.attest verify receipt.json --evidence evidence.json \
  --gate "$GATE_ACCOUNT_ID" --agent-pk "$AGENT_PUBLIC_KEY_HEX" \
  --snapshot snapshot.json --now-ms "$NOW_MS" \
  --registry "$ATTEST_REGISTRY_ID" --measurement "$MEASUREMENT_ID"
```

The evidence file supplies the exact signed bytes. It can be omitted only when the receipt embeds that exact UTF-8 evidence string. For explicit read only network verification, replace the snapshot and clock arguments with:

```sh
python -m gate.attest verify receipt.json --evidence evidence.json \
  --gate "$GATE_ACCOUNT_ID" --agent-pk "$AGENT_PUBLIC_KEY_HEX" \
  --live --rpc-url "$RPC_URL" \
  --registry "$ATTEST_REGISTRY_ID" --measurement "$MEASUREMENT_ID"
```

The live reader selects one final block and pins all Gate and registry reads to that height. It never uses an offline supplied clock in live mode. The verifier checks canonical evidence bytes, evidence hash, Gate and agent subject, seven verdict fields, rebuilt `GATEv1`, current cached and registry leases, immutable renewal identity and local pins and deny policy. At most four signatures are tried. Unsupported or uncheckable input produces UNKNOWN.

Successful CLI output has `verified: true` and `signed_verdict: ALLOW`, while `outcome` remains `UNKNOWN`, `payment_eligibility` remains `not checked` and `dcap_verified` is false. This authenticates the signed subject against the supplied policy inputs. It does not check pause, caps, receiver denylist, agent registration, replay state or settlement. A consumer must compare the signed receiver, token, amount and memo with its intended transfer. An offline file is not authenticated chain state, and an RPC answer is not a state proof.

Independent offline DCAP re-verification from a registration transaction, the optional fourth step in design section 4.7, is not implemented. The reader relies on the owner inspected and pinned registry deployment for verified TD10 attributes, report data, quote status and event replay. Owners must inspect the deployed code and its code replacement authority. A contract having the expected view methods alone establishes none of those properties. Advisory strings are retained, not silently treated as a refusal.

## Upgrade and recovery steps

This lane does not authorize or perform these steps. All public writes below are dry by default. The orchestrator must complete the source review, full suite and artifact records before the owner separately approves execution.

1. Review the prefix registry and citation corrections already applied in fix round 1. Inspect and pin the actual registry account and immutable code, approve the exact measurement id and PPIDs, and confirm a timelock from 24 hours through 30 days. T1 deployment and T2 hardware evidence remain separate work.
2. Pause the existing Gate with its owner. Stop verdict issuance and transfer senders. Wait for each scheduled transfer's terminal receipt and confirm its `get_pending` entry is absent. Freeze policy changes and record the old policy, agents, verifier slots, used verdicts, native and token balances and code hash externally. `GATE_STATE_FROZEN=1` is a script guard, not proof that receipts have drained.
3. Preserve the committed 0.2.2 `res/gate.wasm` and its verified checksum as `res/previous/gate.wasm` and its checksum when promoting the candidate. Update `res/BUILD-gate.md` with both main source commits and the new reproducible artifact and hash. Those files remain unchanged in this lane. The round trip loader recognizes the pinned 0.2.2 code hash in either location and verifies the matching checksum and runtime version. The original round trip accepts a checksum verified 0.2.1 or 0.2.2 baseline and checks that exact version again after rollback.
4. Build the reviewed main source with the pinned image using `cargo near build reproducible-wasm`, with `CARGO_TARGET_DIR` unset for that command. Save its artifact outside this worktree and set `GATE_CANDIDATE_WASM` to its absolute path. Run the tests below again with that variable so the exact deploy bytes exercise both round trips. This lane's passing tests use a source build, not a new reproducible release artifact.
5. Redeploy without an init call and without `--migrate`. Keep the Gate paused. Read `version`, code hash and all recorded old state back. Require version 0.3.0 and the candidate's exact code hash.
6. Call `enable_attested_mode` with the reviewed registry id, measurement id, a timelock from 86400000 through 2592000000 milliseconds and explicit PPIDs. Read `get_attestation` and `get_verifier_keys` back, verify the burned current slot and absent previous slot, then sync the enclave public key. Inspect the final sync result and fresh view before any unpause. An expired quote, pending measurement or wrong platform must not produce a usable key.
7. Unpause only after the owner reviews those readbacks. Subsequent `propose_measurement` calls wait the full timelock. `revoke_measurement`, `deny_verifier_key` and platform removal are immediate owner responses.

The following Bash blocks use the existing dry runner and near-cli-rs 0.30.1 syntax verified through its installed help. Export public ids, the exact candidate path and external key directory first. Each execution flag is off unless explicitly set to `1`. The upgrade flag requires steps 1 through 4 and the owner's approval to be complete.

```sh
export NETWORK=testnet GATE_STATE_FROZEN=1
export DRY_RUN=1
if [[ "${SIYAB_ATTEST_UPGRADE_EXECUTE:-0}" == "1" ]]; then export DRY_RUN=0; fi
source scripts/lib.sh
require_vars GATE_ACCOUNT_ID OWNER_ACCOUNT_ID NEAR_KEYS_DIR GATE_CANDIDATE_WASM
require_vars ATTEST_REGISTRY_ID MEASUREMENT_ID PLATFORMS_JSON ENCLAVE_PUBLIC_KEY_HEX

run near contract call-function as-transaction "$GATE_ACCOUNT_ID" set_paused \
  json-args '{"paused":true}' prepaid-gas '100 Tgas' attached-deposit '1 yoctoNEAR' \
  sign-as "$OWNER_ACCOUNT_ID" network-config testnet \
  sign-with-access-key-file "$NEAR_KEYS_DIR/$OWNER_ACCOUNT_ID.json" send

EXPECT_VERSION=0.3.0 EXPECT_CODE_HASH="$(python3 -m gate codehash "$GATE_CANDIDATE_WASM")" \
  bash scripts/redeploy.sh gate "$GATE_CANDIDATE_WASM"
run near contract call-function as-read-only "$GATE_ACCOUNT_ID" get_policy \
  json-args '{}' network-config testnet now
run near contract call-function as-read-only "$GATE_ACCOUNT_ID" get_verifier_keys \
  json-args '{}' network-config testnet now
```

Complete all step 5 readbacks and review the pinned registry and measurement before enabling the separate activation flag. The next block is also dry by default and uses the variables and runner initialized above.

```sh
export DRY_RUN=1
if [[ "${SIYAB_ATTEST_ENABLE_EXECUTE:-0}" == "1" ]]; then export DRY_RUN=0; fi

ENABLE_ARGS=$(python3 -c 'import json,os; print(json.dumps({"registry_id":os.environ["ATTEST_REGISTRY_ID"],"measurement_id":os.environ["MEASUREMENT_ID"],"measurement_timelock_ms":86400000,"platforms":json.loads(os.environ["PLATFORMS_JSON"])}))')
run near contract call-function as-transaction "$GATE_ACCOUNT_ID" enable_attested_mode \
  json-args "$ENABLE_ARGS" prepaid-gas '100 Tgas' attached-deposit '1 yoctoNEAR' \
  sign-as "$OWNER_ACCOUNT_ID" network-config testnet \
  sign-with-access-key-file "$NEAR_KEYS_DIR/$OWNER_ACCOUNT_ID.json" send

SYNC_ARGS=$(python3 -m gate.attest sync-args "$ENCLAVE_PUBLIC_KEY_HEX")
run near contract call-function as-transaction "$GATE_ACCOUNT_ID" sync_verifier \
  json-args "$SYNC_ARGS" prepaid-gas '100 Tgas' attached-deposit '0 NEAR' \
  sign-as "$OWNER_ACCOUNT_ID" network-config testnet \
  sign-with-access-key-file "$NEAR_KEYS_DIR/$OWNER_ACCOUNT_ID.json" send
run near contract call-function as-read-only "$GATE_ACCOUNT_ID" get_attestation \
  json-args '{}' network-config testnet now
```

These blocks leave the Gate paused. They are a testnet procedure; `scripts/redeploy.sh` refuses mainnet. No mainnet Gate deployment path is added. Pause and receipt draining must complete before a real redeploy even though the dry block prints the calls consecutively. Only after step 6 readbacks and owner review may the owner run the separate unpause block:

```sh
export DRY_RUN=1
if [[ "${SIYAB_ATTEST_UNPAUSE_EXECUTE:-0}" == "1" ]]; then export DRY_RUN=0; fi
run near contract call-function as-transaction "$GATE_ACCOUNT_ID" set_paused \
  json-args '{"paused":false}' prepaid-gas '100 Tgas' attached-deposit '1 yoctoNEAR' \
  sign-as "$OWNER_ACCOUNT_ID" network-config testnet \
  sign-with-access-key-file "$NEAR_KEYS_DIR/$OWNER_ACCOUNT_ID.json" send
```

Before enabling the mode, rollback is the checksum verified previous artifact redeployed without an init call, followed by old state readbacks while paused. After enabling, the same layout rollback preserves the burned old slot and cleared previous slot, so old operator signatures are refused. The old artifact does not enforce attested policy, and its owner could deliberately call its legacy setter to restore operator trust. Keep a rollback paused. Reupgrading to 0.3.0 reads the retained attested prefixes again. There is no rollback claim that removes owner authority. The owner still controls withdraw, pause and caps; a testnet FullAccess key can still replace code. The mainnet key policy in `docs/KEYS.md` still applies.

After preserving 0.2.2 under `res/previous/`, the exact rollback command is below. Its separate execution flag remains off by default. Run the owner pause command first and drain receipts as above.

```sh
export DRY_RUN=1
if [[ "${SIYAB_ATTEST_ROLLBACK_EXECUTE:-0}" == "1" ]]; then export DRY_RUN=0; fi
EXPECT_VERSION=0.2.2 EXPECT_CODE_HASH=5EmM2UQRYcvaNvJX6jfcgtNS7Mm8ZPjZhwK1J8bMXrcu \
  bash scripts/redeploy.sh gate res/previous/gate.wasm
```

## Validation

Linux commands mirror CI's unit and sandbox entry points. Locally they ran in WSL Ubuntu with Rust 1.99.1, cargo-near 0.22.0, near-sdk 5.29.1 and near-workspaces 0.23.0. Targets stayed outside the repository; Cargo build concurrency was two and sandbox tests ran serially.

```sh
export CARGO_TARGET_DIR="$HOME/target/siyab-attest-t3-gate"
export CARGO_BUILD_JOBS=2 RUST_TEST_THREADS=1 CARGO_NET_OFFLINE=true
cargo +1.99.1 test --manifest-path contracts/gate/Cargo.toml --lib --locked --offline
cargo +1.99.1 test --manifest-path contracts/gate/Cargo.toml --test sandbox --locked --offline -- --nocapture
cargo +1.99.1 test --manifest-path contracts/test-attest-mock/Cargo.toml --lib --locked --offline
"$HOME/.venvs/near/bin/python" -m unittest tests.test_attest tests.test_verdict tests.test_attest_vectors tests.test_citations -v
python scripts/check_hygiene.py --all
python scripts/check_citations.py
rustfmt +1.99.1 --edition 2021 --check contracts/gate/src/attested.rs
git diff --check
```

The new mock registry is explicitly test only. It exposes the frozen `get_config` and `get_key` interfaces, with a self call setter for the sandbox to supply both valid and malformed records. It verifies no quote and is not a production registry. Sandbox signing uses published synthetic test material only.

| Check | Final result |
|---|---|
| Gate `cargo test --lib --locked --offline` | 105 passed, 0 failed, 0 ignored |
| Gate `cargo test --test sandbox --locked --offline -- --nocapture` | 4 passed together, 0 failed, 0 ignored, 179.26 seconds |
| Mock `cargo test --lib --locked --offline` | 3 passed, 0 failed, 0 ignored |
| `tests.test_attest` | 43 passed, 0 skipped |
| Untouched `tests.test_verdict`, existing NEAR venv in WSL | 53 passed, 0 skipped; PyNaCl cross check executed |
| Untouched `tests.test_attest_vectors` | 47 passed, 0 skipped |
| `tests.test_citations` | 11 passed, 0 skipped |
| Combined Python command above | 154 passed, 0 failed, 0 skipped, 88.837 seconds |
| `python scripts/check_hygiene.py --all` | Exit 0 |
| `python scripts/check_citations.py` | Exit 0, no findings |
| Direct `rustfmt` check above | Exit 0 |
| `git diff --check` | Exit 0 |

Fix round 1 covers 266 distinct tests, all passing with zero skips. Repeated focused runs are not counted twice. Six unit tests were added, and the existing expiry replacement test now requires refusal when all remaining entries are live. Before the production fix, the focused attestation run had 29 tests with 24 passing and five failing across changed auxiliary data, full cache refusal, expiry replacement and the two oversized timelocks. After the fix all 29 passed, followed by the full requested targeted suites above. The initial citation run had 11 tests with two failing subtests that expected the old callback and withdrawal ranges; both expectations now match the verified source and the final 11 pass. The prior round's four owner command blocks were dry executed successfully with every execution flag off. Their transaction commands are unchanged and were not executed in this fix round.

Both legacy and enabled mode round trips deploy the checksum verified 0.2.2 artifact, initialize and write state, redeploy 0.3.0 without initialization, read old state and exercise the new code, then redeploy 0.2.2 without initialization and read state again. The enabled case also rejects old current and previous signer verdicts, accepts a synthetic enclave verdict in 0.3.0, and confirms retained attested state after reupgrading. Other sandbox controls refuse expired, revoked, denied, wrong platform, unapproved and timelock pending records and malformed key views without scheduled payment, replay consumption or cap debit. Unit tests cover one, two and four signing keys, fifth and sixth key refusal without signer displacement, replacement exactly at expiry, auxiliary replacement and stale callback ordering, exact issue time edges and overflow, and platform transition boundaries. The maximum accepted timelock test schedules both a new measurement and a platform widening.

Independent read only review of the final cache, timelock and regressions found no blocking issue. The final mock and sandbox are source tests, not a hardware attestation or public network demonstration. A diff against HEAD confirmed the frozen spec and shared vectors, all 13 canonical fixtures, `tests/test_verdict.py` and the protected files named in the common lane rules unchanged. The Python reader and its 43 tests are unchanged this fix round. The full repository suite is left to the orchestrator as requested.

## Citation audit and remaining owner work

Fix round 1 permits the five shared documentation files listed above. A throwaway script compared every numeric Gate Rust citation range in those documents, including shorthand and comma separated ranges, against its text at `git merge-base HEAD origin/main`, which is `f3e0bba389a4616682189955ddd2601cedc7fb4d`, and then against the working source. The audit checked 109 range occurrences and changed 86. The script was removed. The four previously reported `docs/KEYS.md` corrections and every unflagged shifted Gate range are applied.

The semantic comparison also caught ranges that a line offset alone would not fix. Legacy key selection is now cited at `contracts/gate/src/verdict.rs:94-105`, signature refusal at `contracts/gate/src/verdict.rs:108-109`, the expanded legacy and attested verification branches at `contracts/gate/src/lib.rs:200-236`, the transfer callback at `contracts/gate/src/lib.rs:348-385`, and owner withdrawal at `contracts/gate/src/policy.rs:232-245`. The two citation regression expectations now follow the callback and withdrawal ranges. The runbook's reference to the layout preserving redeploy commands is corrected to `docs/UPGRADES.md:63-64` after adding the prefix rows.

The owner has no transaction, spending or paid call to run for this fix round. The orchestrator retains independent review, the full repository suite and any commit. Release artifact production and the separately approved deployment, enable and unpause steps remain the owner procedure above. Hardware and deployment evidence remain with T1 and T2. Hosted CI is not triggered because the branch is not pushed; the mock's tests run locally and are now in the CI matrix.

The changed `attested.rs` passes its direct Rust 1.99.1 formatting check. The optional whole crate `cargo fmt -- --check` returns 1 on inherited formatting differences in `lib.rs`, `policy.rs` and `verdict.rs`. Those files are unchanged in this fix round; applying unrelated formatting would move the citations just audited. No required check is waived by that formatting observation.

## Verified primary sources

The frozen spec defines the Siyab hashes, exact JSON shapes and lease policy. External behavior was checked against the pinned installed source, official upstream source or installed CLI help, not inferred from method names.

Fix round 1 rechecked the pinned SDK's millisecond floor and NEAR's independent asynchronous callback behavior against sources 1 and 3 below. Current policy must be checked when a callback consumes a view because other calls can run between dispatch and completion. The 30 day maximum and cache admission policy are local application choices verified by the regression tests, not upstream protocol limits. No external API shape changes are introduced.

1. SDK 5.29.1 block timestamp floor, bounded promise results, storage operations and Ed25519 host delegation: https://raw.githubusercontent.com/near/near-sdk-rs/near-sdk-v5.29.1/near-sdk/src/environment/env.rs
2. SDK promise gas weights and function call API: https://raw.githubusercontent.com/near/near-sdk-rs/near-sdk-v5.29.1/near-sdk/src/promise.rs
3. Independent asynchronous receipts and callback behavior: https://docs.near.org/smart-contracts/anatomy/crosscontract
4. NEAR view query arguments, finality or block id and returned byte array: https://docs.near.org/api/rpc/contracts#call-a-contract-function
5. Final block header height and timestamp fields: https://docs.near.org/api/rpc/block-chunk#block-details
6. Pinned upstream report types, TCB status, PPID and collateral date units: https://github.com/near/mpc/blob/3.18.0/crates/tee-verifier-interface/src/lib.rs
7. Strict Ed25519 small order rejection used by the Python reader: https://docs.rs/ed25519-dalek/2.2.0/src/ed25519_dalek/verifying.rs.html
8. Exact near-workspaces 0.23.0 source for sandbox, views and block advancement: https://raw.githubusercontent.com/near/near-workspaces-rs/534eebad02f7c78137b8a8ad8632ecd8be3b7a79/workspaces/src/worker/impls.rs
9. Exact near-workspaces source build helper: https://raw.githubusercontent.com/near/near-workspaces-rs/534eebad02f7c78137b8a8ad8632ecd8be3b7a79/workspaces/src/cargo/mod.rs
10. Signer targeted gas refund receipts, used to isolate the sandbox payee balance: https://github.com/near/nearcore/blob/master/docs/RuntimeSpec/Refunds.md and https://near.github.io/nearcore/architecture/how/gas.html
