# Attestation

Version: 1. Status: T0 interface freeze and synthetic byte vectors. This file does not assert that a Siyab attestation registry or vote signer has been deployed. The verifier spike and dated network observations are in `docs/lanes/attest-t0.md`. This spec supersedes the design's `register` and `sign_vote` signatures with the `register_key` and `submit_vote` interfaces below. It also supersedes the design's `vote_path` view: v1 has no such view method; derive the path by the voice scope formula below or read it from a successful `VoteSignatureView`.

## Encodings

`||` means byte concatenation. Quoted domain strings are their exact ASCII bytes, without a trailing newline. `SHA256` returns 32 bytes. `SHA384` returns 48 bytes. A hash formula consumes decoded bytes, never their hexadecimal spelling.

JSON byte strings use lowercase hexadecimal with exactly two characters per byte, without `0x`. No whitespace, uppercase spelling, truncation or padding is accepted. Integers are unsigned whole JSON numbers in the range 0 through 2^64 minus 1. Booleans, floats, decimal strings and out of range integers are refused. Millisecond fields count from the Unix epoch. JSON object member order does not affect a digest; the field order specified below does.

A NEAR account id follows the existing account id validator in `gate/canon.py`: 2 through 64 ASCII characters, lowercase letters or digits separated by single `-`, `_` or `.`, with a letter or digit at each end. An account id cannot contain a NUL byte. Siyab v1 requires exact fields in measurement, vote intent and key view objects; missing or additional fields are refused.

The fixed field widths come from the TD10 report and measurement types in near/mpc tag 3.18.0. Its verifier interface exposes PPID as a variable byte vector. Siyab v1 accepts exactly 16 PPID bytes and refuses any other width, rather than truncating or hashing it. This is an application requirement, not a claim that the upstream DTO enforces the width.

Sources: https://github.com/near/mpc/blob/3.18.0/crates/tee-verifier-interface/src/lib.rs and https://github.com/near/mpc/blob/3.18.0/crates/attestation-types/src/measurements.rs

## Report data

Inputs are the registry account id, a 32 byte Ed25519 enclave public key `K`, `issued_at_ms`, and a 32 byte `aux`. The registry uses its own current account id when reconstructing this value.

```
binding = SHA256(
    "siyab-attest-v1" || 0x00 || registry_account_id || 0x00 || K || u64_be(issued_at_ms)
)
report_data = binding || aux
```

The result is exactly 64 bytes. The account id is ASCII without a length prefix. The NUL delimiters and the fixed widths of the key and time make the input unambiguous. The time is eight bytes in big endian order. This time encoding deliberately differs from the Borsh vote encoding below.

When TLS ends inside the CVM, `aux` is SHA256 of that TLS public key's DER SubjectPublicKeyInfo. This serialization is a Siyab v1 choice for the design's TLS public key hash. When TLS ends elsewhere, `aux` is 32 zero bytes. A zero auxiliary value makes no claim about a TLS endpoint. The vectors start from the auxiliary digest, without generating a TLS key or certificate.

Source for the SubjectPublicKeyInfo structure: https://www.rfc-editor.org/rfc/rfc5280#section-4.1.2.7

## Measurement id

The measurement object contains exactly these six fields, in this hash order:

| Field | Bytes | JSON characters |
|---|---:|---:|
| `mrtd` | 48 | 96 |
| `rtmr0` | 48 | 96 |
| `rtmr1` | 48 | 96 |
| `rtmr2` | 48 | 96 |
| `key_provider_digest` | 48 | 96 |
| `compose_hash` | 32 | 64 |

```
measurement_id = SHA256(
    "siyab-measurement-v1" || mrtd || rtmr0 || rtmr1 || rtmr2 ||
    key_provider_digest || compose_hash
)
```

There are no separators, length prefixes or NUL bytes in this formula. The domain is 20 bytes and the fields total 272 bytes. The output is 32 bytes, exposed as 64 lowercase hexadecimal characters.

`key_provider_digest` is the SHA384 event digest of the unique RTMR3 `key-provider` event. It is not SHA256 of that event's payload. `compose_hash` is SHA256 of the exact UTF-8 bytes of `app_compose`, without parsing and serializing the JSON again.

Source: https://github.com/near/mpc/blob/3.18.0/crates/attestation/src/attestation.rs

Before deriving a measurement or accepting a key, read the eight `td_attributes` bytes from the verified quote's exact `TD10` report as a little endian u64. Apply the complete dcap-qvl 0.7.0 production `validate_td10` predicate:

| Bits | Requirement |
|---|---|
| DEBUG 0 | Clear |
| Profiling 4 through 6 | Clear |
| SERVTD_EXT 17 | Clear |
| MIGRATABLE 29 | Clear |
| SEPT_VE_DISABLE 28 | Set |
| Reserved 1 through 3, 7 through 15, 23 through 26, 32 through 61 | All clear |

Equivalently, `attributes & 0x3fffffff2782ffff == 0` and `attributes & 0x10000000 != 0`. The reserved mask alone is `0x3fffffff0780ff8e`. Bits 16, 18 through 22, 27, 30, 31, 62 and 63 are unrestricted by this predicate, matching that upstream version. Application measurement policy still applies.

TDATTRIBUTES starts at byte 120 of the TD report. In a version 4 quote, including the retained regression quote, this is byte 168 of the quote, after its 48 byte header. A version 5 TD10 quote has an additional six byte body header and therefore places it at byte 174. Offsets are zero based. Read the verified report's field directly rather than applying the version 4 absolute offset to another wrapper.

Missing or uncheckable attributes are refused. These are independent Siyab acceptance checks, including when T1 links dcap-qvl directly. A caller supplied attributes value or the verifier's own policy is not a substitute for checking the verified TD report. dcap-qvl 0.7.0 applies these checks through `QuoteVerifier::new_prod()`; that behavior does not remove this requirement.

Sources for the little endian decoding, bit positions, quote layout and production defaults: https://github.com/Phala-Network/dcap-qvl/blob/94208e2753a854191759e493ca5892be482ad453/src/quote.rs, https://github.com/Phala-Network/dcap-qvl/blob/94208e2753a854191759e493ca5892be482ad453/src/verify.rs#L1645-L1666 and https://github.com/Phala-Network/dcap-qvl/blob/94208e2753a854191759e493ca5892be482ad453/src/verify.rs#L287-L337. This revision is the published dcap-qvl 0.7.0 source.

RTMR3 is checked before deriving the measurement. Keep the source event order, select events whose `imr` is 3, require `event_type` 134217729, and decode each hexadecimal event payload. Recompute each 48 byte event digest and extend an initial 48 zero byte register:

```
event_digest = SHA384(u32_le(event_type) || ":" || UTF8(event) || ":" || payload_bytes)
rtmr3_next = SHA384(rtmr3_previous || event_digest)
```

The final register must equal the quote's RTMR3. The unique `compose-hash` payload must equal the compose hash and the unique `key-provider` event supplies its digest. The implementation at the cited tag uses native endian event type bytes; dstack's x86 TDX guest and the Wasm target are little endian, which v1 fixes explicitly. The registry takes MRTD and RTMR0 through RTMR3 from the verified report. An adapter reading `tcb_info` must also compare its supplied registers to the quote before constructing the registration call. A digest supplied by a caller without this replay is not measurement evidence.

Sources: https://github.com/near/mpc/blob/3.18.0/crates/attestation/src/attestation.rs and https://github.com/near/mpc/blob/3.18.0/crates/attestation-types/src/tcb_info.rs

## AttestedKeyView

`get_key` takes exactly `{"public_key":"<K_HEX>"}`, where `K_HEX` is the raw 32 byte Ed25519 enclave public key encoded as 64 lowercase hexadecimal characters. It does not accept `ed25519:` base58, a byte array or another argument name. It returns `null` for an absent record. A present record has exactly the following fields. Their display order is the table order; JSON member order is not signed.

| Field | JSON form | Meaning |
|---|---|---|
| `public_key` | 64 lowercase hexadecimal characters | Raw 32 byte Ed25519 enclave public key |
| `measurement_id` | 64 lowercase hexadecimal characters | Recomputed from `measurement` by the formula above |
| `measurement` | Object with the six fields above | The quote and replay bound measurement |
| `ppid` | 32 lowercase hexadecimal characters | The verified report's 16 PPID bytes |
| `aux` | 64 lowercase hexadecimal characters | The report data's second 32 bytes |
| `tcb_status` | String, exactly `UpToDate` | The verifier's accepted status |
| `advisory_ids` | Array of strings | Verbatim verifier advisory ids, preserving order |
| `issued_at_ms` | u64 JSON integer | Time committed to in report data |
| `registered_ms` | u64 JSON integer | Registry block time when accepting the record |
| `expires_ms` | u64 JSON integer | End of the accepted lease |
| `collateral_expires_ms` | u64 JSON integer | Earliest collateral expiration, converted from seconds |

The accepted report variant is exactly `TD10`. The upstream `as_td10()` helper also projects later TD variants onto a TD10 base, so that helper alone does not implement this requirement. The record is stored only after a `Verified` result with status `UpToDate`, the independent TD attributes check above, complete report data and event replay checks, and the two hour issue time bound. Advisory ids are retained without making their presence a refusal.

The issue time bound is:

```
SKEW = 0 milliseconds
registered_ms - 7_200_000 <= issued_at_ms <= registered_ms + SKEW
```

Both edges are inclusive. SKEW is zero because v1 grants no allowance for an enclave timestamp later than the registry's acceptance block. The lower bound is mathematical subtraction; for u64 arithmetic use `registered_ms.saturating_sub(7_200_000)`, equivalently clamp it to zero. An issue time one millisecond before the lower edge or one millisecond after registration is refused. Readers validate this same bound when consuming a view.

`registered_ms = floor(block_timestamp_ns / 1_000_000)` in the final successful registration callback. Discard any fractional millisecond, without rounding to nearest or rounding up. Use the acceptance callback's block, not the earlier request's block or a caller supplied clock. This matches near-sdk's `env::block_timestamp_ms()`.

At registration, `expires_ms = min(issued_at_ms + max_lease_ms, collateral_expires_ms)`, with checked integer arithmetic and `registered_ms < expires_ms`. The lease starts at the issue time committed to in report data, so a late relay cannot extend the key's lifetime. A relay at or after the computed expiry is refused. `max_lease_ms` is the immutable registry policy returned by `get_config`, not an extra key view field. The verifier's collateral dates are Unix seconds; multiply the earliest expiration by 1000 with overflow checking. The resulting view requires the computed expiry and `registered_ms < expires_ms <= collateral_expires_ms`. A reader treats a lease as expired when `now_ms >= expires_ms`. A missing, malformed, uncheckable or expired record is UNKNOWN or ASK, never ALLOW.

With only a key view, its requested public key, the Gate's current block time and local policy, a Gate can check exactly these properties:

1. The exact eleven fields, canonical byte widths and u64 integers, advisory string array and `UpToDate` status.
2. Equality of `public_key` with the requested key and equality of the recomputed measurement id with `measurement_id`.
3. The inclusive issue time bound, `registered_ms < expires_ms <= collateral_expires_ms`, and `registered_ms <= now_ms < expires_ms`.
4. The measurement, key, PPID, auxiliary value and advisory ids against that Gate's own pins and deny policy.

The view alone cannot check the exact lease formula because it omits `max_lease_ms`. Read `get_config` from the same pinned registry, validate its exact shape and then check the formula with checked addition. Read the key at the same final block or a later final block; the config is immutable. The view also omits the quote, report variant, TD attributes, report data and event log, so it cannot independently prove DCAP, report binding or replay. Those checks rely on the pinned registry's code and verifier. A view is not evidence that a previously stored record was globally revoked or that a vote was signed.

Source for report variants, status and collateral date units: https://github.com/near/mpc/blob/3.18.0/crates/tee-verifier-interface/src/lib.rs

Source for block time conversion: https://github.com/near/near-sdk-rs/blob/near-sdk-v5.29.1/near-sdk/src/environment/env.rs

## Initialization and config

`new` is a one time, self call only initializer: require predecessor to equal the registry's current account id. Its arguments have exactly the following five fields. On success it initializes state and returns empty result bytes, matching near-sdk's `#[init]` wrapper. Refuse reinitialization. `get_config` takes exactly `{}` and returns those same fields and values, without an envelope or extra fields. Both calls attach zero deposit. The names and JSON shape are Siyab v1 choices.

```json
{
    "verifier_account_id": "tee-verifier-2026-11-11.near",
    "signer_account_id": "v1.signer",
    "ed25519_domain_id": 7,
    "max_lease_ms": 86400000,
    "vote_recipients": ["outlayer.near"]
}
```

Both account fields must be canonical NEAR account ids. `ed25519_domain_id` is a u64 and must identify a checked Ed25519 domain of the configured signer before deployment; 7 here is synthetic, not a deployed domain assertion. `max_lease_ms` is a nonzero u64. `vote_recipients` contains 1 through 8 canonical NEAR account ids, already sorted in ascending ASCII order and unique. Reject missing or extra fields, an empty list, duplicates and unsorted input. Do not normalize it. The listed accounts and 24 hour lease are a complete format example, not a deployment authorization.

The initializer stores these values once. There is no owner argument, owner role, config setter, upgrade method or global revoke method. A deployment claiming this policy must also have no access key that can replace its code. Gate operators pin and inspect the registry rather than trusting an arbitrary contract with these method names. This spec does not authorize deployment.

Source for initializer return behavior: https://github.com/near/near-sdk-rs/blob/near-sdk-v5.29.1/near-sdk-macros/src/core_impl/code_generator/snapshots/simple_init.snap

## Registration call

The registration method is `register_key`. It takes exactly this JSON object. Angle bracket values denote schema variables; complete synthetic wire examples are in `fixtures/attest/vectors.json`.

```json
{
    "public_key": "<K_HEX>",
    "issued_at_ms": 0,
    "aux": "<AUX_HEX>",
    "verification_args": "<BORSH_TUPLE_HEX>",
    "app_compose": "<EXACT_COMPOSE_STRING>",
    "event_log": []
}
```

`public_key`, `issued_at_ms` and `aux` are the report data inputs defined above. The zero issue time and empty event array in the schema illustrate JSON types only. A successful registration requires a live issue time and the complete replay and unique event checks. `verification_args` is the lowercase hexadecimal encoding of the exact Borsh tuple `(QuoteBytes, Collateral)` from `tee-verifier-interface` tag 3.18.0. Decode the entire tuple and refuse malformed data or trailing bytes. Forward its decoded bytes as the Borsh arguments to the configured verifier's `verify_quote_with_collateral_dates` method. Do not wrap those bytes in JSON or reframe them as a Borsh byte vector.

`app_compose` is the original compose string. Hash its UTF-8 bytes after decoding the outer JSON string, preserving its contained whitespace and Unicode without normalization. Each `event_log` element has exactly the JSON fields `imr`, `event_type`, `digest`, `event`, `event_payload`. `imr` and `event_type` are unsigned whole JSON integers in 0 through 2^32 minus 1. `digest` is 48 bytes of lowercase hexadecimal. `event` is the exact UTF-8 string. `event_payload` is even length lowercase hexadecimal, with the empty string encoding zero bytes. Keep array order. The selected RTMR3 events must pass the checks above; supplying an empty log cannot bypass replay or the unique events.

Missing or additional fields in these objects are refused. The caller supplies no verifier account, registry account, measurement, PPID, status, registration time, expiration or lease policy. The registry uses its own account and configured verifier and policy, derives the measurement and PPID from verified evidence, and computes the accepted times in its private callback. These method and envelope choices are Siyab v1 interfaces, not upstream registry APIs.

For an existing `public_key`, including an expired record, `register_key` is a renewal only. Require the candidate's `issued_at_ms` to be strictly greater than the stored `issued_at_ms`, and require identical `measurement_id`, `ppid` and `aux`. The auxiliary value cannot change, including a switch between zero and a TLS digest. A different measurement, machine or TLS binding needs a new enclave key. Equal issue time is a replay and is refused even if every byte is identical. A lower issue time is refused even while its quote is within the two hour window. Expiry does not erase this comparison record.

Apply every quote, binding, time and renewal check in the private verifier callback immediately before replacing the record. Load the current stored record in that callback; a request time snapshot cannot authorize replacement. If an older request completes after a newer one has stored its record, refuse the older request and preserve the newer record byte for byte. If the older completes first, a later completing newer quote may renew it. A refusal does not mutate the stored record. The callback clock and issue based lease formula apply on every renewal.

`register_key` is payable and takes exactly 10000000000000000000000 yoctoNEAR, or 0.01 NEAR, with exactly 300 Tgas prepaid, or 300000000000000 gas units. The gas requirement is an equality, not a minimum. A wrong deposit or prepaid gas amount panics at ingress before state mutation or verifier dispatch; it does not return JSON `null` or schedule a contract refund. These are v1 call requirements, not measured storage or gas guarantees for arbitrary evidence. Forward zero deposit and 200 Tgas to the verifier, then reserve 60 Tgas for the private callback, both with unused gas weight zero. A future implementation must bound and measure accepted evidence before enabling this path; an oversized request or insufficient execution gas cannot produce a record.

The method returns a promise, or immediate JSON `null` for a normally handled ingress refusal after the exact call budget checks. An ingress refusal changes no state, dispatches no verifier request and schedules the full attached deposit refund to the relayer captured as predecessor at ingress. The promise resolves to the accepted `AttestedKeyView`, or JSON `null` for a refused registration or failed verifier result. No Borsh verifier output is returned as the public result. Before storing, charge only a positive actual storage byte delta at `env::storage_byte_cost()`, with checked arithmetic and cost no greater than the attached deposit. An insufficient budget refuses the candidate without changing the prior record. On callback completion, schedule a transfer of the unused deposit to the captured relayer, including a full refund on refusal. A record that shrinks does not refund storage paid by an earlier relayer. Do not identify the relayer from the callback's predecessor. A runtime panic, out of gas or failed callback is a failed receipt, with no normal JSON result or guaranteed refund. A scheduled refund is not proof that its transfer succeeded.

Sources for the verifier method, tuple and event fields: https://github.com/near/mpc/blob/e691bdb98d68dcf74653da58d043453a6318765f/crates/tee-verifier/src/lib.rs, https://github.com/near/mpc/blob/e691bdb98d68dcf74653da58d043453a6318765f/crates/tee-verifier-interface/src/lib.rs and https://github.com/near/mpc/blob/e691bdb98d68dcf74653da58d043453a6318765f/crates/attestation-types/src/tcb_info.rs

Sources for storage byte cost, attached deposit and independent callback execution: https://github.com/near/near-sdk-rs/blob/near-sdk-v5.29.1/near-sdk/src/environment/env.rs and https://docs.near.org/smart-contracts/anatomy/crosscontract. Deposit and gas budgets above are Siyab choices; the T0 replay measurement is not a complete registry benchmark.

## Voice scope

A platform set contains 1 through 8 PPIDs, each exactly 16 bytes. Inputs must already be sorted in ascending byte order with no duplicates. For fixed lowercase hexadecimal strings this is also their text order. Implementations refuse an unsorted or repeated value; they do not silently sort or deduplicate.

```
scope_id = SHA256("siyab-voice-v1" || measurement_id || ppid_1 || ... || ppid_n)
vote_path = "vote/" + lowercase_hex(scope_id)
```

The measurement id is 32 decoded bytes. Each PPID is 16 decoded bytes. This formula has no NUL, count or length prefix. Fixed widths distinguish every supported set length. A different measurement or platform set names a different identity. The platform set is an identity choice and is also covered by the enclave's vote intent below.

The registry derives `vote_path` itself from the validated intent's measurement and platform set. The caller cannot supply or override it. Consume its exact 69 ASCII bytes, including `vote/`, as the Chain Signatures `sign` call's `request.path`. Do not hash the path again, decode its hexadecimal suffix or add a registry or relayer prefix. The immediate predecessor of that call must be the registry account. Public key derivation uses that same registry predecessor, path and explicitly configured Ed25519 domain. A relayer calling Chain Signatures directly has a different identity.

The signing call has this JSON shape; `DOMAIN_ID` is the registry's configured u64 JSON integer for an Ed25519 domain:

```json
{
    "request": {
        "path": "vote/<64 lowercase scope hex characters>",
        "payload_v2": {"Eddsa": "<64 lowercase NEP-413 digest hex characters>"},
        "domain_id": 7
    }
}
```

The numeric 7 is synthetic in the vectors and does not name an approved deployed domain. The payload encodes the 32 decoded bytes of `nep413_digest`, not the enclave intent digest, the NEP-413 preimage or the hexadecimal text as message bytes. V1 uses `payload_v2` with `Eddsa` and `domain_id`; the legacy `payload` and `key_version` fields do not express this request. Resolve and check an actual Ed25519 domain before enabling signing. T0 makes no signing request.

Sources for the request fields, payload encoding, predecessor and key derivation: https://github.com/near/mpc/blob/e691bdb98d68dcf74653da58d043453a6318765f/crates/near-mpc-crypto-types/src/sign.rs, https://github.com/near/mpc/blob/e691bdb98d68dcf74653da58d043453a6318765f/crates/near-mpc-crypto-types/src/primitives.rs, https://github.com/near/mpc/blob/e691bdb98d68dcf74653da58d043453a6318765f/crates/contract/src/api/sign.rs and https://github.com/near/mpc/blob/e691bdb98d68dcf74653da58d043453a6318765f/crates/contract/src/api/keys.rs

## Vote intent

The design specified SHA256 of `siyab-vote-v1|` followed by fields, without fixing how those fields are framed or which contextual fields they include. T0 resolves that ambiguity with this exact domain and Borsh tuple. This is a Siyab v1 format choice, not an OutLayer wire format.

```
intent_digest = SHA256("siyab-vote-v1|" || borsh(VoteIntentV1))
```

`VoteIntentV1` has exactly these twelve fields in this serialization order:

| Field | Borsh type | Input validation |
|---|---|---|
| `registry_account_id` | `String` | Current registry account id |
| `public_key` | `[u8; 32]` | Attested enclave key, 64 lowercase hexadecimal characters in JSON |
| `measurement_id` | `[u8; 32]` | Measurement id from that key's record |
| `platforms` | `Vec<[u8; 16]>` | Sorted unique set of 1 through 8 PPIDs |
| `verb` | `String` | Exactly `approve` or `reject` |
| `approval_id` | `String` | Lowercase UUID text, 36 ASCII bytes in 8-4-4-4-12 form |
| `wallet_pubkey` | `String` | Exact original text, 1 through 256 nonspace printable ASCII bytes |
| `request_hash` | `[u8; 32]` | Recomputed operation hash, 64 lowercase hexadecimal characters in JSON |
| `recipient` | `String` | NEP-413 verifying deployment id, `outlayer.near` for the design's mainnet OutLayer integration |
| `nonce` | `[u8; 32]` | Canonical padded standard base64 in JSON, decoding to exactly 32 bytes |
| `evidence_hash` | `[u8; 32]` | 64 lowercase hexadecimal characters in JSON |
| `expires_ms` | `u64` | Expiry of the intent, milliseconds since the Unix epoch |

An enclave signs the 32 byte `intent_digest` with pure Ed25519, not Ed25519ph. It does not sign the preimage directly. The signature is 64 bytes; the signature argument is 128 lowercase hexadecimal characters. The vote submission method is `submit_vote`, with exactly two JSON members: `intent` is the complete twelve field `VoteIntentV1` object above and `signature` is that hexadecimal signature. Its envelope is `{"intent": <VoteIntentV1 object>, "signature": "<SIGNATURE_HEX>"}` in schema notation. No field is flattened, renamed or optional; additional fields are refused. Format vectors use dummy signatures; the separate submission policy vectors use verifiable synthetic signatures and never production keys.

After canonical decoding and the exact deposit and gas checks below, the first vote authorization check is to reconstruct the digest and verify `signature` with the decoded `intent.public_key` over that 32 byte digest. Reconstruct with the registry's current account id, and require the intent's id to equal it. Signature verification precedes key lookups, recipient and time policy, conflict checks, state changes and any MPC request. Invalid or uncheckable signatures cannot reserve a vote. The Ed25519 check refuses small order public keys and signature R points as well as malformed or noncanonical signatures. The registry lane must enforce these strict checks in the contract, using strict verification or equivalent explicit point and scalar checks. It must not assume NEAR's host `env::ed25519_verify` is strict: a successful host result alone is insufficient. The pinned near-vm-runner 0.38.0 host calls ordinary `verify`, while the Rust reference uses `ed25519-dalek` 2.2.0 `verify_strict`.

Sources for SDK delegation, the pinned host and strict reference checks: https://github.com/near/near-sdk-rs/blob/near-sdk-v5.29.1/near-sdk/src/environment/env.rs#L631-L667, https://github.com/near/nearcore/blob/bc4e9e8cfb03d9e82f027883055624e18576993e/runtime/near-vm-runner/src/logic/host.rs#L1999-L2066 and https://docs.rs/ed25519-dalek/2.2.0/src/ed25519_dalek/verifying.rs.html.

Next require `recipient` to be in the immutable `vote_recipients` allowlist. It names the NEP-413 verifying deployment, not the payment payee or the wallet's implicit account. The intended mainnet recipient is `outlayer.near`; `outlayer.testnet` is a separate test integration and must be explicitly allowed in that registry's config. The registry must check that the measurement id belongs to the specified unexpired key and that the record's PPID belongs to the specified platform set. With the current block time floored to milliseconds, require:

```
key.registered_ms <= now_ms < intent.expires_ms <= key.expires_ms
```

Equality at `intent.expires_ms` is expired and refused. Equality between intent expiry and key expiry is permitted while `now_ms` is strictly earlier. No future registered key or intent extending past its key's lease is accepted. Check these bounds and the key's eligibility before dispatching an asynchronous signing request and again before returning registry success. If an intent expires while signing is pending, the callback returns `null` and emits no registry success event. That refusal does not conceal or invalidate a signature already visible in the MPC receipt. ASK produces no signature request.

The registry refuses the opposite verb for a `(scope_id, approval_id, wallet_pubkey, request_hash)` it has already signed or reserved. Derive `scope_id` from the validated intent's measurement and platform set by the voice scope formula above. Equality uses the exact validated strings and decoded 32 byte scope and request hashes, with unambiguous tuple framing. This state is per scope: rotated enclave keys in the same scope share the lock, and changing recipient, nonce, evidence or intent expiry cannot evade it. A different measurement or platform set names a different scope, so an eligible hostile scope that reserves `approve` first cannot block Siyab's scope from reserving `reject`. A wallet listing two Siyab scopes could receive opposite verbs.

To cover concurrent calls, reserve the tuple's verb atomically in the ingress receipt after all signature and eligibility checks and before dispatching MPC. An opposite pending verb in that scope is also refused. A same verb retry is allowed only after all normal checks pass. Retain the reservation permanently after signer failure, callback refusal, runtime failure or expiry, because a signature may already be public. Never unlock it on a callback failure. A refusal before dispatch must leave no new reservation.

Intent expiry controls registry processing. It is signed in the enclave's intent digest but is not a field of the NEP-413 vote payload below. Expiry does not revoke an already released NEP-413 signature; the receiving wallet must apply its own approval and nonce checks.

`submit_vote` is payable and requires exactly 10000000000000000000000 yoctoNEAR and exactly 300 Tgas prepaid, or 300000000000000 gas units. The gas requirement is an equality, not a minimum. A wrong deposit or prepaid gas amount panics at ingress before state mutation or MPC dispatch; it does not return JSON `null` or schedule a contract refund. It allocates 200 Tgas and exactly 1 yoctoNEAR to the configured signer's `sign` call, then 60 Tgas and zero deposit to its private callback, with unused gas weight zero for both. The signer must have a gas requirement no greater than that allocation. At ingress, charge a positive actual storage byte delta for the permanent reservation, using checked multiplication by `env::storage_byte_cost()`. Refuse without mutation or dispatch if that charge plus the 1 yoctoNEAR exceeds the attached deposit. Before dispatch, schedule the remaining deposit's refund to the captured relayer predecessor. A same verb retry with no storage growth pays only the 1 yoctoNEAR. A normally handled refusal before dispatch schedules the full deposit refund. The callback stores no additional vote state. It neither refunds an irreversible signer deposit nor removes the reservation. These budgets are v1 requirements, not a measured T1 gas guarantee. Runtime failure and scheduled refund qualifications are the same as for `register_key`.

The method returns a promise, or an immediate JSON `null` for a normally handled ingress refusal after the exact call budget checks. The promise resolves to `null` on signer failure, an unsupported or malformed response, or failed callback eligibility. On success it resolves to exactly this `VoteSignatureView` shape:

```json
{
    "vote_path": "vote/<64 lowercase scope hex characters>",
    "nep413_digest": "<64 lowercase digest hex characters>",
    "signature": "<128 lowercase signature hex characters>"
}
```

The signature here is the MPC Ed25519 signature on `nep413_digest`, not the enclave signature passed to `submit_vote`. Accept only the configured signer's successful Ed25519 response, which upstream encodes as `{"scheme":"Ed25519","signature":[<64 byte integers>]}`. Decode that exact variant and 64 bytes before converting to lowercase hexadecimal. A public MPC receipt can contain this signature even when the registry callback subsequently returns `null`.

On a successful callback, after all response and eligibility checks, emit the design's `vote_signed` success event in NEP-297 style. Emit one log line with the exact ASCII prefix `EVENT_JSON:` followed by a compact JSON object of this shape:

```json
{
    "standard": "siyab_attest",
    "version": "1.0.0",
    "event": "vote_signed",
    "data": [{
        "scope_id": "<64 lowercase scope hex characters>",
        "approval_id": "<lowercase UUID>",
        "wallet_pubkey": "<exact validated wallet text>",
        "request_hash": "<64 lowercase request hash hex characters>",
        "verb": "approve",
        "evidence_hash": "<64 lowercase evidence hash hex characters>"
    }]
}
```

The envelope has exactly those four members. `data` contains exactly one object with exactly those six string fields. `scope_id` is derived from the validated intent; the other values are copied unchanged from it, with `verb` exactly `approve` or `reject`. The standard name, version and data fields are Siyab v1 choices. Emit exactly once per successful callback, including a successful same verb retry, and never at reservation, dispatch or refusal. A log counts only in a successful registry callback receipt; a failed callback can leave an MPC signature public without a registry success event.

Source for the event envelope and log prefix: https://github.com/near/NEPs/blob/master/neps/nep-0297.md#specification.

Sources for signer deposit, configurable gas requirement and response: https://github.com/near/mpc/blob/e691bdb98d68dcf74653da58d043453a6318765f/crates/near-mpc-contract-interface/src/deposits.rs, https://github.com/near/mpc/blob/e691bdb98d68dcf74653da58d043453a6318765f/crates/contract/src/api/common.rs and https://github.com/near/mpc/blob/e691bdb98d68dcf74653da58d043453a6318765f/crates/near-mpc-crypto-types/src/primitives.rs. The strict Ed25519 reference behavior is verified against https://docs.rs/ed25519-dalek/2.2.0/src/ed25519_dalek/verifying.rs.html.

The registry, key, measurement and platform set are deliberately included in the signed intent. An untrusted relayer cannot change any of them while retaining the enclave signature. Every vote field, including the nonce, recipient, evidence hash and expiry, is also signed. No arbitrary signing payload or callback URL is accepted.

Borsh writes string byte lengths and vector counts as four byte little endian integers, then writes their contents. Fixed arrays have no length prefix. The expiry is eight bytes in little endian order. The domain is outside the Borsh tuple and has no length prefix. These rules make field boundaries independent of separators inside a wallet key.

Source for Borsh encoding: https://borsh.io/

The official OutLayer specification marks the approval id as a UUID, provides `wallet_pubkey` as a string, and binds the wallet key and request hash into its vote message. It does not require that wallet key string to use Ed25519 encoding. V1 therefore preserves the string exactly instead of assuming an algorithm. Lowercase UUID spelling and the 256 byte printable bound are Siyab v1 requirements. Unsupported representations abstain instead of being normalized.

Source: https://raw.githubusercontent.com/out-layer/api-spec/main/openapi.yaml

## NEP-413 vote payload

The enclave's intent signature is distinct from the NEP-413 signature that a later voice implementation requests. The latter message is built from the validated structured intent:

```
message = verb + ":" + approval_id + ":" + wallet_pubkey + ":" + request_hash
nep413_digest = SHA256(
    u32_le(2^31 + 413) ||
    borsh_string(message) || nonce || borsh_string(recipient) || 0x00
)
```

Here `request_hash` is the 64 character lowercase hexadecimal spelling. The nonce is the 32 decoded bytes, without a vector length. The final byte encodes the absent optional callback URL. There is no callback string, extra field, newline or terminator after it. The tag bytes are `9d 01 00 80`. Only the fixed approve or reject grammar is built; ASK produces no intent or vote.

Sources: https://github.com/near/NEPs/blob/master/neps/nep-0413.md and https://raw.githubusercontent.com/out-layer/api-spec/main/openapi.yaml

## Revocation

The attestation registry has no owner and no global revocation. It cannot disable a measurement, machine, key or voice for every consumer, shorten all leases or erase permanent vote reservations. Lease expiry limits registry eligibility; it is not a recall of a signature that has already been released.

Revoke and deny policy live in each Gate. Its owner can deny an enclave key, measurement or PPID and remove a voice from that Gate's approved policy. Check the current local policy both before dispatch and when consuming an asynchronous result. Wallet owners likewise remove an approver identity from their own unlocked policy when its code or machine is no longer trusted. That action changes only that consumer's policy. Siyab must notify affected owners; it cannot switch their pinned identities off centrally. Missing or uncheckable eligibility stays UNKNOWN or ASK, never ALLOW.

This is the ownerless policy required by design section 5.2. No revocation API or production Gate integration is implemented by the T0 references.

## Executable vectors

`fixtures/attest/vectors.json` contains 137 synthetic cases. The original twelve cover three report data bindings, two measurements, three scopes, three vote intents with their NEP-413 payloads, and one key view. Further groups cover 66 TD attribute cases including every bit, nine issue time cases, seven block clock conversions, seven vote expiry cases, one `get_key` argument, two registration tuples, three vote submission envelopes and three Chain Signatures requests. The freeze also includes one immutable config, six renewals, six quote prefix offset cases, five signed vote policy cases, five per scope reservation sequences and four call budget and result cases. Domain 7 and the registration evidence are synthetic and do not assert deployed policy or accepted evidence. Only the vote policy and reservation groups carry verifiable enclave test signatures; other signature values illustrate encoding.

The byte vectors contain exact preimages, expected digests, representative `get_key` JSON bytes, both Borsh optional PCK chain encodings and exact vote path UTF-8 bytes. They include zero auxiliary data, a supplied TLS auxiliary digest, the maximum u64 time, one and eight platform bounds, opaque wallet key text and an informational advisory. These are format examples, not accepted attestations or network observations.

The small standard library reference is in `tests/test_attest_vectors.py`, reusing the repository's Ed25519 implementation for synthetic signature checks. Its tests reproduce every vector and refuse invalid field widths, unknown or missing fields, unsorted or duplicate platforms, noncanonical nonce base64, unsupported verbs and noncanonical integers. They check signed field binding, signature verification before policy or mutation, the recipient allowlist and permanent conflict reservations within each scope, including rotated keys. The reservation sequences show both opposite verb refusals and same verb retry after rotation, and a hostile measurement or platform set reserving first without blocking Siyab's reject. Dictionary member order does not change the specified field order. The TD attributes reference independently reproduces the full production predicate, including a recorded quote's TD report with DEBUG set. The modified quote and synthetic quote prefixes test the predicate and offsets, not quote signature validity. Version 5 tests read byte 174 and include a decoy DEBUG at byte 168. Lease tests cover both inclusive issue edges, each adjacent refusal, a ten day future issue time, times near epoch zero, delayed registration, collateral capping and expired relays. The overflow case has issue time equal to registration time at u64 maximum minus one and a two millisecond lease; it fails on checked addition alone. Renewal tests refuse an older quote within two hours, equal issue time and identity changes, preserve the existing record on refusal and exercise both callback orders. Vote expiry tests cover both clock boundaries and intent expiry at or after key expiry. The key vector includes its policy `max_lease_ms` outside the eleven field view, with the policy available through the frozen config. The Rust tests in `contracts/attest` read the same JSON vectors independently; sandbox quote controls also check TD attributes on every report they accept.

Run the Python reference with `python -m unittest tests.test_attest_vectors -v`. T0 implements pure reference state transitions, not a deployable registry, live signer, enrollment or payment path.
