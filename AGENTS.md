# Siyab

Siyab is security intelligence for the NEAR ecosystem: sourced labels, incident records and checks for accounts, tokens and proposed transactions. Every label has a source. Every verdict has evidence. Unknown never passes. Agent payments are the enforced case: a verdict service judges the receiver, token, amount and memo, a guard contract on testnet pays only on a signed ALLOW bound to that exact transfer, and a registry on testnet records verdicts with their evidence hashes (`docs/TESTNET.md` lists them). Unsigned checks are advice only. `README.md` has the full description and `docs/COVERAGE.md` lists the coverage and gaps.

## Install

- Python 3.11 or later. The offline path uses the standard library only.
- Contract work: the toolchain pinned in `rust-toolchain.toml` (1.99.1 with the `wasm32-unknown-unknown` target), cargo-near 0.22.0, and Docker for the reproducible wasm build.
- The live path (network): a venv at `~/.venvs/near` with PyNaCl and py-near.

## Test

Run from the repository root. Set `CARGO_TARGET_DIR` to a directory outside the repository on every cargo command (for example `$HOME/target/shieldbot-near`), except `cargo near build reproducible-wasm`, which needs it unset: its container writes `target/near` inside the crate (`res/BUILD-gate.md`).

```
python -m unittest discover -s tests -v                       # offline
python -m gate demo --offline                                 # offline
python scripts/check_hygiene.py --all                         # offline
python scripts/counts.py --readme README.md                   # offline; the README must not change
cd contracts/<crate> && cargo test --lib                      # network once (crates.io)
cd contracts/<crate> && cargo test --test sandbox             # network, Linux or macOS (sandbox download)
cd contracts/<build-crate> && cargo near build reproducible-wasm # network, Docker
```

`<crate>` is `gate`, `registry`, `reports` or `test-token` for `cargo test`; sandbox tests are in `gate`, `registry` and `reports`, while `test-token` has no sandbox test. `<build-crate>` is `gate`, `registry` or `reports`; each has a reproducible build record in `res/BUILD-<crate>.md`, while `test-token` has no build record. The PyNaCl and py-near cross checks run with `~/.venvs/near/bin/python` in place of `python` and skip without it: the py-near checks print a `skipped:` line, the PyNaCl checks show their reason with `-v`.

## Run

- `python -m gate demo --offline`: four verdicts over recorded testnet responses; the ALLOW is signed with the TEST key.
- `python -m gate verdict --kind ft --token <token> --receiver <account> --amount <base units> --gate <gate>`: one verdict; `--live` signs with the key `VERIFIER_KEY_FILE` names. `python -m gate verdict --live` needs `GATE_FIXTURES_MODE=off` for every client and reads the policy from the Gate.
- `python -m gate verify <record.json> <evidence.json>`, `python -m gate incident rhea`, `python -m intel counts`: offline reads of committed files.
- `python -m mcp.server`: the stdio MCP server (`mcp/README.md`).
- `python -m intel serve --host 127.0.0.1 --port 8792 --site demo/site --live --state-dir /var/lib/shieldbot-near/siyab --workspace-origin https://siyab.xyz`: public account reads and authenticated team workspaces with external SQLite state.
- `python -m intel monitor --state-dir /var/lib/shieldbot-near/siyab --once`: one retained-feed indexing pass for team inboxes; omit `--once` for the worker.
- `python -m intel history-follow --state-dir /var/lib/shieldbot-near/siyab --once`: one bounded ordinary-transfer watch polling pass; omit `--once` for the worker.
- `python -m intel investigation-follow --state-dir /var/lib/shieldbot-near/siyab --once`: advance one bounded historical investigation page; omit `--once` for the worker.
- `python -m intel delivery-follow --state-dir /var/lib/shieldbot-near/siyab --once`: one confirmed Telegram delivery pass; requires the external bot token file for network access.
- `python -m intel workspace-backup --state-dir /var/lib/shieldbot-near/siyab --out <new-external-backup-path>`: SQLite workspace backup, refusing an existing destination or a path inside the repository. This does not back up the separate history and monitor databases. See `docs/WORKSPACES.md` for configuration, permissions, restore procedures and source bounds.
- Network: `python -m gate pay`, `python -m gate attack <name>`, `python -m gate scan <account>` with `GATE_FIXTURES_MODE=off`, and the testnet scripts in `scripts/` (`scripts/README.md`; `DRY_RUN=1` prints the command lines only).

## Layout

```
contracts/gate/         guard contract that holds the agent's funds and pays only on a signed ALLOW
contracts/registry/     registry contract that records verdicts with their evidence hashes
contracts/reports/      community reports contract
contracts/test-token/   NEP-141 token for the sandbox and testnet runs
gate/                   canonical message, Ed25519, evidence, RPC, rules, verdict service, EVM leg, agent, scan, CLI
intel/                  labels, incidents, feed, account reads, bounded history/graphs, renderer, team workspace API
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
- `NEAR_MAINNET_RPC_URL`: mainnet JSON-RPC endpoint override for the live account check and other configurable mainnet reads. The MCP account check uses fixed `rpc.MAINNET_RPC_URL`, `rpc.NEARBLOCKS_URL` and `rpc.ARCHIVAL_RPC_URL` endpoints, ignoring the corresponding endpoint overrides. The token check in both MCP and the live API always uses fixed `rpc.MAINNET_RPC_URL` and ignores `NEAR_MAINNET_RPC_URL`.
- `NEAR_ARCHIVAL_RPC_URL`: archival JSON-RPC endpoint override.
- `NEARBLOCKS_API_URL`: NearBlocks endpoint override.
- `SHIELDBOT_API_URL`: optional ShieldBot API base URL for firewall and memo checks.
- `GATE_ACCOUNT_ID`: Gate account used by the MCP server, the testnet scripts and `python -m gate pay` and `attack`; the MCP server defaults to `gate.near`, and `pay` and `attack` run only for an id ending in `.testnet`, which selects the testnet endpoints.
- `GATE_POLICY`: payment policy JSON used by the MCP server.
- `VERIFIER_KEY_FILE`: path to the live verifier key file, kept outside the repository.
- `AGENT_KEY_FILE`: path to the agent function call key file, kept outside the repository.
- `GATE_FIXTURES_MODE`: fixture behavior, `replay` by default, `record` or `off`. MCP account and token checks inherit it; both live check routes always use `off`.
- `SIYAB_CHECKS`: set to `1` to enable the three unsigned MCP checks and the live account and token check routes; disabled by default. MCP tools remain listed but refuse calls when disabled; the live routes return 404. `/live/health` reports the setting as `checks`.
- `GATE_ALLOW_PURE_PYTHON_SIGNER`: set to `1` to enable the pure Python live signer.
- `GATE_LIVE_SIGNING`: set to `1` to enable MCP live signing when all required conditions hold.
- `GATE_KEEP_EVIDENCE`: set to `1` to retain evidence directories after process exit.
- `AGENT_PUB`: agent public key used by the MCP live verdict and testnet registration scripts.
- `FEED_STATE_DIR`: feed state directory; defaults to `intel/feed`.
- `FASTNEAR_API_KEY_FILE`: path to an external provider key file for account, portfolio and scanner reads; sent only with a provider budget database. The feed follower never reads or sends it.
- `SIYAB_PROVIDER_BUDGET_DB`: external provider budget database path; otherwise service entry points use their state directory.
- `SIYAB_VERDICT_NEARBLOCKS`: set to `1` to enable the hosted verdict and live account check account-age fallback; disabled by default. The live account check honors `NEAR_ARCHIVAL_RPC_URL` and uses `NEARBLOCKS_API_URL` only when this fallback is enabled; otherwise it does not read NearBlocks.
- `SIYAB_TELEGRAM_TOKEN_FILE`: path to the dedicated Siyab Telegram bot token file outside the repository; missing credentials leave delivery unconfigured.
- `SIYAB_TELEGRAM_CHECKS`: set to `1`, with `SIYAB_CHECKS=1`, to have the delivery worker answer account, token and transaction checks in private Telegram chats; off by default.

- `SIYAB_ASK`: Set to 1 to enable Ask Siyab; off by default.
- `ANTHROPIC_API_KEY_FILE`: Path outside the repository, never a key value; read at call time.
- `SIYAB_ASK_MODEL`: Ask model id from the priced model table.
- `SIYAB_ASK_EFFORT`: Ask effort: low by default, medium or high.
- `SIYAB_ASK_DAILY_CAP_MICRO`: Global Ask micro-dollar cap per UTC day: 0 to 3000000, default 3000000.
- `SIYAB_ASK_USER_TOKENS`: Ask billed tokens per user per UTC day: 0 to 1000000, default 100000.
- `SIYAB_ASK_TEAM_TOKENS`: Ask billed tokens per team per UTC day: 0 to 3000000, default 300000.
- `SIYAB_ASK_CACHE_ROWS`: Ask answer cache rows: 100 to 100000, default 10000.
- `SIYAB_ASK_CACHE_BYTES`: Ask answer cache bytes: 1048576 to 268435456, default 33554432.

Testnet script variables:

- `NETWORK`: network selector, defaults to `testnet`; scripts refuse other values.
- `DRY_RUN`: set to `1` to print planned commands without reading keys or sending transactions.
- `OWNER_ACCOUNT_ID`: account authorized to configure the Gate and registry.
- `VENDOR_ACCOUNT_ID`: account used for the vendor payment path.
- `AGED_PAYEE_ACCOUNT_ID`: account used for the aged payee path.
- `COUNTERPARTY_ACCOUNT_ID`: our demo counterparty account, used to fund a lookalike receiver.
- `NEAR_KEYS_DIR`: directory for external account key files.
- `COLD_ACCOUNT_ID`: account `kill_switch.sh` moves the Gate's token balance to; must be storage registered on the token.
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
- Keep browser storage as the default save destination. Authentication must not upload local investigations or notes; shared imports require explicit confirmation. Public account archives must not contain private workspace data.
- Keep workspace, history and monitor state outside the source tree and generated site. Document provider bounds, contract-reported metadata and independent worker health; do not infer complete history, external delivery or a favorable verdict from missing data.
