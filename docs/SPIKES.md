# Spikes

Measured facts and spike results for the build. Every entry names its date, the command and what it printed.

## Environment on 29 Oct (read only probes)

- WSL distro `Ubuntu`: `rustc 1.99.1 (58cf8f9c3 2026-10-13)`; `rustup target list --installed` includes `wasm32-unknown-unknown`; `cargo near --version` prints `cargo-near-near 0.22.0`; `near --version` prints `near-cli-rs 0.30.1`; `python3 --version` prints `Python 3.12.3`.
- Not installed in WSL: `docker` (command not found); before the venv below, the Python modules `nacl` (PyNaCl) and `py_near` (py-near) both raised `ModuleNotFoundError`.
- Windows: `python --version` prints `Python 3.12.10`.
- crates.io on 29 Oct: near-sdk 5.30.0 and near-workspaces 0.24.0 are newer than the pins 5.29.1 and 0.23.0. The pins stay unless S1 passes on both.

## WSL environment variables

No shell startup file is edited. Each command sets its variables inline:

- `CARGO_TARGET_DIR=$HOME/target/shieldbot-near` on every cargo command (the directory exists). Nothing builds into the repository tree.
- `NEAR_SANDBOX_BIN_PATH`: see S1.

## S1: sandbox under WSL

Pending (29 Oct).

## S4: reproducible build

Pending (29 Oct).

## S7: py-near install and signing with a function call key

- Install, 29 Oct: the venv `~/.venvs/near` runs Python 3.11.16 and was made with uv, because the distro's `python3` has no working venv module. Installed: pynacl 1.6.2, py-near 1.2.22, ruff 0.17.3. `~/.venvs/near/bin/python -c "import nacl.signing, py_near; print('ok')"` prints `ok`.
- py-near on Python 3.12 stays UNVERIFIED: the venv is 3.11, which meets the 3.11 or later floor.
- Signing a testnet transaction with a function call key: not run. Due 1 Nov, 30 minute box. Fallback: a near-cli-rs subprocess from `gate/agent.py`.

## S8: NearBlocks testnet keys endpoint and account age

Not run. Due 2 Nov: the `/keys` fixture for an account created that day decides whether account age stays in the rules.
