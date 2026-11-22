# Attestation T0

21 November 2026, updated for fix round 4 on 22 November 2026. Worktree `siyab-attest-t0`, branch `spike/attest-t0`, base `b1019108b`. Changes are local and uncommitted. T1, T2 and T3 were not started.

Use the existing locked NEAR verifier and the `tee-verifier-interface` git dependency at tag 3.18.0 (Recommended). The deployed verifier accepted every retained dstack CPU quote in the sandbox. A general mainnet registry that could replace T1 was not established by the prior art search.

## Deployed code

Read-only final RPC queries fetched `tee-verifier-2026-11-11.near` with `view_code` and inspected both it and `tee-verifier-2026-11-02.testnet` with `view_account` and `view_access_key_list`. The wasm is 422327 bytes and matches the requested SHA256, retained in `fixtures/attest/deployment.json`. Both accounts reported code hash `8SrqVCh3RtHz6mG2eiMwBGZDPkmccA5JiVvqrDYT3VL3` and zero access keys. Each account reported storage usage of 422427 bytes. The capture includes request parameters, final block hashes, heights and recording times.

Sources: https://rpc.mainnet.near.org and https://rpc.testnet.near.org. RPC request definitions: https://docs.near.org/api/rpc/contracts and https://docs.near.org/api/rpc/access-keys.

The unchanged mainnet bytes are in `contracts/attest/tests/vendor/tee-verifier-mainnet.wasm`. The MIT notice points to the complete copied upstream license. The directory and Borsh inputs have `-text` attributes. `fixtures/attest/wasm-interfaces.json` records the binary export inspection: the deployed and two pinned artifacts export both verifier methods. The deployed build also exports `__contract_abi`; the test builds omit the ABI feature. The successful Borsh calls below establish compatibility of the method used by this spike, rather than equivalence of every export or deployed build provenance.

Sources: https://github.com/near/mpc/blob/3.18.0/LICENSE, https://github.com/near/mpc/blob/3.18.0/crates/tee-verifier/src/lib.rs and https://webassembly.github.io/spec/core/binary/modules.html#export-section.

## Verifier proof and gas

`fixtures/attest/sandbox-run-20261121.json` retains 76 structured observation events from the passing local run, including arguments hashes, returned statuses, errors, PPIDs, collateral expiration and receipt gas. These observations were made in an isolated near-workspaces 0.23.0 sandbox, without a mainnet or testnet transaction.

| Artifact and input | Result | Verifier receipt gas |
|---|---|---:|
| Unchanged mainnet wasm, all 16 retained NEAR AI gateway, model and manager CPU quotes | All `Verified`, `TD10`, `UpToDate` | 125070104515218 through 125366911224830 |
| Pinned test wasm, the same 16 quotes at Unix 1795274641 | All `Verified`, `TD10`, `UpToDate` | 124306915248368 through 124627161453664 |
| Pinned test wasm, near/mpc's own quote at Unix 1786622400 | `Verified`, `TD10`, `UpToDate` | 124655934083040 |
| Mainnet wasm, one flipped report byte at offset 60 | `Rejected` | 123108746479539 |
| Pinned NEAR AI wasm, one flipped report byte at offset 60 | `Rejected` | 122550977458185 |
| Pinned upstream wasm, one flipped report byte at offset 60 | `Rejected` | 122583462849561 |
| Pinned NEAR AI wasm, PCK chain from another machine | `Rejected` | 109679885649613 |

All three flipped report controls returned `dcap verification failed: ISV enclave report signature is invalid`. The wrong machine chain returned `dcap verification failed: Signature is invalid for qe_report in quote`. Positive NEAR AI calls use `pck_certificate_chain: None`, so the verifier uses each quote's embedded chain. The existing recordings were not changed. Their committed source and quote hashes are checked before use.

The dated mainnet proof consumed 125421223632190 through 125720885629520 gas including its transaction overhead. This is sandbox gas for the actual deployed bytes, not a claim about measured public network latency or fees. Each direct call prepaid 200 Tgas.

Sources for inputs and conversion rules: https://github.com/near/mpc/blob/3.18.0/crates/tee-verifier/tests/verify_quote.rs, https://github.com/near/mpc/blob/3.18.0/crates/test-utils/src/attestation.rs and https://github.com/near/mpc/blob/3.18.0/crates/tee-verifier-interface/src/lib.rs. NEAR AI public report source URLs and original recording times are retained in the four `fixtures/nearai/attestation-*.capture.json` files. The quote inventory combines the existing 14 row `fixtures/nearai/crosscheck-results.json` with the two separate gateway captures cataloged in `fixtures/attest/additional-quotes.json`. Their embedded PCK chains match those of the already cataloged gateway quotes. The public API is described at https://docs.near.ai/cloud/verification.

The kill criterion did not fire: the unchanged live verifier artifact accepted all 16 same-day recorded dstack quotes. No dcap-qvl 0.7.1 fallback was linked. A fresh owner-hosted Siyab quote, its image policy and production registry behavior remain outside T0. DCAP acceptance alone does not authorize an enclave key, establish application measurements or produce an ALLOW.

## Clock and durable replay

The upstream fixture's valid time is Unix 1786622400, and its earliest collateral expiration is Unix 1789171031. It is already expired at present block time. The two test-only builds use tag 3.18.0 with default features disabled, so `block-clock` is off, and set `TEE_VERIFIER_PINNED_NOW_SECONDS` to 1786622400 and 1795274641 respectively. Their names and metadata state TEST-ONLY. They must never be deployed to a public network or used as freshness evidence.

The NEAR AI fixtures expire at Unix 1797862926, 21 December 2026 at 14:22:06 UTC. All 32 successful pinned and dated NEAR AI verifier calls returned that same earliest expiration. `fixtures/attest/clock.json` freezes the boundary. The dated mainnet test now has an `#[ignore]` gate with that deadline in its reason, so an ordinary run reports it as ignored both before and after expiry. It must be selected explicitly before expiry using the command below. If forced at or after expiry, it fails before starting a sandbox instead of passing without verification. The two pinned tests continue to verify the recordings. The retained November run remains the dated proof.

Source for Rust's ignored test reporting: https://doc.rust-lang.org/reference/attributes/testing.html#the-ignore-attribute.

Sources: https://github.com/near/mpc/blob/3.18.0/crates/tee-verifier/src/lib.rs, https://github.com/near/mpc/blob/3.18.0/crates/test-utils/src/attestation.rs and https://github.com/near/mpc/blob/3.18.0/crates/tee-verifier/tests/verify_quote.rs. The retained Intel QE identity sets the NEAR AI boundary. The official endpoint was independently read without credentials and returned HTTP 200: https://api.trustedservices.intel.com/tdx/certification/v4/qe/identity. Its current response has a later issue time; it does not replace the retained collateral.

The original upstream workspace build began fetching unrelated nearcore dependencies during Cargo resolution and was stopped. The local build isolated the unchanged verifier, conversions and interface source into a three crate workspace, removed only their test dependencies from scratch manifests and retained the exact upstream relevant lock identities. The 329 package minimal lock uses dcap-qvl 0.7.0, ring 0.17.14, near-sdk 5.30.0 and Borsh 1.8.1. Rust 1.99.1, clang 18 and the upstream `release-contract` profile were used, with `opt-level = "z"`, fat LTO and one codegen unit. Both test wasm files are 499006 bytes. They are local test builds, not reproductions of the deployed mainnet release binary.

`contracts/attest/tests/vendor/builds.json` records immutable source URLs, source and archive hashes, tools, profile, exact clock pins, the isolated manifest and lock hashes and resulting wasm hashes. The retained `TEST-ONLY-Cargo.toml` and `TEST-ONLY-Cargo.lock` plus `contracts/attest/tests/build_vendor.py` rebuild the two test artifacts from a checked immutable archive. Both repeated locked builds matched the retained bytes. Cargo targets stay outside the repository.

Sources: https://github.com/near/mpc/blob/3.18.0/Cargo.toml, https://github.com/near/mpc/blob/3.18.0/Cargo.lock and https://github.com/near/mpc/blob/3.18.0/crates/tee-verifier/Cargo.toml. Immutable source: https://github.com/near/mpc/tree/e691bdb98d68dcf74653da58d043453a6318765f.

## Interface choice and frozen formats

The git dependency was tried first and passed a one test Borsh round trip under the Gate's pinned Rust 1.99.1 toolchain. It is declared at tag 3.18.0 and locked to revision `e691bdb98d68dcf74653da58d043453a6318765f`. The final crate and callback probe both compile and use it. This keeps the upstream Borsh field and enum order without maintaining a second DTO copy. Initial duplicate online Cargo resolution stalled; the cached git dependency then compiled with `--offline`. This was a network resolution issue, not a toolchain incompatibility.

Source: https://github.com/near/mpc/blob/3.18.0/crates/tee-verifier-interface/Cargo.toml and https://github.com/near/mpc/blob/3.18.0/crates/tee-verifier-interface/src/lib.rs.

`docs/spec/attest-v1.md` freezes report data, measurement id, `AttestedKeyView`, immutable registry config and call arguments, voice scope, enclave vote intent and the NEP-413 vote digest. `fixtures/attest/vectors.json` contains 137 synthetic cases, reproduced independently by the Rust and Python references. The Python file reuses the repository's standard library Ed25519 implementation. The Rust crate contains format types, hash helpers, validation and pure renewal and vote reservation transitions. Hash helpers assume callers have performed the boundary validation specified in the spec. It is not a deployable registry.

The design left the vote fields' framing unspecified. T0 fixes a domain plus Borsh tuple that binds the registry, enclave key, measurement and sorted unique platform set alongside every vote field. The TLS auxiliary input is explicitly DER SubjectPublicKeyInfo. These are Siyab v1 choices, not claims about an existing external API. Exact TD10 is required; `as_td10()` alone would accept broader report variants. The key provider digest is the 48 byte event digest, not a 32 byte payload hash. These distinctions are covered in the spec and vectors.

Sources: https://github.com/near/mpc/blob/3.18.0/crates/tee-verifier-interface/src/lib.rs, https://github.com/near/mpc/blob/3.18.0/crates/attestation/src/attestation.rs, https://borsh.io/, https://github.com/near/NEPs/blob/master/neps/nep-0413.md, https://www.rfc-editor.org/rfc/rfc5280#section-4.1.2.7 and https://raw.githubusercontent.com/out-layer/api-spec/main/openapi.yaml.

## Fix round 1

The spec now requires an independent check of the verified TD10 report's eight little endian TD attributes bytes before deriving a measurement or storing a key. DEBUG bit 0, profiling bits 4 through 6 and MIGRATABLE bit 29 must all be clear. These checks apply even if the selected verifier already enforces them, including a future T1 build that links dcap-qvl directly. They do not accept a caller's attributes assertion in place of the verified report.

The bit positions and current production defaults were checked against the installed dcap-qvl 0.7.0 source and the identical immutable upstream files, both returned with HTTP 200. Sources: https://github.com/Phala-Network/dcap-qvl/blob/94208e2753a854191759e493ca5892be482ad453/src/quote.rs#L213-L241, https://github.com/Phala-Network/dcap-qvl/blob/94208e2753a854191759e493ca5892be482ad453/src/verify.rs#L1645-L1666 and https://github.com/Phala-Network/dcap-qvl/blob/94208e2753a854191759e493ca5892be482ad453/src/verify.rs#L287-L337.

The Python reference checks the attributes independently of the DCAP library. Its DEBUG regression starts from the retained version 4 TDX quote, reads TD attributes from its TD report and sets DEBUG in a copy. The positive attributes pass and the changed attributes are refused. This tests the policy predicate, not the signature validity of a fabricated debug quote. Further cases refuse each profiling bit, migration and malformed attribute widths. The quote header and TD report layout were checked in the same source: https://github.com/Phala-Network/dcap-qvl/blob/94208e2753a854191759e493ca5892be482ad453/src/quote.rs#L88-L98 and https://github.com/Phala-Network/dcap-qvl/blob/94208e2753a854191759e493ca5892be482ad453/src/quote.rs#L287-L315.

The lease now expires at the minimum of `issued_at_ms + max_lease_ms` and collateral expiration, with checked u64 addition. Registration never moves that endpoint. The shared key vector retains its 24 hour policy outside the eleven field view and now expires at Unix millisecond 1795305600000, 1000 milliseconds earlier than the prior vector. Both language references enforce the formula. Regression tests cover delayed registration, attempted extension, collateral capping, registration at expiry, malformed policy integers and u64 overflow. The new regressions were run before the fix and exposed the old vector and missing checks, then passed after the fix.

All three flipped report controls now require the complete rejection string `dcap verification failed: ISV enclave report signature is invalid`. A different refusal reason no longer satisfies them. No verifier artifact, quote recording or deployment was changed.

## Fix round 2

The issue bound is now explicit and enforced in both references: `registered_ms - 7_200_000 <= issued_at_ms <= registered_ms`, with both edges inclusive and SKEW zero. Future issue times receive no allowance. The lower bound clamps to zero for u64 arithmetic. Registration uses the acceptance callback's nanosecond block timestamp divided by 1000000 with floor rounding. The nine issue vectors cover both edges, one millisecond outside each edge, a ten day future issue time, epoch boundaries and checked lease overflow. Seven further vectors freeze block clock rounding, including a fractional millisecond and maximum u64 nanoseconds.

The independent TD10 predicate now matches all of dcap-qvl 0.7.0 production `validate_td10`: DEBUG bit 0, profiling bits 4 through 6, MIGRATABLE bit 29 and SERVTD_EXT bit 17 clear; SEPT_VE_DISABLE bit 28 set; reserved bits 1 through 3, 7 through 15, 23 through 26 and 32 through 61 clear. The reserved mask is `0x3fffffff0780ff8e`. Bits left unrestricted by that exact predicate remain unrestricted here. Both references consume 66 shared attribute vectors, including every single bit and the combination of all permitted bits. Sandbox quote controls also apply the Rust predicate to the verified TD10 field, independently of the verifier's internal policy.

The offset is 120 bytes into TD10 and byte 168 of a version 4 quote. Version 5 adds a six byte body header and places the same field at byte 174. The retained mutated quote test uses version 4. Reading the authenticated report field avoids applying a version 4 absolute offset to a different wrapper.

The first two new regression methods were run before the predicate fixes. They failed on 51 subtests: 48 unchecked TD attribute cases and three issue time cases. The same methods pass after the fixes. The larger shared set now has 110 cases; both languages reproduce every group.

The remaining interface choices are frozen as follows:

| Interface | Choice |
|---|---|
| `get_key` | Exactly one `public_key` argument, raw 32 byte Ed25519 key in lowercase hexadecimal |
| `register_key` | Exactly `public_key`, `issued_at_ms`, `aux`, `verification_args`, `app_compose`, `event_log` |
| `verification_args` | Lowercase hexadecimal of the exact upstream Borsh `(QuoteBytes, Collateral)` tuple, with malformed or trailing data refused |
| `submit_vote` | Exactly `intent` containing the existing twelve fields, plus the 64 byte enclave `signature` as 128 lowercase hexadecimal characters |
| `vote_path` | Registry derived 69 byte ASCII string passed unchanged as Chain Signatures `request.path`, with registry predecessor and a configured Ed25519 domain |
| Signing payload | `request.payload_v2.Eddsa` carries the hexadecimal encoding of the 32 byte NEP-413 digest |
| Vote time | `key.registered_ms <= now_ms < intent.expires_ms <= key.expires_ms`, checked before dispatch and before returning asynchronous success |

The event JSON fields and their integer and hexadecimal encodings are explicit in the spec. Registration obtains measurement, PPID, status and accepted times from verified evidence and policy; none are caller supplied. New byte vectors include the `get_key` JSON, both optional PCK chain tuple encodings, exact UTF-8 compose bytes, vote submission envelopes and signing paths and requests. Rust additionally proves trailing Borsh input is refused. Domain 7, the registration evidence and the enclave signatures in these cases are synthetic. They do not assert a deployed Ed25519 domain, valid quote or verified signature. Intent expiry gates registry processing and cannot revoke a previously released NEP-413 signature, whose payload has no expiry field.

The source review checked the installed dcap-qvl 0.7.0 package's source revision and compared fresh immutable `quote.rs` and `verify.rs` responses byte for byte with those installed files. Both returned HTTP 200 and matched. The block clock conversion was verified against the official near-sdk 5.29.1 source, which also returned HTTP 200. The upstream MPC request and event types, method and key derivation were checked against the pinned source. Method names and registry JSON envelopes are explicit Siyab choices.

Sources verified for this freeze:

1. TD bit layout, reserved mask and quote offsets: https://github.com/Phala-Network/dcap-qvl/blob/94208e2753a854191759e493ca5892be482ad453/src/quote.rs
2. Complete production TD10 check: https://github.com/Phala-Network/dcap-qvl/blob/94208e2753a854191759e493ca5892be482ad453/src/verify.rs#L1645-L1690
3. Block timestamp floor conversion: https://github.com/near/near-sdk-rs/blob/near-sdk-v5.29.1/near-sdk/src/environment/env.rs
4. Borsh verifier inputs and method: https://github.com/near/mpc/blob/e691bdb98d68dcf74653da58d043453a6318765f/crates/tee-verifier-interface/src/lib.rs and https://github.com/near/mpc/blob/e691bdb98d68dcf74653da58d043453a6318765f/crates/tee-verifier/src/lib.rs
5. Registration event fields: https://github.com/near/mpc/blob/e691bdb98d68dcf74653da58d043453a6318765f/crates/attestation-types/src/tcb_info.rs
6. Signing request JSON and payload encoding: https://github.com/near/mpc/blob/e691bdb98d68dcf74653da58d043453a6318765f/crates/near-mpc-crypto-types/src/sign.rs and https://github.com/near/mpc/blob/e691bdb98d68dcf74653da58d043453a6318765f/crates/near-mpc-crypto-types/src/primitives.rs
7. Signing predecessor and public key derivation: https://github.com/near/mpc/blob/e691bdb98d68dcf74653da58d043453a6318765f/crates/contract/src/api/sign.rs and https://github.com/near/mpc/blob/e691bdb98d68dcf74653da58d043453a6318765f/crates/contract/src/api/keys.rs

## Fix round 3

The spec supersedes the design's `register` and `sign_vote` signatures. Existing keys renew only with a strictly greater issue time and unchanged measurement, PPID and auxiliary hash. The comparison is against the current record in the verifier callback, including an expired record. Equal or older quotes are refused even within the two hour issue window. Refusal preserves the stored record. The shared renewal cases and both reference state transitions cover these rules and both completion orders of two in flight registrations. The changed measurement case is a self consistent different measurement, so the renewal identity check must refuse it.

The eleven field key view stays unchanged. The exact five field, immutable `new` and `get_config` JSON now exposes `max_lease_ms`, verifier, signer, Ed25519 domain and a bounded, sorted, unique `vote_recipients` allowlist. Initialization is one time and self call only, with no owner retained. The spec lists every check possible from the view with the Gate's clock and local pins, and distinguishes the lease formula that needs config from quote and replay checks that require the registry's evidence. A claimed ownerless deployment must also have no code replacement access key.

Vote authentication reconstructs the digest with the registry's own account and verifies the enclave signature with `intent.public_key` before eligibility, reservation or MPC dispatch. Actual synthetic test signatures exercise this order; malformed or small order signatures fail. The recipient names the OutLayer verifying deployment, `outlayer.near` for the intended mainnet integration, and must be explicitly allowed. Round 3 used a registry wide reservation for `(approval_id, wallet_pubkey, request_hash)`; fix round 4 below replaces that rule with a per scope reservation. An ingress reservation prevents concurrent opposite requests and remains after failed signing or an expired callback, because a signature may already be visible in the MPC receipt. Same verb retries still pass every eligibility check.

Callback refusal now means no registry success result or success event; it cannot conceal or revoke a signature exposed by MPC. The registry has no owner and no global revocation. Revoke and deny policy live in each Gate, and a wallet owner can remove its own pinned voice. No global revoke method or production Gate change was added. Per the request, only design section 5.2 was read this round; no claim about unread design sections is needed for these rules.

Both public write methods have exact 0.01 NEAR deposits and 300 Tgas call budgets, with 200 Tgas to the verifier or signer and 60 Tgas to the private callback. Those are v1 policy choices, not a complete T1 benchmark. Checked positive storage charges cannot exceed the caller's deposit. The verifier gets zero deposit; MPC gets 1 yoctoNEAR. Refunds name the original relayer, and the spec does not promise a refund after a runtime or callback failure. `register_key` resolves to an accepted key view or `null`; `submit_vote` resolves to exactly the path, NEP-413 digest and lowercase hexadecimal signature or `null`. Upstream MPC returns `scheme: Ed25519` with a 64 integer signature array, which the future registry must decode; `Eddsa` names its request variant, not its response. The config, budgets and successful and refused result shapes have shared vectors.

The u64 overflow vector now sets issue and registration time to maximum u64 minus one, expiry and collateral expiry to maximum u64, and lease to two milliseconds. All other time predicates pass; only checked lease addition fails. Six synthetic quote prefix vectors cover version 4 and version 5 offsets, DEBUG refusal, a DEBUG decoy at the old version 4 offset and a refused version 5 non TD10 body. They test offset extraction and the independent attributes predicate, not quote signature acceptance. The original byte format hashes are retained.

Sources checked for this round:

1. Quote version 5 body header, TD10 layout and attribute offsets: https://github.com/Phala-Network/dcap-qvl/blob/94208e2753a854191759e493ca5892be482ad453/src/quote.rs
2. Verifier Borsh method, result variants and collateral dates: https://github.com/near/mpc/blob/e691bdb98d68dcf74653da58d043453a6318765f/crates/tee-verifier/src/lib.rs and https://github.com/near/mpc/blob/e691bdb98d68dcf74653da58d043453a6318765f/crates/tee-verifier-interface/src/lib.rs
3. MPC signing deposit and configurable gas preconditions: https://github.com/near/mpc/blob/e691bdb98d68dcf74653da58d043453a6318765f/crates/near-mpc-contract-interface/src/deposits.rs and https://github.com/near/mpc/blob/e691bdb98d68dcf74653da58d043453a6318765f/crates/contract/src/api/common.rs
4. Ed25519 response JSON: https://github.com/near/mpc/blob/e691bdb98d68dcf74653da58d043453a6318765f/crates/near-mpc-crypto-types/src/primitives.rs
5. Storage, deposit and block time units: https://github.com/near/near-sdk-rs/blob/near-sdk-v5.29.1/near-sdk/src/environment/env.rs
6. Empty initializer return and promise return behavior: https://github.com/near/near-sdk-rs/blob/near-sdk-v5.29.1/near-sdk-macros/src/core_impl/code_generator/snapshots/simple_init.snap and https://github.com/near/near-sdk-rs/blob/near-sdk-v5.29.1/near-sdk/src/promise.rs
7. Independent receipt execution and callback failure: https://docs.near.org/smart-contracts/anatomy/crosscontract
8. Strict Ed25519 public key, R point and signature checks at the installed 2.2.0 version: https://docs.rs/ed25519-dalek/2.2.0/src/ed25519_dalek/verifying.rs.html
9. OutLayer's verifying deployment id and NEP-413 payload: https://raw.githubusercontent.com/out-layer/api-spec/main/openapi.yaml and https://github.com/near/NEPs/blob/master/neps/nep-0413.md

The pinned dependency and official source were read before adding the direct `ed25519-dalek` dependency. Version 2.2.0 already existed in the root lock as a transitive dependency; disabling its default features avoids unnecessary dependency expansion. Neither a quote provider, signer, public contract function nor paid model was called. No key file was read.

## Fix round 4

The opposite verb reservation is now keyed by `(scope_id, approval_id, wallet_pubkey, request_hash)` in both references and the spec. The registry derives the scope from the validated measurement and platform set. Rotated enclave keys in the same scope share the permanent lock. An unrelated permissionless identity cannot reserve a verb for Siyab's scope. A wallet listing two Siyab scopes could receive opposite verbs. Reservation ordering, same verb retries and persistence after failed signing, callback refusal or expiry stay unchanged.

The global rule test was replaced in both languages by five shared signed sequences. Three cover rotation within a scope: approve then reject refuses, reject then approve refuses, and approve then approve retries successfully. Two start with a hostile scope reserving approve, then accept Siyab's reject for the same approval, wallet and request hash. One hostile scope changes the compose measurement; the other changes the platform set. Each step independently validates its key view and enclave signature before exercising the transition. These are self consistent synthetic `UpToDate` records and test signatures, not new accepted quotes. The existing policy vectors now name their scope explicitly. Their opposite verb checks also change recipient, nonce, evidence and intent expiry while retaining the scope, and still refuse without mutation. The shared vector count is 137; the original byte format hashes are unchanged.

Before the reservation fix, the replacement Python test failed on both hostile scope subcases with `opposite verb already reserved`. The replacement Rust test also failed on the hostile measurement sequence. After adding scope to the reservation key, the same tests passed. No registry, signer or production Gate was implemented.

The spec defines the design's `vote_signed` success event with NEP-297 prefix `EVENT_JSON:`, standard `siyab_attest`, version `1.0.0` and one data object containing exactly `scope_id`, `approval_id`, `wallet_pubkey`, `request_hash`, `verb` and `evidence_hash`. The name matches the design; this standard, version and exact data shape are explicit Siyab choices. Emit once per successful callback, including a successful retry, and never at reservation, dispatch or refusal. Failed receipts do not establish registry success.

Both public writes require exactly 300000000000000 prepaid gas units, not a minimum, and the existing exact 0.01 NEAR deposit. A wrong deposit or gas amount panics at ingress before mutation or dispatch, with no normal JSON result or contract scheduled refund. Other normally handled ingress refusals return immediate JSON `null` and schedule the full deposit refund to the captured relayer; the runtime failure and scheduled transfer qualifications still apply. The registration method now states its immediate refusal result explicitly. The opening of the spec explicitly supersedes the design's `vote_path` view; callers use the deterministic formula or the successful vote result's path.

The registry lane must enforce strict Ed25519 verification in the contract rather than assume the NEAR host is strict. The published near-vm-runner 0.38.0 source, pinned in the retained verifier build lock, calls ordinary `verify`. Its source file was checked byte for byte against the immutable upstream revision. SDK 5.29.1 delegates its Wasm check to that host interface. The Rust reference already uses `ed25519-dalek` 2.2.0 `verify_strict`; T1 must use strict verification or equivalent explicit point and scalar checks. A successful host check alone is insufficient under this spec.

Sources verified for this round:

1. NEP-297 event prefix, envelope and application defined data: https://github.com/near/NEPs/blob/master/neps/nep-0297.md#specification
2. SDK 5.29.1 Ed25519 host delegation: https://github.com/near/near-sdk-rs/blob/near-sdk-v5.29.1/near-sdk/src/environment/env.rs#L631-L667
3. Published near-vm-runner 0.38.0 host using ordinary verification: https://static.crates.io/crates/near-vm-runner/near-vm-runner-0.38.0.crate and matching https://github.com/near/nearcore/blob/bc4e9e8cfb03d9e82f027883055624e18576993e/runtime/near-vm-runner/src/logic/host.rs#L1999-L2066
4. Strict Ed25519 reference behavior: https://docs.rs/ed25519-dalek/2.2.0/src/ed25519_dalek/verifying.rs.html
5. Independent receipts and callback failure qualifications: https://docs.near.org/smart-contracts/anatomy/crosscontract

The reservation scope, event data, exact gas equality and budget mismatch panic are application policy choices. They are not claims about an existing deployed Siyab registry. Independent read only review checked all round 4 items and found no remaining blocker.

## Replay callback

`contracts/attest/tests/probe` is a test-only contract with no registration or key storage. It sends the same Borsh verification call with 200 Tgas and zero unused gas weight, followed by a private callback with 60 Tgas and zero unused gas weight. The top-level sandbox call prepays 300 Tgas. The callback checks `Verified`, `UpToDate`, exact TD10, every selected event type and recomputed SHA384 event digest, then extends and compares RTMR3. A corrupt event digest returns false.

The callback receipt burned 1987846853290 gas, or 1.98784685329 Tgas, replaying nine RTMR3 events from the 29 event upstream log. The complete promise chain burned 133460791435804 gas. This is a measured minimal callback, not a unit benchmark estimate. It includes callback input decoding and result handling. It excludes future T1 report data, compose policy, timestamp, lease and storage checks. It proves this retained log fits; T1 must bound event count and payload size before claiming a callback gas limit for arbitrary inputs.

The replay algorithm was checked against both tag 3.18.0 and the design's requested revision. The upstream uses native endian bytes for the event type; the probe spells these as little endian for Wasm and x86 TDX. No complete registry replay module was implemented.

Sources: https://github.com/near/mpc/blob/86e2cc4d/crates/attestation/src/attestation.rs and https://github.com/near/mpc/blob/3.18.0/crates/attestation/src/attestation.rs. Gas allocation pattern: https://github.com/near/mpc/blob/3.18.0/crates/contract/src/config.rs. Promise and view behavior: https://docs.near.org/smart-contracts/anatomy/crosscontract.

## Prior art kill search

No maintained, deployed, general TDX attestation registry on NEAR mainnet was proven. This is a bounded negative result, not proof of absence. A verifier, an application registry and a reusable framework are different candidates. The strongest mainnet counterexample is OutLayer, but its public source restricts admission to its initialization contract and governance approved builds. T1 remains a registry above the existing verifier; if a general deployment is subsequently established, T1 should become an adapter.

| Candidate | What was found | Source |
|---|---|---|
| near/mpc verifier | Stateless quote verification; callers apply policy and key binding | https://github.com/near/mpc/blob/3.18.0/crates/tee-verifier/src/lib.rs |
| near/mpc signer | Participant attestations bound to MPC images, launcher and account keys | https://github.com/near/mpc/blob/3.18.0/crates/contract/src/api/attestation.rs |
| OutLayer | Mainnet worker key registration restricted to its initialization contract and approved builds | https://github.com/out-layer/outlayer/blob/main/register-contract/README.md and https://github.com/out-layer/outlayer/blob/main/register-contract/src/lib.rs |
| Oathline | Close testnet model key registry, verifier callback, nonce binding and RTMR3 replay | https://github.com/Anshv784/oathline |
| Symbolon | Testnet attested key registry within a payment wallet | https://github.com/0d1026/Symbolon |
| ProvenExit | Testnet VPN instance registry | https://github.com/erhantrk/provenexit |
| Shade Agent Framework | Reusable library and contract templates, no shared general mainnet deployment established | https://github.com/NearDeFi/shade-agent-framework |
| Phala dstack | KMS application authorization contracts under its Ethereum authorization module | https://github.com/Dstack-TEE/dstack/tree/next/dstack/kms/auth-eth and https://docs.phala.com/dstack/design-documents/whitepaper |
| Archetype | Package metadata assertions, not TDX verification | https://github.com/archetype-org/attestation-registry |
| near-dcap | Repository labels the work beta and testnet | https://github.com/mattlockyer/near-dcap |
| dcap-qvl-near | Minimal verification example and gas benchmark | https://github.com/think-in-universe/dcap-qvl-near |

Live final RPC reads found `worker.outlayer.near` deployed at final block 225606184 with code hash `DLJHXTm94Ud1A9c9d9N9Mhz1RVEH15DVAsjjEVkegDDn`, five access keys including one full access key and two approved measurement sets. Owner, governance and initialization views returned `owner.outlayer.near`, `dao.outlayer.near` and `init-worker.outlayer.near`. Source to deployed byte equivalence was not rebuilt. A `v1.signer` view at block 225606418 returned `tee-verifier-2026-11-11.near` as its verifier account.

Sources: https://rpc.mainnet.near.org, https://nearblocks.io/address/worker.outlayer.near, https://nearblocks.io/address/v1.signer and https://nearblocks.io/address/tee-verifier-2026-11-11.near.

Searches covered GitHub repositories for `near tdx`, `near attestation registry`, `near tee registry` and `NEAR dcap`; public web searches combining NEAR, general or permissionless registry, attested key, TDX and mainnet; and targeted NEAR docs, near/mpc, OutLayer, Phala docs and NearBlocks searches. Repository metadata showed near/mpc, OutLayer and dstack active and unarchived on the observation date. Activity does not establish a deployment or general interface. Close testnet projects prevent claiming novelty for on-chain NEAR AI key attestation.

Search and documentation entry sources: https://api.github.com/search/repositories, https://docs.near.org/, https://docs.phala.com/, https://github.com/near/mpc and https://nearblocks.io/.

## Validation and owner commands

Run the Rust commands under Linux or macOS for near-workspaces. The verified local environment was Ubuntu WSL with Rust 1.99.1. From the repository root:

```sh
export CARGO_TARGET_DIR="$HOME/target/siyab-attest-t0"
cargo +1.99.1 build --manifest-path contracts/attest/tests/probe/Cargo.toml --locked --offline --target wasm32-unknown-unknown --release -j 2
cargo +1.99.1 test --manifest-path contracts/attest/Cargo.toml --locked --offline -j 2 -- --test-threads=1
cargo +1.99.1 test --manifest-path contracts/attest/Cargo.toml --locked --offline -j 2 -- --include-ignored --test-threads=1
python -m unittest tests.test_attest_vectors -v
python scripts/check_hygiene.py --all
python scripts/check_citations.py
git diff --check
```

The ordinary Rust run reports 20 unit tests and three sandbox tests passed, with the dated test and separate replay measurement test ignored. The third command includes all five sandbox tests after building the replay prerequisite. It is a local sandbox run, not a public transaction. The dated proof must run before 21 December 2026 at 14:22:06 UTC. To select only that proof in a later run:

```sh
export CARGO_TARGET_DIR="$HOME/target/siyab-attest-t0"
cargo +1.99.1 test --manifest-path contracts/attest/Cargo.toml --locked --test sandbox dated_mainnet_wasm_accepts_current_dstack_quote_until_collateral_expiry -j 2 -- --ignored --exact --nocapture --test-threads=1
```

A bare `cargo test` reports `replay_callback_measurement` as ignored, so a missing measurement is never counted as a passing test. The upstream positive quote and exact signature rejection controls are a separate test and still run. Selecting the ignored replay test explicitly requires `CARGO_TARGET_DIR` and a readable probe wasm; either missing prerequisite fails with build instructions. There is no early successful return. Keep targets outside the repository even when omitting the environment variable by passing Cargo's `--target-dir` option. Round 2 also ran the complete ordinary suite with `CARGO_TARGET_DIR` unset and an external `--target-dir`, retaining the two explicit ignored results; that alternate environment run was not repeated in round 3.

The retained verifier artifacts suffice for those tests. To rebuild only the TEST-ONLY artifacts, install the pinned Rust target and clang 18, then run `python3 contracts/attest/tests/build_vendor.py --target-dir "$HOME/target/siyab-attest-t0-upstream"`. The builder verifies the immutable source archive and retained manifest and lock. It does not deploy. Source and compiler paths can affect a rebuilt hash; a mismatch must be reviewed before replacing the recorded baseline.

The owner can make the deferred testnet check from the repository root with near-cli-rs 0.30.1. Its installed help confirmed `file-args`, `network-config testnet` and `now`:

```sh
near contract call-function as-read-only tee-verifier-2026-11-02.testnet verify_quote_with_collateral_dates file-args fixtures/attest/deepseek-gateway-20261121.borsh network-config testnet now
```

This is a view call: 0 NEAR in on-chain fees, no signer key, no account funding and no transaction. Provider usage limits can still apply. It returns Borsh bytes. The retained input uses the gateway's quote and its own embedded PCK chain. Before 21 December at 14:22:06 UTC, expect `Verified` with `UpToDate`; after that, obtain a new quote and collateral rather than using a pinned clock on a public network. The upstream August input is already expired and must not be used for the positive owner check. No external verifier function call was made by this lane.

Sources: https://github.com/near/mpc/blob/3.18.0/docs/development/deploy-tee-verifier.md, https://github.com/near/mpc/blob/3.18.0/crates/tee-verifier/src/lib.rs, https://docs.near.org/api/rpc/contracts#call-a-contract-function and https://docs.near.org/smart-contracts/anatomy/functions#read-only-functions.

| Check | Result |
|---|---|
| `cargo +1.99.1 test --manifest-path contracts/attest/Cargo.toml --locked --offline -j 2 -- --test-threads=1` | 20 unit tests and 3 sandbox tests passed, 0 failures, 2 tests ignored; 0 doctests |
| The same Cargo command with `--include-ignored` | 20 unit tests and all 5 sandbox tests passed, 0 failures, 0 ignored; 0 doctests; valid and corrupt replay controls and dated unchanged mainnet wasm both executed locally |
| `python -m unittest tests.test_attest_vectors -v` | 47 passed, 0 failures, 0 skips |
| `python scripts/check_hygiene.py --all` | Exit 0 |
| `python scripts/check_citations.py` | Exit 0 |
| `git diff --check` | Exit 0 |

Fix round 3 ran 72 distinct repository tests successfully: 47 Python, 20 Rust unit and five Rust sandbox tests. Ordinary and include ignored runs both passed; the ordinary run honestly reports the two opt in tests as ignored. New focused Python regressions were first run red with three missing reference helper errors, then passed after implementation. Independent read only source and reference reviews found no remaining correctness blocker. Review also corrected the changed measurement vector so it cannot pass by failing unrelated hash validation. The orchestrator retains final independent review and commit ownership. Round 2 had 52 distinct passing tests and the retained replay measurement of 1987846853290 callback gas and 133460791435804 complete chain gas; round 3 required successful controls, without making a new gas measurement claim.

Fix round 4 rebuilt the probe with the locked offline command above and passed the same 72 distinct repository tests: 47 Python, 20 Rust unit and all five Rust sandbox tests. The ordinary Cargo run passed 20 unit and three sandbox tests with two ignored, and the include ignored run passed 20 unit and all five sandbox tests with zero ignored. Both had zero doctests and zero failures. The Python run had zero failures or skips. The replacement regression was first red in both languages, as recorded above. The requested hygiene and citation commands, rustfmt check and whitespace checks passed. No dependency or sandbox test was changed, and no new gas measurement claim is made.

The earlier spike's isolated compatibility probe passed one test, its builder refusal checks passed six and its retained integrity checks passed nine; those auxiliary checks were not rerun in round 2. No existing application Python module was changed or imports this reference test. The full repository suite is left to the orchestrator as requested.

Fix round 4 changed five files: `docs/spec/attest-v1.md`, `docs/lanes/attest-t0.md`, `tests/test_attest_vectors.py`, `fixtures/attest/vectors.json` and `contracts/attest/src/lib.rs`. The manifests, lockfiles, sandbox tests and all eight protected files were hash checked unchanged this round. The owner has no transaction or paid call to run for these fixes. Independent review, the full repository suite and any eventual commit remain with the orchestrator; the read only public verifier check above remains deferred. Complete T1 evidence bounds and callback gas measurement, real Ed25519 domain selection, registry deployment and Gate integration remain outside T0 and were not attempted.

Changed files are limited to `.gitattributes`, the new `contracts/attest` tree, `docs/spec/attest-v1.md`, this lane report, the new `fixtures/attest` tree and `tests/test_attest_vectors.py`. The source tree contains no registry deployment or payment path. All user protected files remain unchanged, and no commit, remote change, submission tag mutation, public deployment, paid model call or public network transaction was performed.

Automatic approval review rejected the targeted recursive cleanup of the ignored `.codex/attest-t0/build` directory with the reason `blocked by policy`. No alternate deletion was attempted. Ignored build scratch remains local, outside the changed file inventory. It is not needed for the tests because durable artifacts, recipes, source hashes and the rebuild script are retained.
