reproducible build

## Source and build environment

Source commit: c2d42c02d1942d39fa0fa2651d3d1652875ffdcc

Deployed: 7 Nov, `kestrelridge-main.testnet`, version 0.2.1, code_hash `8EGqzorvbx5PiPCnLJCbi79p9HWsie93iwgCT6GjStL5` MATCH (layout preserving redeploy without an init call over the 0.1.0 state, transaction `2N6HEK3d...EBW9R1`; `version` read 0.1.0 before and 0.2.1 after; policy, verifier key, agent and used verdict ids read back unchanged).

Previous deploy, version 0.1.0 from source commit eb54c8e11c2916201af1d13ce7b92e1c8c8c473b, now `res/previous/gate.wasm` and the rollback for 0.2.1: 2 Nov, `kestrelridge-main.testnet`, code_hash `Hfdk3jGUFxQyP8wEEoejNUkZ81bE9gDmoUBfGRghR5iE` MATCH (transaction `GTHR62ER...Wik5sW`, `scripts/redeploy.sh`, without an init call, state kept).

The source link embedded by NEP-330 is `https://github.com/Ridwannurudeen/shieldbot-near/tree/c2d42c02d1942d39fa0fa2651d3d1652875ffdcc`.

- cargo-near: 0.22.0.
- Image: `sourcescan/cargo-near:0.21.1-rust-1.96.0`, digest `sha256:ccb22bb4e677ed022d8b9d1aa1b32f0af52dd0471ef1c32e48dedbf68ee6ee17`.
- The container compiles with Rust 1.96.0 from the image. `rust-toolchain.toml` pins Rust 1.99.1 for local builds, so a local non reproducible build produces a different hash.
- Gate wasm: 264073 bytes; sha256 `6b684655...9d000aa6`; code_hash `8EGqzorvbx5PiPCnLJCbi79p9HWsie93iwgCT6GjStL5`; version 0.2.1. Two independent clones at the source commit built byte identical wasm on 7 Nov.
- Previous gate wasm: 241330 bytes; sha256 `f7a09b28...fd14a107`; code_hash `Hfdk3jGUFxQyP8wEEoejNUkZ81bE9gDmoUBfGRghR5iE`; version 0.1.0, source commit eb54c8e11c2916201af1d13ce7b92e1c8c8c473b.

## Verification recipe

Clone this repository, check out the source commit above, and build the crate with the same image:

```sh
git clone https://github.com/Ridwannurudeen/shieldbot-near shieldbot-near-repro
cd shieldbot-near-repro
git checkout c2d42c02d1942d39fa0fa2651d3d1652875ffdcc
unset CARGO_TARGET_DIR
(cd contracts/gate && cargo near build reproducible-wasm)
sha256sum contracts/gate/target/near/gate.wasm
```

Compare the printed sha256 with the first field in `res/gate.wasm.sha256`. The checksum file names `res/gate.wasm` for `sha256sum -c` from the repository root.

reproducing the hash from source needs Docker; checking it against testnet needs network

The Gate's `contract_source_metadata` view returns the NEP-330 link to the source commit above.
