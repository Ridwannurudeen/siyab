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
| `NEAR_KEYS_DIR` | Directory of `<account>.json` key files with mode 600. |
| `VERIFIER_PUB` | Public verifier key passed to the Gate initializer. |
| `AGENT_PUB` | Function call public key registered with the Gate. |
| `AGENT_LABEL` | Label stored with the agent. |
| `TOKEN_SYMBOL` | Token symbol, defaults to `DMT`. |
| `TOKEN_DECIMALS` | Token decimals, defaults to `6`. |
| `TOKEN_SUPPLY` | Initial token supply in base units. |
| `GATE_WASM` | Gate wasm path, defaults to `res/gate.wasm`. |
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

`setup_testnet.sh` creates the token, registry and recorder subaccounts when absent, deploys missing contract state, registers the token storage accounts and checks the Gate balance.

`register_agent.sh` registers the agent and converts token caps to base units.

`set_allowlist.sh` replaces the agent allowlist with the supplied comma separated accounts.

`redeploy.sh` checks the version before and after deploying a selected Gate or registry wasm.

`make_attack_receiver.sh` creates a funded receiver account and prints the creation time.

`record_verdicts.sh` records a batch from a JSON argument file and can record labels from a second file.

Run a dry pass with `DRY_RUN=1 bash scripts/<script>.sh` and the required environment variables. `record_verdicts.sh` also needs its input JSON file to be readable during a dry pass. Subaccount key export from near-cli-rs legacy keychain storage is UNVERIFIED until the first run; see the marked lines in the scripts.
