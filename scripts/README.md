# Testnet scripts

The scripts use environment variables and the near-cli-rs command lines recorded in `docs/SPIKES.md`. Set `DRY_RUN=1` or pass `--dry-run` to print the planned command lines; neither mode reads key contents or submits a transaction. The environment example file at the repository root (RUN-N9) will show these values. No script contains or prints a key.

| Variable | Use |
|---|---|
| `NETWORK` | Testnet only, defaults to `testnet`. |
| `OWNER_ACCOUNT_ID` | Account authorized to configure the Gate and registry. |
| `GATE_ACCOUNT_ID` | Account that holds the Gate contract. |
| `VENDOR_ACCOUNT_ID` | Account used for the vendor payment path. |
| `AGED_PAYEE_ACCOUNT_ID` | Account used for the aged payee path. |
| `ATTACKER_ACCOUNT_ID` | Account that funds the lookalike receiver. |
| `NEAR_KEYS_DIR` | Directory for `<account>.json` key files; created subaccount files are set to mode 600 on a Linux filesystem, while chmod is a no op on `/mnt/c`. |
| `COLD_ACCOUNT_ID` | Account that `kill_switch.sh` moves the Gate's token balance to; it must be storage registered on the token. |
| `VERIFIER_PUB` | Public verifier key passed to the Gate initializer. |
| `AGENT_PUB` | Function call public key registered with the Gate. |
| `AGENT_LABEL` | Label stored with the agent. |
| `TOKEN_SYMBOL` | Token symbol, defaults to `DMT`. |
| `TOKEN_DECIMALS` | Token decimals, defaults to `6`. |
| `TOKEN_SUPPLY` | Initial token supply in base units. |
| `GATE_WASM` | Gate wasm path, defaults to `res/gate.wasm` and falls back to `$CARGO_TARGET_DIR/near/gate.wasm`. |
| `REGISTRY_WASM` | Registry wasm path. |
| `TOKEN_WASM` | Test token wasm path. |
| `MAX_TTL_MS` | Gate verdict lifetime in milliseconds, defaults to `300000`. |
| `CAP_PER_CALL` | Agent token cap per call, in whole tokens. |
| `CAP_WINDOW` | Agent token cap per window, in whole tokens. |
| `CAP_WINDOW_MS` | Agent cap window length in milliseconds. |
| `UNKNOWN_PER_CALL` | Unknown payee token cap per call, in whole tokens. |
| `UNKNOWN_WINDOW` | Unknown payee token cap per window, in whole tokens. |
| `AGENT_ALLOWANCE_NEAR` | Function call key allowance in NEAR, defaults to `1`. |
| `GATE_STATE_FROZEN` | Must equal `1` to redeploy after 4 Nov. |

`setup_testnet.sh` checks all wasm files and the Gate balance before creating any subaccount, then creates missing accounts, recovers generated keys, deploys missing contract state and registers the token storage accounts. The balance check subtracts 3 NEAR for each missing token or registry account, 1 NEAR for a missing recorder account, and 0.00387 NEAR for the three storage deposits (0.00129 each, the token's storage_balance_bounds minimum), leaving at least 10 NEAR.

`register_agent.sh` registers the agent and converts token caps to base units.

`set_allowlist.sh` replaces the agent allowlist with the supplied comma separated accounts.

`kill_switch.sh <agent_pub>` pauses the Gate, revokes the agent (removing its entry and function call key), reads the token balance, then withdraws the full token balance to `COLD_ACCOUNT_ID`. The cold account must already be storage registered on the token. If it is not, pause and revocation have already run and the script stops before withdrawing. The Gate's NEAR balance stays in place to stake its own state; the owner can withdraw NEAR by hand with `withdraw` and asset `"Near"` if needed. After a testnet drill, run `register_agent.sh` and `set_allowlist.sh` again: revocation removes the agent entry, and `register_agent` starts with an empty allowlist.

`set_denylist.sh <account[,account...]>` replaces the entire Gate denylist with the supplied comma separated accounts.

`redeploy.sh` checks the version before and after deploying a selected Gate or registry wasm.

`make_attack_receiver.sh` creates a funded receiver account and prints the creation time.

`record_verdicts.sh` records a batch from a JSON argument file and can record labels from a second file. The labels file can be a bare JSON list of label argument objects or an object with a `labels` list; the script validates the file before submitting either transaction.

Run a dry pass with `DRY_RUN=1 bash scripts/<script>.sh` and the required environment variables. `record_verdicts.sh` also needs its input JSON file to be readable during a dry pass. The first testnet run on 29 Oct confirmed the legacy key path (`~/.near-credentials/testnet/<account>.json`); created files are moved into `NEAR_KEYS_DIR` and set to mode 600 on a Linux filesystem, while chmod is a no op on `/mnt/c`.
