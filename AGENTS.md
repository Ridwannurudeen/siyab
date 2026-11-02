# ShieldBot for NEAR

Who your agent pays gets a verdict with evidence. Unknown never passes. A verdict service checks an agent payment on NEAR, a guard contract pays only on a signed ALLOW, and a registry records each verdict with its evidence hash. `README.md` has the full description.

## Install

- Python 3.11 or later. The offline path uses the standard library only.
- Contract work: the toolchain pinned in `rust-toolchain.toml` (1.99.1 with the `wasm32-unknown-unknown` target), cargo-near 0.22.0, and Docker for the reproducible wasm build.
- The live path (network): a venv at `~/.venvs/near` with PyNaCl and py-near.

## Test

Run from the repository root. Set `CARGO_TARGET_DIR` to a directory outside the repository on every cargo command (for example `$HOME/target/shieldbot-near`), except `cargo near build reproducible-wasm`, which needs it unset: its container writes `target/near` inside the crate (`res/BUILD.md`).

```
python -m unittest discover -s tests -v                       # offline
python -m gate demo --offline                                 # offline
python scripts/check_hygiene.py --all                         # offline
python scripts/counts.py --readme README.md                   # offline; the README must not change
cd contracts/<crate> && cargo test --lib                      # network once (crates.io)
cd contracts/<crate> && cargo test --test sandbox             # network, Linux or macOS (sandbox download)
cd contracts/<crate> && cargo near build reproducible-wasm    # network, Docker
```

`<crate>` is `gate`, `registry` or `test-token` (`test-token` has no sandbox test). Both contract crates carry the reproducible build metadata; `res/BUILD.md` has the source commit and the recipe. The PyNaCl and py-near cross checks run with `~/.venvs/near/bin/python` in place of `python` and skip without it: the py-near checks print a `skipped:` line, the PyNaCl checks show their reason with `-v`.

## Run

- `python -m gate demo --offline`: four verdicts over recorded testnet responses; the ALLOW is signed with the TEST key.
- `python -m gate verdict --kind ft --token <token> --receiver <account> --amount <base units> --gate <gate>`: one verdict; `--live` signs with the key `VERIFIER_KEY_FILE` names.
- `python -m gate verify <record.json> <evidence.json>`, `python -m gate incident rhea`, `python -m intel counts`: offline reads of committed files.
- `python -m mcp.server`: the stdio MCP server (`mcp/README.md`).
- Network: `python -m gate pay`, `python -m gate attack <name>`, `python -m gate scan <account>` with `GATE_FIXTURES_MODE=off`, and the testnet scripts in `scripts/` (`scripts/README.md`; `DRY_RUN=1` prints the command lines only).

## Layout

```
contracts/gate/         guard contract that holds the agent's funds and pays only on a signed ALLOW
contracts/registry/     registry contract that records each verdict with its evidence hash
contracts/test-token/   NEP-141 token for the sandbox and testnet runs
gate/                   canonical message, Ed25519, evidence, RPC, rules, verdict service, EVM leg, agent, scan, CLI
intel/                  label store, seeds, incidents, feed, flow trace, static page renderer, read only API
mcp/                    stdio MCP server
fixtures/               recorded responses, replayed offline
tests/                  Python tests
scripts/                hygiene check, counts, idea text (gen_idea.py), offline smoke (smoke.py), testnet scripts
demo/site/              static pages written by python -m intel render
docs/                   records, claims, attacks, evidence, testnet, demo shot list (DEMO.md), spikes, log
res/                    built wasm files, their sha256 and the build record
```

## Environment variables

`env.example` lists every variable with a comment. The code reads `NEAR_RPC_URL`, `NEAR_MAINNET_RPC_URL`, `NEAR_ARCHIVAL_RPC_URL`, `NEARBLOCKS_API_URL`, `SHIELDBOT_API_URL` (empty means off), `GATE_ACCOUNT_ID` (the MCP server defaults to `gate.near`), `GATE_POLICY` (the agent policy JSON the MCP server checks payments against), `VERIFIER_KEY_FILE` and `AGENT_KEY_FILE` (key file paths kept outside the repository), `GATE_FIXTURES_MODE` (`replay` by default, `record` or `off`), `GATE_ALLOW_PURE_PYTHON_SIGNER`, `GATE_LIVE_SIGNING` (`1` lets the MCP server sign with the live key), `GATE_KEEP_EVIDENCE` (`1` keeps the evidence directory `python -m gate demo` and the MCP server write at exit) and `AGENT_PUB` (the agent public key the MCP server binds a live verdict to). The testnet scripts read the variables in the `scripts/README.md` table.

## Working agreements

- No secrets in the repository: no private key in any form, no `.env`. Key files live outside the tree and are named by environment variables.
- Offline tests stay green on every commit.
- No attribution in commits and no co-author trailers.
- No text about how this repository is evaluated; no mention of other projects.
- The hooks in `.githooks/` run `scripts/check_hygiene.py` on staged files and check commit messages. Never bypass them.
- `docs/LOG.md` gets one line a day on what landed.
