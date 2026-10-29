# Spikes

Measured facts and spike results for the build. Every entry names its date, the command and what it printed. UNVERIFIED marks anything read from source or docs but not run.

## Environment on 29 Oct (read only probes)

- WSL distro `Ubuntu` (Ubuntu 24.04.1 LTS): `rustc 1.99.1 (58cf8f9c3 2026-10-13)`; `rustup target list --installed` includes `wasm32-unknown-unknown`; `cargo near --version` prints `cargo-near-near 0.22.0`; `near --version` prints `near-cli-rs 0.30.1`; `python3 --version` prints `Python 3.12.3`.
- Not installed in WSL: `docker` (command not found); before the venv below, the Python modules `nacl` (PyNaCl) and `py_near` (py-near) both raised `ModuleNotFoundError`.
- No C toolchain in WSL: `which cc gcc clang` finds nothing; `dpkg -l` finds no `build-essential`, `gcc`, `libc6-dev`, `pkg-config` or `libssl-dev`; `/usr/lib/x86_64-linux-gnu/crt1.o` is absent. `sudo -n true` prints `sudo: a password is required`. This blocks S1 (below).
- Windows: `python --version` prints `Python 3.12.10`.
- crates.io on 29 Oct: near-sdk 5.30.0 and near-workspaces 0.24.0 are newer than the pins 5.29.1 and 0.23.0. The pins stay unless S1 passes on both.
- Line endings: Git for Windows sets `core.autocrlf=true` in its system config, so a worktree that Windows git checks out has CRLF line endings. The hooks still run from Git Bash (measured: a CRLF `commit-msg` hook with an `if` block ran and returned its exit code). WSL bash does not run a CRLF script (measured: `set: -: invalid option`, then `syntax error: unexpected end of file`, exit 2). Python reads CRLF files without trouble.

## WSL environment variables

No shell startup file is edited. Each command sets its variables inline:

- `CARGO_TARGET_DIR=$HOME/target/shieldbot-near` on every cargo command (the directory exists). Nothing builds into the repository tree.
- `NEAR_SANDBOX_BIN_PATH`: not needed so far. near-sandbox 0.3.16 downloads the binary into the cargo target directory, not `/tmp` (see S1); pass it inline only to pin a copied binary.

## S1: sandbox under WSL

Result, 29 Oct: BLOCKED before any wasm was built. Not a sandbox failure: WSL has no C linker, so no host build script links.

What was run, in `~/spikes/s1` (WSL native, outside any repository):

1. `cargo near new s1-template` (network): wrote the project, then its last step `git commit -m init` failed with `fatal: empty ident name ... not allowed` because WSL git has no committer identity (left unset on purpose). The project files are complete.
2. What cargo-near 0.22.0 generates: edition 2024, `rust-version = "1.93"`, its own `rust-toolchain.toml` with `channel = "1.93.0"`; `near-sdk = "5.29"` (lock 5.29.1); dev dependencies `near-sandbox = "0.3.12"` (lock 0.3.16), `near-api = "0.8"` (lock 0.8.7), `cargo-near-build = "0.11.0"` (lock 0.11.5), tokio, testresult. Its test starts the sandbox with near-sandbox and calls the contract with near-api; it does not use near-workspaces. Its `[package.metadata.near.reproducible_build]` names the image `sourcescan/cargo-near:0.21.1-rust-1.96.0` (digest `sha256:ccb2...ee17`).
3. Edits for the spike: `near-sdk = "=5.29.1"` in both tables, `near-workspaces = "=0.23.0"` as a dev dependency, the toolchain channel set to `1.99.1` with rustfmt, clippy and the wasm target, and one added test `tests/workspaces_s1.rs` that reads the built wasm, starts `near_workspaces::sandbox()`, prints `worker.root_account()?.id()` and `worker.status()`, deploys with `dev_deploy`, calls `init` and `bid`, and views `get_highest_bid`.
4. `cargo --version` in the spike: rustup auto installed `1.99.1-x86_64-unknown-linux-gnu` with rustfmt, clippy and `wasm32-unknown-unknown` in 36 s (network once). The repository's `rust-toolchain.toml` resolves to it: `rustup show active-toolchain` prints `1.99.1-x86_64-unknown-linux-gnu (overridden by '.../shieldbot-near/rust-toolchain.toml')`.
5. `cargo fetch` (network, 27 s): the lock resolved near-sdk 5.29.1, near-workspaces 0.23.0, near-sandbox 0.3.16, near-api 0.8.7, cargo-near-build 0.11.5.
6. `cargo near build non-reproducible-wasm` (2.3 s): cargo-near printed `contract's near-sdk targets protocol version 84; rustc 1.99.1 accepted (bulk-memory opcodes supported by nearcore VM)`, then the ABI step failed with `error: linker 'cc' not found` in the build scripts of proc-macro2, quote, serde and others. `target/near/` stayed empty.
7. `cargo test -- --nocapture --test-threads=1` (1.2 s): the same `linker 'cc' not found`. Nothing reached the sandbox.

Findings that hold already:

- Pin with `=`: `near-sdk = "5.29.1"` is a caret requirement and resolves to 5.30.0 on a fresh lock; the crates need `near-sdk = "=5.29.1"` and `near-workspaces = "=0.23.0"` (or a committed lock that holds those versions).
- The C toolchain is needed by more than the build scripts: the spike's lock holds openssl-sys (through native-tls), secp256k1-sys, ring, aws-lc-sys and zstd-sys, which compile C code.
- Every Rust build in WSL is blocked until the install below, including `cargo test --lib` in `contracts/gate` (host build scripts link with `cc`).

To unblock (the owner, because sudo needs the password): `sudo apt-get update && sudo apt-get install -y build-essential pkg-config libssl-dev`. Whether aws-lc-sys also needs cmake here is UNVERIFIED.

Re-run once unblocked (WSL, each line timed; the second `cargo test` gives the warm time):

```
. ~/.cargo/env && cd ~/spikes/s1/s1-template
time cargo near build non-reproducible-wasm
time cargo test -- --nocapture --test-threads=1
time cargo test -- --nocapture --test-threads=1
ls -l target/near/s1_template.wasm
find ~ -name "near-sandbox*" -type f 2>/dev/null
```

Then, only if that passes, set `near-sdk = "=5.30.0"` and `near-workspaces = "=0.24.0"`, run `cargo update` and the same three commands; the pins change only if that full run passes.

Not measured yet: pass or fail, the first and second run wall times, the sandbox binary path and version, the root account id, the protocol version, any clock step failure, the 5.30.0 and 0.24.0 repeat, and the release wasm size (`cargo near build non-reproducible-wasm` builds the release profile unless `--no-release` is given; the template's profile is `opt-level = "z"`, `lto = true`, `codegen-units = 1`, `panic = "abort"`, `overflow-checks = true`).

From the crate sources (UNVERIFIED until the run prints them):

- Sandbox binary: near-sandbox 0.3.16 pins nearcore sandbox `2.13.4` (`DEFAULT_NEAR_SANDBOX_VERSION`) and downloads `https://s3-us-west-1.amazonaws.com/build.nearprotocol.com/nearcore/<platform>/2.13.4/near-sandbox.tar.gz` into `$OUT_DIR/.near/near-sandbox-2.13.4/` inside the cargo target directory (`$HOME/.near` only with the `global_install` feature). `NEAR_SANDBOX_BIN_PATH` overrides the path. The binary is therefore not under `/tmp`; a `cargo clean` removes it. Each sandbox run gets a fresh home directory from `tempfile::tempdir()`, which is under `/tmp`.
- Root account id: near-sandbox 0.3.16 adds a genesis account `sandbox` (the template's accounts are `alice.sandbox`, `bob.sandbox` and so on), and near-workspaces 0.23.0 adds `registrar`. near-workspaces takes its root signer from `validator_key.json`, which `near-sandbox --home <dir> init --fast` writes. Whether `worker.root_account()` returns `test.near` or `sandbox` is open until the run prints it; the sandbox vectors wait for the measured id.

## S4: reproducible build

Result, 29 Oct: the Docker half cannot run on this machine; the hash half is closed.

- `docker --version` in WSL: `bash: line 1: docker: command not found` (exit 127).
- `cargo near build reproducible-wasm --help` (cargo-near 0.22.0): "Requires `[reproducible_build]` section in Cargo.toml, and all changes committed to git (recommended for the production release)". Options: `--no-locked` (makes the build not reproducible), `--profile`, `--out-dir`, `--manifest-path`, `--color`, `--variant` (a named override under `[package.metadata.near.reproducible_build.variant.<name>]`). The contract is built inside a Docker container; the image, its digest, the passed environment variables and the build command come from `[package.metadata.near.reproducible_build]`; `--locked` is on by default, so the crate needs a committed `Cargo.lock` that is current.
- What it needs: that metadata section in each crate's `Cargo.toml`, a Docker daemon that can pull the image, a clean committed git tree, and a current `Cargo.lock`.
- Open for the build day: the template's image carries Rust 1.96.0, while `rust-toolchain.toml` pins 1.99.1. Whether the container honours `rust-toolchain.toml` is UNVERIFIED; if it does not, the reproducible wasm is compiled by 1.96.0.
- Hash half: closed by AUDIT-v1-A A15: the testnet `code_hash` equals base58 of sha256 of the wasm (live MATCH on 25 Oct).

Fallback ladder (decision (s)):

1. The owner installs Docker Desktop with WSL integration before 7 Nov, and the orchestrator runs the reproducible build on 8 Nov.
2. Else the `reproducible` CI job on `ubuntu-latest` runs it after the private repository exists. UNVERIFIED that the runner can pull the cargo-near build image; test it the day the remote exists.
3. Else `res/*.wasm` is a non reproducible build and the README says "the hash is not source verifiable".

## near-cli-rs 0.30.1 command lines

Checked on 29 Oct with `--help` at every level; nothing was signed or sent. `<...>` marks a value to fill in.

Create an account through the faucet (the first form stores the key in the system keychain, the second writes a key file in the legacy keychain format of the old CLI):

```
near account create-account sponsor-by-faucet-service <name>.testnet autogenerate-new-keypair save-to-keychain network-config testnet create
near account create-account sponsor-by-faucet-service <name>.testnet autogenerate-new-keypair save-to-legacy-keychain network-config testnet create
```

Read only:

```
near account view-account-summary <name>.testnet network-config testnet now
near account list-keys <name>.testnet network-config testnet now
near contract call-function as-read-only <contract> <method> json-args '{}' network-config testnet now
```

A transaction call signed with a key file:

```
near contract call-function as-transaction <contract> <method> json-args '<json>' prepaid-gas '30 Tgas' attached-deposit '1 yoctoNEAR' sign-as <signer>.testnet network-config testnet sign-with-access-key-file <path/to/key.json> send
```

Deploy, with and without an init call:

```
near contract deploy <account>.testnet use-file <path/to/contract.wasm> with-init-call new json-args '<json>' prepaid-gas '100.0 Tgas' attached-deposit '0 NEAR' network-config testnet sign-with-access-key-file <path/to/key.json> send
near contract deploy <account>.testnet use-file <path/to/contract.wasm> without-init-call network-config testnet sign-with-access-key-file <path/to/key.json> send
```

Add a function call key, add a full access key, delete keys:

```
near account add-key <account>.testnet grant-function-call-access --allowance '1 NEAR' --contract-account-id <contract>.testnet --function-names request_transfer use-manually-provided-public-key ed25519:<base58> network-config testnet sign-with-access-key-file <path/to/key.json> send
near account add-key <account>.testnet grant-full-access use-manually-provided-public-key ed25519:<base58> network-config testnet sign-with-access-key-file <path/to/key.json> send
near account delete-keys <account>.testnet public-keys ed25519:<base58> network-config testnet sign-with-access-key-file <path/to/key.json> send
```

Export (prints the private key to the terminal; the owner runs it, the output never goes into chat or the repository):

```
near account export-account <account>.testnet using-private-key network-config testnet
```

Notes from the help output:

- Signing variants in 0.30.1, the same list after every `network-config testnet` of a transaction: `sign-with-keychain`, `sign-with-legacy-keychain`, `sign-with-ledger`, `sign-with-plaintext-private-key`, `sign-with-access-key-file`, `sign-with-seed-phrase`, `sign-with-mpc`, `sign-later`, `submit-as-dao-proposal`. After the signer: `send`, `save-to-file` or `display`.
- Scripts use `sign-with-access-key-file <path>`: `sign-with-plaintext-private-key` takes the key as a command line argument, which lands in the shell history and the process list. `sign-with-legacy-keychain` (no path; reads the legacy keychain) also exists.
- Key deletion is `delete-keys` (plural), with `public-keys` taking a comma separated list.
- The parser accepted `'100.0 Tgas'`, `'30 Tgas'`, `'0 NEAR'` and `'1 yoctoNEAR'` at `--help` time and rejected `100.0_Tgas` with `invalid value`.
- `create-account ... autogenerate-new-keypair` also takes `--signature-scheme` (`ed25519` default, `ml-dsa-65`).
- UNVERIFIED until first use: the JSON layout `sign-with-access-key-file` expects (expected: the legacy keychain file with the account id, the public key and the private key), whether `save-to-keychain` works in WSL without a desktop keyring, the directory the legacy keychain writes to, and whether `--function-names` takes a comma separated list.

## S7: py-near install and signing with a function call key

- Install, 29 Oct: the venv `~/.venvs/near` runs Python 3.11.16 and was made with uv, because the distro's `python3` has no working venv module. Installed: pynacl 1.6.2, py-near 1.2.22, ruff 0.17.3. `~/.venvs/near/bin/python -c "import nacl.signing, py_near; print('ok')"` prints `ok`.
- py-near on Python 3.12 stays UNVERIFIED: the venv is 3.11, which meets the 3.11 or later floor.
- Signing a testnet transaction with a function call key: not run. Due 1 Nov, 30 minute box. Fallback: a near-cli-rs subprocess from `gate/agent.py`, using the `sign-with-access-key-file` line above.

## S8: NearBlocks testnet keys endpoint and account age

Not run. Due 2 Nov: the `/keys` fixture for an account created that day decides whether account age stays in the rules.
