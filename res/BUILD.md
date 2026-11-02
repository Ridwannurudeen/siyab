reproducible build

## Source and build environment

Source commit: eb54c8e11c2916201af1d13ce7b92e1c8c8c473b

The source link embedded by NEP-330 is `https://github.com/Ridwannurudeen/shieldbot-near/tree/eb54c8e11c2916201af1d13ce7b92e1c8c8c473b`.

- cargo-near: 0.22.0.
- Image: `sourcescan/cargo-near:0.21.1-rust-1.96.0`, digest `sha256:ccb2...ee17`.
- The container compiles with Rust 1.96.0 from the image. `rust-toolchain.toml` pins Rust 1.99.1 for local builds, so a local non reproducible build produces a different hash.
- Gate wasm: 241330 bytes; sha256 `f7a09b28...fd14a107`; code_hash `Hfdk3jGUFxQyP8wEEoejNUkZ81bE9gDmoUBfGRghR5iE`.
- Registry wasm: 157180 bytes; sha256 `2a154191...f0ec0cfa`; code_hash `3qGvVjTHVyvYVGQjJhqQGJMzmkUHwEbTCfWqHhXnW4VX`.

## Verification recipe

Clone this repository, check out the source commit above, and build both crates with the same image:

```sh
git clone https://github.com/Ridwannurudeen/shieldbot-near shieldbot-near-repro
cd shieldbot-near-repro
git checkout eb54c8e11c2916201af1d13ce7b92e1c8c8c473b
unset CARGO_TARGET_DIR
(cd contracts/gate && cargo near build reproducible-wasm)
(cd contracts/registry && cargo near build reproducible-wasm)
sha256sum contracts/gate/target/near/gate.wasm contracts/registry/target/near/registry.wasm
```

Compare the two printed sha256 values with the first field in `res/gate.wasm.sha256` and `res/registry.wasm.sha256`. The checksum files name `res/gate.wasm` and `res/registry.wasm` for `sha256sum -c` from the repository root.

reproducing the hash from source needs Docker; checking it against testnet needs network

Testnet code_hash after redeploy: pending
