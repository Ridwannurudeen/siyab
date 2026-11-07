reproducible build

## Source and build environment

Source commit: 16a899392687e405d0308b7712c7501b755a30d6

Deployed: pending

Previous deploy, version 0.1.0 from source commit eb54c8e11c2916201af1d13ce7b92e1c8c8c473b, now `res/previous/gate.wasm` and still the code on testnet: 2 Nov, `kestrelridge-main.testnet`, code_hash `Hfdk3jGUFxQyP8wEEoejNUkZ81bE9gDmoUBfGRghR5iE` MATCH (transaction `GTHR62ER...Wik5sW`, `scripts/redeploy.sh`, without an init call, state kept).

The source link embedded by NEP-330 is `https://github.com/Ridwannurudeen/shieldbot-near/tree/16a899392687e405d0308b7712c7501b755a30d6`.

- cargo-near: 0.22.0.
- Image: `sourcescan/cargo-near:0.21.1-rust-1.96.0`, digest `sha256:ccb22bb4e677ed022d8b9d1aa1b32f0af52dd0471ef1c32e48dedbf68ee6ee17`.
- The container compiles with Rust 1.96.0 from the image. `rust-toolchain.toml` pins Rust 1.99.1 for local builds, so a local non reproducible build produces a different hash.
- Gate wasm: 260751 bytes; sha256 `63224c4f...644690a8`; code_hash `7fyhLcFm4nsof4gbfS5zN77cartyrjCBdKBtYRnSV3u9`; version 0.2.0. Two independent clones at the source commit built byte identical wasm on 7 Nov.
- Previous gate wasm: 241330 bytes; sha256 `f7a09b28...fd14a107`; code_hash `Hfdk3jGUFxQyP8wEEoejNUkZ81bE9gDmoUBfGRghR5iE`; version 0.1.0, source commit eb54c8e11c2916201af1d13ce7b92e1c8c8c473b.

## Verification recipe

Clone this repository, check out the source commit above, and build the crate with the same image:

```sh
git clone https://github.com/Ridwannurudeen/shieldbot-near shieldbot-near-repro
cd shieldbot-near-repro
git checkout 16a899392687e405d0308b7712c7501b755a30d6
unset CARGO_TARGET_DIR
(cd contracts/gate && cargo near build reproducible-wasm)
sha256sum contracts/gate/target/near/gate.wasm
```

Compare the printed sha256 with the first field in `res/gate.wasm.sha256`. The checksum file names `res/gate.wasm` for `sha256sum -c` from the repository root.

reproducing the hash from source needs Docker; checking it against testnet needs network

The Gate's `contract_source_metadata` view returns the NEP-330 link to the source commit above.
