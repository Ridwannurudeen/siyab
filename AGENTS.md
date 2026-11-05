# ShieldBot for NEAR

Every label has a source. Every verdict has evidence. Unknown never passes. A verdict service judges the receiver, the token, the amount and the memo of an agent payment on NEAR, a guard contract on testnet pays only on a signed ALLOW bound to that exact transfer, and a registry on testnet records verdicts with their evidence hashes (`docs/TESTNET.md` lists them). `README.md` has the full description.

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
contracts/registry/     registry contract that records verdicts with their evidence hashes
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

`env.example` lists every environment variable read by the Python code and testnet scripts, once each, with its purpose.

Python variables:

- `NEAR_RPC_URL`: testnet JSON-RPC endpoint override.
- `NEAR_MAINNET_RPC_URL`: mainnet JSON-RPC endpoint override.
- `NEAR_ARCHIVAL_RPC_URL`: archival JSON-RPC endpoint override.
- `NEARBLOCKS_API_URL`: NearBlocks endpoint override.
- `SHIELDBOT_API_URL`: optional ShieldBot API base URL for firewall and memo checks.
- `GATE_ACCOUNT_ID`: Gate account used by the MCP server, the testnet scripts and `python -m gate pay` and `attack`; the MCP server defaults to `gate.near`, and `pay` and `attack` run only for an id ending in `.testnet`, which selects the testnet endpoints.
- `GATE_POLICY`: payment policy JSON used by the MCP server.
- `VERIFIER_KEY_FILE`: path to the live verifier key file, kept outside the repository.
- `AGENT_KEY_FILE`: path to the agent function call key file, kept outside the repository.
- `GATE_FIXTURES_MODE`: fixture behavior, `replay` by default, `record` or `off`.
- `GATE_ALLOW_PURE_PYTHON_SIGNER`: set to `1` to enable the pure Python live signer.
- `GATE_LIVE_SIGNING`: set to `1` to enable MCP live signing when all required conditions hold.
- `GATE_KEEP_EVIDENCE`: set to `1` to retain evidence directories after process exit.
- `AGENT_PUB`: agent public key used by the MCP live verdict and testnet registration scripts.
- `FEED_STATE_DIR`: feed state directory; defaults to `intel/feed`.
- `FASTNEAR_API_KEY_FILE`: path to a file holding the FastNEAR key; the follower reads it for authenticated requests.

Testnet script variables:

- `NETWORK`: network selector, defaults to `testnet`; scripts refuse other values.
- `DRY_RUN`: set to `1` to print planned commands without reading keys or sending transactions.
- `OWNER_ACCOUNT_ID`: account authorized to configure the Gate and registry.
- `VENDOR_ACCOUNT_ID`: account used for the vendor payment path.
- `AGED_PAYEE_ACCOUNT_ID`: account used for the aged payee path.
- `ATTACKER_ACCOUNT_ID`: account used to fund a lookalike receiver.
- `NEAR_KEYS_DIR`: directory for external account key files.
- `COLD_ACCOUNT_ID`: account kill_switch.sh moves the Gate's token balance to; must be storage registered on the token.
- `VERIFIER_PUB`: public verifier key passed to the Gate initializer.
- `AGENT_LABEL`: label stored with the agent.
- `TOKEN_SYMBOL`: test token symbol, defaults to `DMT`.
- `TOKEN_DECIMALS`: test token decimals, defaults to `6`.
- `TOKEN_SUPPLY`: initial test token supply in base units.
- `GATE_WASM`: Gate wasm path, with a repository default and external target fallback.
- `REGISTRY_WASM`: registry wasm path.
- `TOKEN_WASM`: test token wasm path.
- `CARGO_TARGET_DIR`: build directory used by testnet setup, defaults outside the repository.
- `MAX_TTL_MS`: Gate verdict lifetime in milliseconds, defaults to `300000`.
- `CAP_PER_CALL`: agent token cap per call, in whole tokens.
- `CAP_WINDOW`: agent token cap per window, in whole tokens.
- `CAP_WINDOW_MS`: agent cap window length in milliseconds.
- `UNKNOWN_PER_CALL`: unknown payee token cap per call, in whole tokens.
- `UNKNOWN_WINDOW`: unknown payee token cap per window, in whole tokens.
- `AGENT_ALLOWANCE_NEAR`: function call key allowance in NEAR, defaults to `1`.
- `GATE_STATE_FROZEN`: set to `1` to allow redeployment after 4 Nov.

## Working agreements

- No secrets in the repository: no private key in any form, no `.env`. Key files live outside the tree and are named by environment variables.
- Offline tests stay green on every commit.
- No attribution in commits and no co-author trailers.
- No text about how this repository is evaluated; no mention of other projects.
- The hooks in `.githooks/` run `scripts/check_hygiene.py` on staged files and check commit messages. Never bypass them.
- `docs/LOG.md` gets one line a day on what landed.
