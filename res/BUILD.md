non reproducible build; the hash is not source verifiable

## Build record

The reproducible build ladder reached the local Docker step. The configured container compiled Gate and printed a wasm digest, but cargo-near 0.22.0 could not materialize the artifact at the required external target path. An explicit `--out-dir` did not change that error. The next rung is the CI `reproducible` job, which was not run for this local commit. This record therefore uses the non reproducible fallback.

The final artifacts were built in the WSL clone at `/home/gudman/n7-build`, commit `7bfd1de`, with `CARGO_TARGET_DIR=$HOME/target/shieldbot-near`.

- Toolchain: Rust 1.99.1 from `rust-toolchain.toml`.
- cargo-near: 0.22.0.
- Command for both crates: `cargo near build non-reproducible-wasm`.
- Reproducible image attempted: `sourcescan/cargo-near:0.21.1-rust-1.96.0`, digest `sha256:ccb2...ee17`.
- Compiler used by the attempted container build: Rust 1.96.0. It did not produce the committed artifacts.
- Compiler used for the committed artifacts: Rust 1.99.1.
- Gate: 238633 bytes; `python -m gate codehash res/gate.wasm` printed sha256 `d9ba...e9a5` and code_hash `FevViVVs...443rU4`.
- Registry: 156443 bytes; `python -m gate codehash res/registry.wasm` printed sha256 `6f35...e8653` and code_hash `8V7syXZ...snGTC`.

reproducing the hash from source needs Docker; checking it against testnet needs network

Testnet code_hash after redeploy: pending

The orchestrator can replace the pending line after `scripts/redeploy.sh` reads both deployed code hashes. No testnet comparison was made during this build.
