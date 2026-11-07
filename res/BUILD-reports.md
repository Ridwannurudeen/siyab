reproducible build

## Source and build environment

Source commit: 2d647e544ccc164c14f9ad72096f8ec5cbcfcbd8

Deployed: pending

- cargo-near: 0.22.0.
- The repository toolchain is Rust 1.99.1 with `wasm32-unknown-unknown`.
- The crate manifest names the reproducible build image and build command.
- Reproducing the wasm needs Docker and the cargo-near build image.
- Reports wasm: 198649 bytes; sha256 `50570436...f4e19763` (full value in `res/reports.wasm.sha256`); code_hash `6QcaGeoCN7QXe2ZV8VAL4ZHTRYWTURJYQmdDD2tBqBpN`. Two independent clones at the source commit built byte identical wasm on 7 Nov.

## Verification recipe

Check out the source commit recorded above, then build from the repository root:

```sh
unset CARGO_TARGET_DIR
(cd contracts/reports && cargo near build reproducible-wasm)
sha256sum contracts/reports/target/near/reports.wasm
```

The orchestrator fills in the source commit after the build. The build output is written inside the crate, so `CARGO_TARGET_DIR` must be unset for the reproducible build command.
