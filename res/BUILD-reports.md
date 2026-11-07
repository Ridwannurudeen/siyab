reproducible build

## Source and build environment

Source commit: 5abc782b2126ac4cc91001b20ecb89e78fca11f8

Deployed: 7 Nov, `reports.kestrelridge-main.testnet`, version 0.1.2, code_hash `DXdqac6HBiXtZVCrJjdGjDEFfzcFF7EEDza8h8rU5UCC` MATCH (layout preserving redeploy without an init call over the 0.1.1 state, transaction `HDsZJNZV...aRiuXS`; `version` read 0.1.1 before and 0.1.2 after, `state_version` 1 and `report_deposit` 0.03 NEAR after, `fixtures/runs/reports-012-readback-20261107.txt`).

Previous deploy, version 0.1.1 from source commit 937615878543e9f0d45eeb5002b65f88e3fc3757, now `res/previous/reports.wasm` and the rollback for 0.1.2: 7 Nov, `reports.kestrelridge-main.testnet`, code_hash `GFKs9k5P6raHNxN6suXJNJByjsYdXAZdGdJE5a5duZes` MATCH (layout preserving redeploy without an init call over the 0.1.0 state, transaction `59PSfJtQ...LzsErK`; `version` read 0.1.0 before and 0.1.1 after, `state_version` 1 after).

Earlier deploy, version 0.1.0 from source commit 2d647e544ccc164c14f9ad72096f8ec5cbcfcbd8: 7 Nov, `reports.kestrelridge-main.testnet`, code_hash `6QcaGeoCN7QXe2ZV8VAL4ZHTRYWTURJYQmdDD2tBqBpN` MATCH (account created by `kestrelridge-main.testnet` with 3 NEAR, transaction `Cza55oWj...gcDwKF`; deploy with init `new(owner kestrelridge-owner.testnet, moderators [kestrelridge-owner.testnet])`, transaction `AMfe5SLG...nLCWCg`).

- cargo-near: 0.22.0.
- The repository toolchain is Rust 1.99.1 with `wasm32-unknown-unknown`.
- The crate manifest names the reproducible build image and build command.
- Reproducing the wasm needs Docker and the cargo-near build image.
- Reports wasm: 202972 bytes; sha256 `ba2551dc...7f8b7705` (full value in `res/reports.wasm.sha256`); code_hash `DXdqac6HBiXtZVCrJjdGjDEFfzcFF7EEDza8h8rU5UCC`; version 0.1.2. Two independent clones at the source commit built byte identical wasm on 7 Nov (`fixtures/runs/reproducible-reports-0.1.2-20261107-clones.txt`; the build output of each clone is in `fixtures/runs/reproducible-reports-0.1.2-20261107-clone1.txt` and `fixtures/runs/reproducible-reports-0.1.2-20261107-clone2.txt`). The layout preserving round trip ran on this exact artifact (`fixtures/runs/cargo-reports-0.1.2-candidate-20261107.txt`).
- Previous reports wasm: 198593 bytes; sha256 `e28ac3fc...b4737134` (full value in `res/previous/reports.wasm.sha256`); code_hash `GFKs9k5P6raHNxN6suXJNJByjsYdXAZdGdJE5a5duZes`; version 0.1.1, source commit 937615878543e9f0d45eeb5002b65f88e3fc3757 (two clone record `fixtures/runs/reproducible-reports-0.1.1-20261107-clones.txt`).

## Verification recipe

Check out the source commit recorded above, then build from the repository root:

```sh
unset CARGO_TARGET_DIR
(cd contracts/reports && cargo near build reproducible-wasm)
sha256sum contracts/reports/target/near/reports.wasm
```

The build output is written inside the crate, so `CARGO_TARGET_DIR` must be unset for the reproducible build command.
