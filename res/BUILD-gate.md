reproducible build

## Source and build environment

Source commit: 57860c96f071fac0f7bb99f242fcae7e6c4430d2

Deployed: 9 Nov, `kestrelridge-main.testnet`, version 0.2.2, code_hash `5EmM2UQRYcvaNvJX6jfcgtNS7Mm8ZPjZhwK1J8bMXrcu` MATCH (layout preserving redeploy without an init call, transaction `HnFAFtxq...1YKTvU`; FINAL success, no failed receipt; policy, agent configuration, signing public keys and token balances unchanged; storage grew by 531 bytes and the native amount decrease exactly matched total transaction and receipt fees). Public readbacks and transaction proof fields are in `fixtures/runs/gate-022-readbacks-20261109.json` and `fixtures/runs/gate-022-transactions-20261109.json`; the verified summary is `fixtures/runs/gate-022-deploy-20261109.txt`.

Previous deploy, version 0.2.1 from source commit c2d42c02d1942d39fa0fa2651d3d1652875ffdcc, now `res/previous/gate.wasm` and the rollback for 0.2.2: 7 Nov, `kestrelridge-main.testnet`, code_hash `8EGqzorvbx5PiPCnLJCbi79p9HWsie93iwgCT6GjStL5` MATCH (layout preserving redeploy without an init call over the 0.1.0 state, transaction `2N6HEK3d...EBW9R1`; `version` read 0.1.0 before and 0.2.1 after; policy, verifier key, agent and used verdict ids read back unchanged).

The source link embedded by NEP-330 is `https://github.com/Ridwannurudeen/shieldbot-near/tree/57860c96f071fac0f7bb99f242fcae7e6c4430d2`.

Current Gate source is 0.3.0 (not deployed). The source commit, build recipe and artifact in this record remain the reproducible 0.2.2 build deployed on 9 Nov. No 0.3.0 artifact has been promoted.

- cargo-near: 0.22.0.
- Image: `sourcescan/cargo-near:0.21.1-rust-1.96.0`, digest `sha256:ccb22bb4e677ed022d8b9d1aa1b32f0af52dd0471ef1c32e48dedbf68ee6ee17`.
- The container compiles with Rust 1.96.0 from the image. `rust-toolchain.toml` pins Rust 1.99.1 for local builds, so a local non reproducible build produces a different hash.
- Gate wasm: 264604 bytes; sha256 `3ef57087...622f0b76`; code_hash `5EmM2UQRYcvaNvJX6jfcgtNS7Mm8ZPjZhwK1J8bMXrcu`; version 0.2.2. Two independent clean clones at the source commit built byte identical wasm on 9 Nov (`fixtures/runs/reproducible-gate-0.2.2-20261109-clones.txt`; the build output of each clone is in `fixtures/runs/reproducible-gate-0.2.2-20261109-clone1.txt` and `fixtures/runs/reproducible-gate-0.2.2-20261109-clone2.txt`).
- Previous gate wasm: 264073 bytes; sha256 `6b684655...9d000aa6`; code_hash `8EGqzorvbx5PiPCnLJCbi79p9HWsie93iwgCT6GjStL5`; version 0.2.1, source commit c2d42c02d1942d39fa0fa2651d3d1652875ffdcc. Its original two-clone build record remains in `fixtures/runs/reproducible-gate-0.2.1-20261107-clones.txt`.

The exact 0.2.2 artifact passed the layout preserving upgrade and rollback sandbox with the deployed 0.2.1 artifact. The harness correction committed as `9d4b6342` waits for the prior 0.2.1 queue head before the pruning check; it changes only the sandbox test, not runtime source or manifests. The artifact remains pinned to source commit `57860c96f071fac0f7bb99f242fcae7e6c4430d2`. The run checks both native memo refusals and the absent-token refusal without a scheduled transfer, used verdict or cap debit (`fixtures/runs/cargo-gate-0.2.2-sandbox-20261109.txt`). All 73 unit tests passed (`fixtures/runs/cargo-gate-0.2.2-unit-20261109.txt`).

## Verification recipe

Clone this repository, check out the source commit above, and build the crate with the same image:

```sh
git clone https://github.com/Ridwannurudeen/shieldbot-near shieldbot-near-repro
cd shieldbot-near-repro
git checkout 57860c96f071fac0f7bb99f242fcae7e6c4430d2
unset CARGO_TARGET_DIR
(cd contracts/gate && cargo near build reproducible-wasm)
sha256sum contracts/gate/target/near/gate.wasm
```

Compare the printed sha256 with the first field in `res/gate.wasm.sha256`. The checksum file names `res/gate.wasm` for `sha256sum -c` from the repository root.

reproducing the hash from source needs Docker; checking it against testnet needs network

The Gate's `contract_source_metadata` view returns the NEP-330 link to the source commit above.
