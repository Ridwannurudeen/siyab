reproducible build

## Source and build environment

Source commit: eb54c8e11c2916201af1d13ce7b92e1c8c8c473b

Deployed: 2 Nov, `registry.kestrelridge-main.testnet`, code_hash `3qGvVjTHVyvYVGQjJhqQGJMzmkUHwEbTCfWqHhXnW4VX` MATCH (transaction `9GzMDrYe...UPBb66`, `scripts/redeploy.sh`, without an init call, state kept).

- cargo-near: 0.22.0.
- Image: `sourcescan/cargo-near:0.21.1-rust-1.96.0`, digest `sha256:ccb22bb4e677ed022d8b9d1aa1b32f0af52dd0471ef1c32e48dedbf68ee6ee17`.
- The container compiles with Rust 1.96.0 from the image. `rust-toolchain.toml` pins Rust 1.99.1 for local builds, so a local non reproducible build produces a different hash.
- Registry wasm: 157180 bytes; sha256 `2a154191...f0ec0cfa`; code_hash `3qGvVjTHVyvYVGQjJhqQGJMzmkUHwEbTCfWqHhXnW4VX`.

## Verification recipe

Clone this repository, check out the source commit above, and build the crate with the same image:

```sh
git clone https://github.com/Ridwannurudeen/shieldbot-near shieldbot-near-repro
cd shieldbot-near-repro
git checkout eb54c8e11c2916201af1d13ce7b92e1c8c8c473b
unset CARGO_TARGET_DIR
(cd contracts/registry && cargo near build reproducible-wasm)
sha256sum contracts/registry/target/near/registry.wasm
```

Compare the printed sha256 with the first field in `res/registry.wasm.sha256`. The checksum file names `res/registry.wasm` for `sha256sum -c` from the repository root.

reproducing the hash from source needs Docker; checking it against testnet needs network
