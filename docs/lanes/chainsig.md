# Chain Signatures derivation spike

21 Nov 2026. Public derivation and read-only signer queries are implemented in
`gate/chainsig/`. The guard below is a design, not an implemented signing path.
No signature request, transaction, deposit, private key or paid API was used.

## Result and scope

The standard library Python implementation derives secp256k1 and ed25519 child
public keys from a supplied signer root, predecessor account and UTF-8 path.
It encodes Ethereum EIP-55 addresses, Bitcoin P2WPKH, P2PKH and P2SH-P2WPKH
addresses on mainnet, testnet and regtest, and Solana ed25519 addresses.

There are independent answers for every implemented chain and encoding.
`tests/data/chainsig_reference.json` contains 13 input cases, 26 child keys,
13 Ethereum addresses, 117 Bitcoin addresses and 13 Solana addresses. The cases
include both NEAR networks, Sign domains 0/1 and 4/5, an empty path, a Unicode
path with a comma and a 64 character implicit predecessor. Six Keccak vectors
cover 0, 3, 135, 136, 137 and 272 bytes. Generator points and addresses are also
recorded.

The Bitcoin adapter in the reference derives P2WPKH. P2PKH and P2SH-P2WPKH
are encoding comparisons against its bitcoinjs-lib dependency, not claims
that the adapter prepares those transaction forms. Taproot signing, Schnorr,
P2WSH, multisig, CKD and ForeignTx are outside this implementation. Unknown
protocols, including RobustEcdsa, are refused. Resharing state is refused even
though the signer may serve requests during resharing. An address derivation
does not produce a payment verdict or authorize a transfer. No target-chain
transaction or MPC signature was tested.

## Reproduce

Run the offline tests and repository check from the root:

```text
python -m unittest tests.test_chainsig -v
python scripts/check_hygiene.py --all
```

Derive with a supplied public root without network access:

```text
python -m gate.chainsig --root ed25519:6vSEtQxrQj6txUMh33WC4ERyCWmNMRTdufDWAaDY3Un2 --predecessor gate.testnet --path solana-1
```

The explicit `--live` flag enables only views. These commands fetch the root
and compare the local child with the signer's `derived_public_key` result:

```text
python -m gate.chainsig --live --network testnet --domain-id 0 --predecessor example.testnet --path ethereum-1
python -m gate.chainsig --live --network mainnet --domain-id 1 --predecessor example.near --path ethereum-1
python -m gate.chainsig --live --network testnet --domain-id 5 --predecessor gate.testnet --path solana-1
```

No key, account login or wallet is needed. There is no sign or broadcast
command. Missing or inconsistent data aborts with an error and no addresses;
there is no cached-root or alternate-domain fallback. Supplied offline roots
are explicitly marked as supplied and are not contract cross-checked.

The runtime uses the standard library, including SHA3-256, SHA256 and
OpenSSL's RIPEMD160 through `hashlib`. This Python 3.12.10 installation exposes
RIPEMD160. A build without it refuses Bitcoin encoding rather than replacing
the hash. Keccak-256 and both public point derivations are Python code. The
existing `gate.base58`, `gate.canon.account_id` and `gate.ed25519` operations
are reused. These operations handle public data only.

The reference was `chainsig.js` 1.1.16 at commit
`17aec4f932ec5ca49a4a52a167dbb31a2b77710e`, Node 24.22.3 and npm 11.19.1.
The JSON records source URLs, npm integrity values, dependency versions,
exact roots, block hashes, inputs and results. The JS reproduction script
uses the published library's derivation functions and actual chain adapters;
it does not import the Python implementation. It also checks the two captured
answers in the upstream derivation test. Install into a disposable directory
within this worktree:

```text
npm install --prefix node_modules/chainsig-reference --ignore-scripts --no-audit --no-fund chainsig.js@1.1.16 bitcoinjs-lib@6.1.10 js-sha3@0.9.3 bs58@6.0.0 viem@2.58.4 elliptic@6.6.1 @noble/curves@1.9.7 @scure/base@1.2.6 @solana/web3.js@1.99.0
node tests/data/chainsig_reference.mjs node_modules/chainsig-reference --check
```

This check reads the recorded RPC observations; it does not contact a signer.

Replay the raw signer views through `rpc.Client` without network access:

```text
python -m tests.data.chainsig_record
```

To record the same read-only views again, name a new directory:

```text
python -m tests.data.chainsig_record --live --out node_modules/chainsig-rpc-new
```

The recording command requires `--live` and refuses an existing directory.
It reads the original block heights, checks their hashes and compares each
decoded reply with the reference. It calls only public views and block reads.
Historical network reproduction depends on the provider retaining those blocks;
offline replay uses the included responses. No transaction is sent.

## Signer and domain observations

The official signer accounts are `v1.signer` on mainnet and
`v1.signer-prod.testnet` on testnet [1]. Both reported version `3.19.0`.
The release source used below is pinned to
`86e2cc4d124397ec81f436b11f48bf9b6dfd062e`. A version string is not a proof
that the deployed wasm was built from that source; that comparison was not done.

Root observations in the reference came from these public RPC endpoints:

| Network | RPC | Final block height | Block hash |
| --- | --- | ---: | --- |
| Mainnet | https://rpc.mainnet.near.org | 225570535 | 7vsbUvtYtcbph4BRDrpR7tRzssToSnw67AauSJFwyuDD |
| Testnet | https://rpc.testnet.near.org | 278690713 | 23cq4rx54Cq1BLw6B7vnLGmdsX4xYaX9uCrbpg4RHtpZ |

Both registries contained the following domains, each with reconstruction
threshold 11 and `next_domain_id` 8. Domain ids identify separate distributed
keys; an id is not a curve number or a target-chain id [2].

| Domain ids | Protocol | Purpose | Curve |
| --- | --- | --- | --- |
| 0, 4 | CaitSith | Sign | secp256k1 |
| 1, 5 | Frost | Sign | ed25519 |
| 2, 6 | ConfidentialKeyDerivation | CKD | BLS12-381 |
| 3, 7 | CaitSith | ForeignTx | secp256k1 |

Solana derivation is supported by the deployed ed25519 Sign domains. All 26
reference child keys matched the deployed `derived_public_key` view at the
recorded root block hashes. These are additional independent answers, not
signing requests. The full root strings and the returned child strings are
in `tests/data/chainsig_reference.json`.

`tests/data/chainsig_rpc/` contains fresh `rpc.Client` record-mode captures at
those same heights: 26 `derived_public_key`, eight `public_key`, two `state`,
two `version` and two `config` replies. Each of the 40 view rows retains its
request, URL, fetch time and unmodified JSON-RPC response, including the raw
result byte array, block height and block hash. Two additional block-header
rows retain the fields that `rpc.Client` uses to check those hashes. These are
test data outside the application's `fixtures/` tree. They do not add a live
root fallback. All recorded calls were read-only; no transaction was sent.

The relevant view methods are `state({})`,
`public_key({"domain_id": id})` and
`derived_public_key({"path": path, "predecessor": account, "domain_id": id})`.
The public-key methods return JSON strings in NEAR key notation [3]. All calls
use JSON-RPC `query` with `request_type: call_function` and base64 JSON arguments
[4]. The original reference read `state` with finality `final`, then pinned
subsequent views to its returned block hash. The raw captures repeat all views,
including `state`, at those recorded heights. The Python live reader reads final
`state`, then pins subsequent views to its returned height using the existing
`rpc.Client.call_function` interface. The client's RPC validation checks each
pinned response hash against its block header. The live CLI uses the repository's
public FastNEAR RPC endpoints and a 30 second deadline.

Never omit `predecessor` from a reproducibility view: the view default is its
caller. Never omit `domain_id`: the key-view default is legacy domain 0 [3].
For signing, `domain_id` is required. `key_version` is a deprecated u32 alias
for the domain id, not another dimension of key derivation. Supplying both is
rejected. The current domain id itself is a u64 [2, 5].

The guide's unqualified `example.near` address examples do not match the current
mainnet root. For `ethereum-1`, both the pinned library and signer produce
`0xEF4Ea05e5c11057DeCC8E1bd6F5369f2acD91fA5`; for `ethereum-2`, they produce
`0x273EeB4d7651d9296ec5bD9A6cEeBCa219101cb8`. Use the root-specific committed
vectors, not the illustrative addresses in that guide [1, 6].

## Exact derivation and encodings

For a Sign domain, let `H` be the 32 byte result:

```text
H = SHA3-256(UTF8("near-mpc-recovery v0.1.0 epsilon derivation:" + predecessor + "," + path))
```

There is no added space, newline, normalization, BIP32 parsing, root key or
domain id in these hash input bytes. A BIP32-looking path remains a literal
string. Validating the predecessor as a NEAR account prevents a comma from
crossing the account/path boundary [7].

For secp256k1, epsilon is `int_be(H)`. The MPC implementation requires epsilon
below the secp256k1 group order `n`; it does not reduce an out-of-range hash.
The child is `root_point + epsilon * G`. Infinity is rejected. The reference
JS elliptic multiplication effectively reduces its scalar; the Python code
follows the stricter contract rule for the extremely rare out-of-range input.
Both normal vectors and explicit order-boundary tests cover this distinction
[6, 8].

For ed25519, epsilon is `int_le(H) mod L`, without private-key clamping. The
child is `root_point + epsilon * B`, encoded as the canonical compressed
Edwards point [6, 8]. Input identity, torsion, noncanonical encodings and
non-prime-order roots are refused. A derived identity is also refused.

Ethereum uses the final 20 bytes of Keccak-256 over the 64 uncompressed point
bytes without the SEC1 `04` prefix, then EIP-55 case encoding [9, 10]. Keccak
uses its original padding suffix; the derivation hash remains SHA3-256. Their
outputs must not be interchanged [7, 11].

Bitcoin uses compressed SEC1 keys. P2WPKH encodes `HASH160(key)` as witness
version zero with Bech32. P2PKH uses Base58Check version 0 on mainnet and 111
on testnet/regtest. P2SH-P2WPKH uses `HASH160(0x0014 || HASH160(key))`, with
version 5 on mainnet and 196 on testnet/regtest. HRPs are `bc`, `tb` and `bcrt`
[12, 13, 14]. Solana is Base58 of the 32 compressed ed25519 public-key bytes
[15]. The NEAR prefix `ed25519:` is omitted from its address.

## Guard design

Recommended: add a separate `GATECSv1` authorization path. The existing
`GATEv1` message in `gate/canon.py` and
`contracts/gate/src/verdict.rs` authorizes only NEAR native/FT transfers.
It has no MPC payload, path, domain or target-chain binding. It must not be
accepted as authorization to call MPC.

A future `Gate.request_chain_signature` would accept the unsigned transaction
and a verifier-signed ALLOW. Both the verifier and Gate must build or validate
the same transaction bytes and recompute the target signing payload. A friendly
receiver field beside an unrelated caller-supplied digest is insufficient.
Unrecognized transaction types, scripts, programs or incomplete source checks
must result in ASK or UNKNOWN and no signing promise.

The new authorization must bind the following fields using fixed-width
integers and explicit byte lengths for every variable field. Do not join raw
path strings with a separator. The exact schema and shared Python/Rust vectors
must land with the future guard implementation.

| Binding | Required content |
| --- | --- |
| Authorization scope | `GATECSv1`, NEAR network, Gate account, registered agent public key, literal ALLOW |
| Signing identity | MPC contract, u64 domain id, protocol/curve and expected root public key |
| Derivation | Predecessor fixed to the Gate account and exact UTF-8 path bytes |
| Target | Chain and network identifier, transaction format and complete unsigned transaction commitment |
| Payload | Ecdsa/Eddsa tag, byte length, commitment to the exact bytes sent to MPC |
| Bitcoin input | Input index, verified previous output amount/script and exact sighash type for each requested signature |
| Verdict | Issue/expiry times, one-use verdict id, evidence hash and verifier key id |

For example, a proposed payload commitment is
`SHA256(variant_byte || u32_le(payload_length) || payload_bytes)`. This is an
authorization commitment only. Do not send that extra SHA256 to MPC in place
of the chain's actual payload. The complete unsigned transaction commitment
must also cover fee, nonce, chain/network, calldata/instructions, all outputs
and change. Every Bitcoin input payload in a multi-input transfer must belong
to that same reviewed transaction and authorization.

The predecessor seen by MPC is the Gate contract when it makes the cross-call,
not the agent or human who originally called the Gate. `sign` computes the
tweak from that actual predecessor and `request.path`; it accepts no caller
override (`api/sign.rs:62-67` and `api/common.rs:67-78` at the pinned commit)
[16, 21]. MPC identifies the calling account, not the Gate method that created
the promise, and it does not evaluate Siyab policy. Use a distinct approved path
per target chain/network to avoid exposing the same signing identity across
incompatible replay domains [17].

This binding requires all three preconditions below. They are requirements for
the future signing guard, not protections implemented by this derivation spike.

| Precondition | Required enforcement and acceptance check |
| --- | --- |
| No Gate full-access keys | After deployment and initialization, remove every full-access key before enabling signing or funding its derived accounts. Exclude full-access gas keys too. Remaining function-call keys must name the Gate and only approved guarded methods. Read the complete finalized `view_access_key_list`, including every page, and `view_account` at the same block to check permissions and the approved code hash. Missing pages, unknown permissions or a full-access key mean ASK and signing stays disabled [29, 30, 31]. |
| No other Gate method can call the signer | Only the guarded sign request may create a promise to the approved signer account. Other transfer, withdrawal and forwarding paths must reject that account as a receiver. Fix the guarded request's receiver, `sign` method and validated request bytes in code. Review all exported methods, callbacks, administrative methods and key-management paths for generic promise forwarding, self-call escapes or ways to add unrestricted keys. Match the deployed code hash to the reviewed reproducible wasm. Sandbox tests must attempt bypasses through every other entry point and confirm no signer promise is created. A method that can forward arbitrary calls violates this condition. |
| Upgrades obey the same policy | Keep no account key that can deploy replacement code. An immutable Gate needs no upgrade route. If upgrades are added, the same authorization policy must approve a distinct upgrade action bound to the replacement wasm hash, Gate, network, policy generation and one-use authorization. Consume it before deployment and preserve the no-key and sole-sign-route invariants in the reviewed replacement. Pause signing while acceptance checks are incomplete. An owner-only deployment bypass is insufficient [29, 32]. |

A key-list or code-hash snapshot checks one block, not a permanent invariant.
The reviewed code must also preserve these restrictions after that check.
The current Gate has no signing or programmatic upgrade method. Its agent key
registration restricts keys to `request_transfer`
(`contracts/gate/src/policy.rs:83-89`). However, the current setup does not remove
the deployment key (`scripts/setup_testnet.sh:104-114`), and the redeploy script
uses a Gate account key (`scripts/redeploy.sh:80-83`). This spike neither changes
those keys nor claims that an existing deployment satisfies the preconditions.

The future Gate would use these methods, not `respond` or the MPC's internal
yield callback:

1. Read/configure an approved signer contract, domain, root and protocol using
   `state`, `public_key`, `derived_public_key`, `version` and `config` views.
   Keep the expected identity in Gate policy. A refresh must never silently
   replace an approved root or make an unknown domain eligible.
2. Rebuild the payload, verify the new ALLOW signature, Gate/agent binding,
   policy, expiry, evidence commitment and unused verdict id.
3. Consume the verdict id, reserve policy exposure and save an immutable
   request record before scheduling the external `sign` promise.
4. Call the approved MPC account's `sign` with exactly the saved path, domain
   and payload. ECDSA wire form is
   `{"request":{"path":path,"payload_v2":{"Ecdsa":digest_hex},"domain_id":id}}`.
   EdDSA uses `{"Eddsa":message_hex}` in `payload_v2` [5, 16].
5. In a private Gate callback, correlate the request id, commitment and policy
   generation, and verify the returned signature under the expected derived
   key and exact payload before releasing it to the relayer. A callback may
   never substitute a new path, domain or transaction.

For Ethereum, sign the actual EIP-1559 signing digest with its chain id,
nonce, destination, amount, data, access list and fee fields. For Bitcoin,
require supported P2WPKH inputs and `SIGHASH_ALL` without `ANYONECANPAY`, and
validate previous outputs, every output, change and fee. Reject partial
sighash forms that omit reviewed commitments. For Solana, sign the serialized
message itself, including the recent blockhash, fee payer, account keys and
all instructions; do not sign a prehash. Resolve and commit lookup-table
accounts if versioned messages are admitted [18, 19, 20].

The deposit, gas and payload figures have separate sources at pinned commit
`86e2cc4d124397ec81f436b11f48bf9b6dfd062e`:

| Figure | Exact source or recorded view |
| --- | --- |
| Minimum deposit 1 yoctoNEAR | `crates/near-mpc-contract-interface/src/deposits.rs:4` sets `SIGN`; `crates/contract/src/api/sign.rs:35-40` uses it. `crates/contract/src/api/common.rs:131-151` enforces the minimum and refunds excess to the predecessor [16, 21]. |
| Minimum sign receipt gas 15 Tgas | The two recorded `config({})` views at mainnet block 225570535 and testnet block 278690713 return `sign_call_gas_attachment_requirement_tera_gas: 15`. Their block hashes are in the table above and in each raw reply. `crates/contract/src/config.rs:13-14,60-61` defines the default and configurable field; `crates/contract/src/api/common.rs:56-65` enforces the configured minimum [21, 33]. |
| ECDSA payload exactly 32 bytes | `crates/near-mpc-crypto-types/src/primitives.rs:63-65,89-98` defines and checks the fixed size [23]. |
| ECDSA payload integer from zero inclusive to group order `n` exclusive | `crates/contract/src/api/sign.rs:45-50` uses `Scalar::from_repr`. The pinned workspace selects `k256` 0.13.4 in `Cargo.toml:166`; that version's `arithmetic/scalar.rs:328-335` decodes big endian and checks below the order [16, 34, 35]. |
| EdDSA payload zero through 1232 bytes | `crates/near-mpc-crypto-types/src/primitives.rs:67-68,99-106` defines and checks the inclusive maximum [23]. Empty messages do not constitute a valid Solana transfer [20]. |

A Gate cross-call would attach the deposit from the Gate balance; an agent
function-call key does not need permission to attach funds itself. The gas
figure applies to the signer receipt, not a measured end-to-end Gate budget.
Budget validation and callback gas separately and read config again before any
future execution. No current required-fee view exists in the pinned catalog [22].
The deposit and payload bounds were checked in source and gas in read-only views.
No signature request or transaction was sent to test any of these figures.

Signing issues a public capability. A failed callback or signer timeout cannot
prove that no usable signature exists. Keep the verdict consumed and exposure
reserved across such failures; do not copy the current transfer callback's
automatic cap refund into this path. A separate reconciliation decision needs
target-chain evidence. NEAR receipts execute asynchronously with separate
rollback, and the signer itself has a timeout path [24, 25]. Gate expiry limits
when it may authorize a signing request; it cannot revoke that request or an
already published signature. MPC completion may occur after the Gate expiry.
Target-chain nonce, spent UTXO or recent-blockhash rules must enforce replay
bounds. A returned signature must never be reported as payment execution or
target-chain finality.

Under all three preconditions above, the design would close approval
substitution: an agent obtains ALLOW for one transfer, then changes its receiver,
amount, calldata, outputs, instructions, fee, path or domain before asking MPC
to sign. Consuming the bound authorization before the asynchronous call also
closes duplicate issuance and callback replacement.

A retained Gate full-access key can call the signer directly from that same
Gate account or deploy bypass code. A second Gate method can create the same
signer predecessor without checking ALLOW. An upgrade outside the policy can
introduce either route. Each defeats the claimed binding even when the guarded
method's authorization encoding is correct. The signer cannot distinguish
these routes; every precondition must be enforced and checked before enabling
signing. An unchecked precondition means ASK or UNKNOWN, never ALLOW.

## Validation and remaining work

`python -m unittest tests.test_chainsig` passed all 46 tests with no skips.
`python scripts/check_hygiene.py --all` passed with exit 0. The JS runner
passed all 13 derivation cases,
six Keccak vectors, generator primitives, two captured upstream answers and
26 recorded deployed child-key replies. Independent live CLI checks passed
mainnet domains 0 and 1 and testnet domains 0 and 5. A separate reviewer also
ran the testnet domain 5 CLI. No full repository suite was run in this lane.

The fix-round regression tests first reproduced the parser and missing-hash
diagnostic defects. The 46-test pass includes offline replay of all 40 captured
view replies with network access blocked, checks of every recorded block context
and raw result byte array, and recording CLI flag checks. The separate command
`python -m tests.data.chainsig_record` also replayed all 40 views successfully.
The original invalid-path-before-network test remains and passes.

The guard requires a separate implementation and review before any signing
experiment: a new canonical authorization schema with shared vectors,
transaction parsing/validation, Gate storage and asynchronous state machine,
signature checks, target-chain replay handling and an explicit execution flag
off by default. There is no owner signing command for this spike because no
such execution path was added. The orchestrator owns the full suite and
integration of these uncommitted files.

## Sources

1. https://docs.near.org/chain-abstraction/chain-signatures/implementation
2. https://github.com/near/mpc/blob/86e2cc4d124397ec81f436b11f48bf9b6dfd062e/crates/primitives/src/domain.rs
3. https://github.com/near/mpc/blob/86e2cc4d124397ec81f436b11f48bf9b6dfd062e/crates/contract/src/api/keys.rs
4. https://docs.near.org/api/rpc/contracts
5. https://github.com/near/mpc/blob/86e2cc4d124397ec81f436b11f48bf9b6dfd062e/crates/near-mpc-crypto-types/src/sign.rs
6. https://github.com/NearDeFi/chainsig.js/blob/17aec4f932ec5ca49a4a52a167dbb31a2b77710e/src/utils/cryptography.ts
7. https://github.com/near/mpc/blob/86e2cc4d124397ec81f436b11f48bf9b6dfd062e/crates/near-mpc-crypto-types/src/kdf.rs
8. https://github.com/near/mpc/blob/86e2cc4d124397ec81f436b11f48bf9b6dfd062e/crates/contract/src/crypto_shared/kdf.rs
9. https://github.com/NearDeFi/chainsig.js/blob/17aec4f932ec5ca49a4a52a167dbb31a2b77710e/src/chain-adapters/EVM/EVM.ts
10. https://eips.ethereum.org/EIPS/eip-55
11. https://keccak.team/keccak_specs_summary.html
12. https://github.com/NearDeFi/chainsig.js/blob/17aec4f932ec5ca49a4a52a167dbb31a2b77710e/src/chain-adapters/Bitcoin/Bitcoin.ts
13. https://github.com/bitcoinjs/bitcoinjs-lib/tree/91800d465c79e8069db728191beb548528691a4e/ts_src/payments
14. https://bips.dev/173/ and https://bips.dev/13/ and https://bips.dev/141/
15. https://github.com/NearDeFi/chainsig.js/blob/17aec4f932ec5ca49a4a52a167dbb31a2b77710e/src/chain-adapters/Solana/Solana.ts
16. https://github.com/near/mpc/blob/86e2cc4d124397ec81f436b11f48bf9b6dfd062e/crates/contract/src/api/sign.rs#L35-L67
17. https://docs.near.org/chain-abstraction/chain-signatures#derivation-paths-one-account-multiple-chains
18. https://eips.ethereum.org/EIPS/eip-1559
19. https://github.com/bitcoin/bips/blob/master/bip-0143.mediawiki#specification
20. https://solana.com/docs/core/transactions/transaction-structure
21. https://github.com/near/mpc/blob/86e2cc4d124397ec81f436b11f48bf9b6dfd062e/crates/near-mpc-contract-interface/src/deposits.rs#L4 and https://github.com/near/mpc/blob/86e2cc4d124397ec81f436b11f48bf9b6dfd062e/crates/contract/src/api/common.rs#L56-L78 and https://github.com/near/mpc/blob/86e2cc4d124397ec81f436b11f48bf9b6dfd062e/crates/contract/src/api/common.rs#L131-L151
22. https://github.com/near/mpc/blob/86e2cc4d124397ec81f436b11f48bf9b6dfd062e/crates/near-mpc-contract-interface/src/method_names.rs
23. https://github.com/near/mpc/blob/86e2cc4d124397ec81f436b11f48bf9b6dfd062e/crates/near-mpc-crypto-types/src/primitives.rs#L63-L106
24. https://docs.near.org/smart-contracts/anatomy/crosscontract
25. https://github.com/near/mpc/blob/86e2cc4d124397ec81f436b11f48bf9b6dfd062e/crates/contract/src/api/sign.rs#L191
26. https://github.com/NearDeFi/chainsig.js/blob/17aec4f932ec5ca49a4a52a167dbb31a2b77710e/__tests__/contracts/derivation.test.ts
27. https://registry.npmjs.org/chainsig.js/1.1.16
28. https://www.secg.org/sec2-v2.pdf
29. https://docs.near.org/protocol/accounts-contracts/access-keys#full-access-keys
30. https://docs.near.org/api/rpc/contracts#view-access-key-list and https://docs.near.org/api/rpc/contracts#view-account
31. https://docs.near.org/protocol/accounts-contracts/gas-keys
32. https://docs.near.org/smart-contracts/release/upgrade#programmatic-update
33. https://github.com/near/mpc/blob/86e2cc4d124397ec81f436b11f48bf9b6dfd062e/crates/contract/src/config.rs#L13-L14 and https://github.com/near/mpc/blob/86e2cc4d124397ec81f436b11f48bf9b6dfd062e/crates/contract/src/config.rs#L60-L61
34. https://github.com/near/mpc/blob/86e2cc4d124397ec81f436b11f48bf9b6dfd062e/Cargo.toml#L166 and https://github.com/near/mpc/blob/86e2cc4d124397ec81f436b11f48bf9b6dfd062e/crates/contract/Cargo.toml#L88
35. https://docs.rs/k256/0.13.4/src/k256/arithmetic/scalar.rs.html#328-335

Every additional reference encoder source and exact dependency integrity is
listed in `tests/data/chainsig_reference.json` under `provenance`.
