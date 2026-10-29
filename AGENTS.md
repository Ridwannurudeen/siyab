# ShieldBot for NEAR

Who your agent pays gets a verdict with evidence. Unknown never passes.

This file is a stub. The full version lands with the README on 10 Nov.

## Install

- Python 3.11 or later. The offline path uses the standard library only.
- Rust: the toolchain pinned in `rust-toolchain.toml` (1.99.1 with the `wasm32-unknown-unknown` target), and cargo-near 0.22.0.
- Docker only for the reproducible wasm build (`cargo near build reproducible-wasm`).
- The live path (network): a venv at `~/.venvs/near` with PyNaCl and py-near.

## Test

Run from the repository root. Set `CARGO_TARGET_DIR` to a directory outside the repository on every cargo command (for example `$HOME/target/shieldbot-near`); nothing builds into the tree.

```
python3 -m unittest discover -s tests -v                                              # offline
cd contracts/<crate> && CARGO_TARGET_DIR=$HOME/target/shieldbot-near cargo test --lib   # network once (crates.io)
cd contracts/<crate> && CARGO_TARGET_DIR=$HOME/target/shieldbot-near cargo test --test sandbox   # network, Linux or macOS (sandbox download)
```

The live path cross checks use `~/.venvs/near/bin/python` in place of `python3`.

## Layout (planned; directories land as the build proceeds)

```
contracts/gate/         guard contract that holds the agent's funds and pays only on a signed ALLOW
contracts/registry/     registry contract that records each verdict with its evidence hash
contracts/test-token/   NEP-141 token for the sandbox and testnet runs
gate/                   canonical message, Ed25519, base58, evidence, RPC, rules, verdict service, signers, CLI
intel/                  label store, seeds, incidents
mcp/                    stdio MCP server
fixtures/               recorded responses, replayed offline
tests/                  Python tests
scripts/                hygiene check, testnet scripts, counts
res/                    built wasm files and their sha256
docs/                   spikes, records, log
```

## Environment variables

- `NEAR_RPC_URL`: testnet JSON-RPC endpoint; default `https://test.rpc.fastnear.com`
- `NEAR_MAINNET_RPC_URL`: mainnet JSON-RPC endpoint for read only views; default `https://free.rpc.fastnear.com`
- `NEAR_ARCHIVAL_RPC_URL`: archival JSON-RPC endpoint that dates a receiver when NearBlocks gives no creation time; default `https://archival-rpc.testnet.fastnear.com` for a testnet Gate, else `https://archival-rpc.mainnet.fastnear.com`
- `GATE_ACCOUNT_ID`, `REGISTRY_ACCOUNT_ID`, `OWNER_ACCOUNT_ID`: the guard, the registry and the owner accounts
- `VERIFIER_KEY_FILE`, `RECORDER_KEY_FILE`, `AGENT_KEY_FILE`: key file paths, kept outside the repository
- `SHIELDBOT_API_URL` (empty means off), `NEARBLOCKS_API_URL`, `GOPLUS_API_URL`, `ONECLICK_API_URL`: data source base URLs
- `GATE_FIXTURES_MODE`: `replay` (default), `record` or `off`
- `GATE_ALLOW_PURE_PYTHON_SIGNER`: unset by default; `1` lets the pure Python signer sign with a live key

## Working agreements

- No secrets in the repository: no private key in any form, no `.env`. Key files live outside the tree and are named by environment variables.
- Offline tests stay green on every commit.
- No attribution in commits and no co-author trailers.
- No text about how this repository is evaluated; no mention of other projects.
- The hooks in `.githooks/` run `scripts/check_hygiene.py` on staged files and check commit messages. Never bypass them.
