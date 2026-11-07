reproducible build

## Source and build environment

Source commit: 937615878543e9f0d45eeb5002b65f88e3fc3757

Deployed: pending

Previous deploy, version 0.1.0 from source commit 2d647e544ccc164c14f9ad72096f8ec5cbcfcbd8, now `res/previous/reports.wasm`: 7 Nov, `reports.kestrelridge-main.testnet`, code_hash `6QcaGeoCN7QXe2ZV8VAL4ZHTRYWTURJYQmdDD2tBqBpN` MATCH (account created by `kestrelridge-main.testnet` with 3 NEAR, transaction `Cza55oWj...gcDwKF`; deploy with init `new(owner kestrelridge-owner.testnet, moderators [kestrelridge-owner.testnet])`, transaction `AMfe5SLG...nLCWCg`).

- cargo-near: 0.22.0.
- The repository toolchain is Rust 1.99.1 with `wasm32-unknown-unknown`.
- The crate manifest names the reproducible build image and build command.
- Reproducing the wasm needs Docker and the cargo-near build image.
- Reports wasm: 198593 bytes; sha256 `e28ac3fc...b4737134` (full value in `res/reports.wasm.sha256`); code_hash `GFKs9k5P6raHNxN6suXJNJByjsYdXAZdGdJE5a5duZes`; version 0.1.1. Two independent clones at the source commit built byte identical wasm on 7 Nov.
- Previous reports wasm: 198649 bytes; sha256 `50570436...f4e19763` (full value in `res/previous/reports.wasm.sha256`); code_hash `6QcaGeoCN7QXe2ZV8VAL4ZHTRYWTURJYQmdDD2tBqBpN`; version 0.1.0, source commit 2d647e544ccc164c14f9ad72096f8ec5cbcfcbd8.

## Verification recipe

Check out the source commit recorded above, then build from the repository root:

```sh
unset CARGO_TARGET_DIR
(cd contracts/reports && cargo near build reproducible-wasm)
sha256sum contracts/reports/target/near/reports.wasm
```

The orchestrator fills in the source commit after the build. The build output is written inside the crate, so `CARGO_TARGET_DIR` must be unset for the reproducible build command.
