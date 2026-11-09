reproducible build

## Source and build environment

Source commit: c270d76e21f11310b6d64e78c6af0fb98710491f

Deployed: 9 Nov 2026, transaction `zrSXNV7k...spCxuF`, version 0.1.3, code_hash `En4wm3kmoZRTP8jZQCHhuDwwyXXEbQcNqfJyyiQYve4k` MATCH.

Previous deploy and rollback target, version 0.1.2 from source commit 5abc782b2126ac4cc91001b20ecb89e78fca11f8; code_hash `DXdqac6HBiXtZVCrJjdGjDEFfzcFF7EEDza8h8rU5UCC`; artifact `res/previous/reports.wasm`.

Earlier deploy, version 0.1.1 from source commit 937615878543e9f0d45eeb5002b65f88e3fc3757; code_hash `GFKs9k5P6raHNxN6suXJNJByjsYdXAZdGdJE5a5duZes`.

- cargo-near: 0.22.0.
- The repository toolchain is Rust 1.99.1 with `wasm32-unknown-unknown`.
- The crate manifest names the reproducible build image and build command.
- Reproducing the wasm needs Docker and the cargo-near build image.
- Reports wasm: 213832 bytes; sha256 `ccb386ad...e043fde5` (full value in `res/reports.wasm.sha256`); code_hash `En4wm3kmoZRTP8jZQCHhuDwwyXXEbQcNqfJyyiQYve4k`; version 0.1.3. Two independent Linux clones at the source commit built byte identical wasm on 8 Nov (`fixtures/runs/reproducible-reports-0.1.3-20261108-clones.txt`; each build output is in `fixtures/runs/reproducible-reports-0.1.3-20261108-clone1.txt` and `fixtures/runs/reproducible-reports-0.1.3-20261108-clone2.txt`). After artifact rotation, the exact artifact passed all 3 sandbox tests on checkout commit `94076a8e`, including the previous artifact checksum assertion (`fixtures/runs/cargo-reports-0.1.3-candidate-20261108.txt`); the library tests passed 93 tests (`fixtures/runs/cargo-reports-0.1.3-lib-20261108.txt`).
- Previous reports wasm: 202972 bytes; sha256 `ba2551dc...7f8b7705` (full value in `res/previous/reports.wasm.sha256`); code_hash `DXdqac6HBiXtZVCrJjdGjDEFfzcFF7EEDza8h8rU5UCC`; version 0.1.2.
- Earlier reports deploy, version 0.1.1: 198593 bytes; sha256 `e28ac3fc...b4737134`; code_hash `GFKs9k5P6raHNxN6suXJNJByjsYdXAZdGdJE5a5duZes`; source commit 937615878543e9f0d45eeb5002b65f88e3fc3757.

## Verification recipe

Check out the source commit recorded above, then build from the repository root:

```sh
unset CARGO_TARGET_DIR
(cd contracts/reports && cargo near build reproducible-wasm)
sha256sum contracts/reports/target/near/reports.wasm
```

The build output is written inside the crate, so `CARGO_TARGET_DIR` must be unset for the reproducible build command.
