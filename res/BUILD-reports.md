reproducible build

## Source and build environment

Source commit: TBD by the orchestrator at deploy

- cargo-near: 0.22.0.
- The repository toolchain is Rust 1.99.1 with `wasm32-unknown-unknown`.
- The crate manifest names the reproducible build image and build command.
- Reproducing the wasm needs Docker and the cargo-near build image.

## Verification recipe

Check out the source commit recorded above, then build from the repository root:

```sh
unset CARGO_TARGET_DIR
(cd contracts/reports && cargo near build reproducible-wasm)
sha256sum contracts/reports/target/near/reports.wasm
```

The orchestrator fills in the source commit after the build. The build output is written inside the crate, so `CARGO_TARGET_DIR` must be unset for the reproducible build command.
