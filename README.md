# ShieldBot for NEAR: security intelligence for agent money, enforced on chain.

ShieldBot for NEAR is a security intelligence layer for AI agent money on NEAR: a label and verdict store with a provenance class on every assertion, a verdict service that judges the receiver, the token, the amount and the memo of an agent payment, and the destination when the payment leaves NEAR through NEAR Intents, a NEAR guard contract that holds the agent's funds and pays out only on a signed ALLOW, and a NEAR registry where every verdict is recorded with its evidence hash. The agent holds a function call key to the guard and nothing else, and a verdict of Unknown is refused. It is unrelated to NEAR Intents SHIELD. Built for NEAR HACKATHON track 06; hints used: AGENT SECURITY, FUNCTION CALL KEYS. ShieldBot's EVM engine, its registries on Robinhood Chain and BOT Chain, and its SDKs are pre existing baseline; everything under `contracts/`, `gate/`, `intel/` and `mcp/` was built in the hackathon window; the diff is linked in New in this hackathon vs reused.

**Who your agent pays gets a verdict with evidence. Unknown never passes.**

## What the track asks for, and where it is

The phrases in the first column are quoted from NEAR's track description for AI Security & Defense; `tests/test_readme_track_map.py` asserts that every function, test, path and explorer link the table names exists.

| Track phrase | What this repository does | Code | Test | Testnet evidence |
|---|---|---|---|---|
| "spending limits" | The Gate keeps a per call cap and a window cap per asset for each agent, with separate unknown payee caps for receivers off its allowlist, and `request_transfer` refuses an amount over any of them. | `contracts/gate/src/lib.rs:request_transfer` | `contracts/gate/src/lib.rs::tests::per_call_cap_is_enforced`, `contracts/gate/src/lib.rs::tests::window_cap_is_enforced` | [refused over-cap transfer, REFUSED:per_call_cap](https://testnet.nearblocks.io/txns/2vsukFAzVFCUFAxSRSHd39ubi3xavfSB86gcRdr8nWgU) |
| "policy engines" | The owner sets each agent's allowlist and caps and the Gate's denylist on chain, and `request_transfer` refuses a denylisted receiver, a receiver off the allowlist when the agent has no unknown payee caps, and an unlisted payee over the unknown payee cap. | `contracts/gate/src/policy.rs:set_allowlist`, `contracts/gate/src/policy.rs:set_denylist`, `contracts/gate/src/lib.rs:request_transfer` | `contracts/gate/src/lib.rs::tests::denylisted_receiver_is_refused`, `contracts/gate/src/lib.rs::tests::unlisted_receiver_without_unknown_caps_is_refused`, `contracts/gate/src/lib.rs::tests::unknown_payee_cap_is_enforced` | [refused unknown payee, REFUSED:unknown_payee_cap](https://testnet.nearblocks.io/txns/H6WqJYSbwBPECSVqADRHAGA1qV1a2ictujUiBqaLe416) |
| "sandboxed wallets" | The agent's only credential is a function call key that `register_agent` adds to the Gate's account for `request_transfer` alone, with a non zero gas allowance, so the protocol rejects it calling `on_transfer`, calling the token's `ft_transfer` and attaching 1 yoctoNEAR. | `contracts/gate/src/policy.rs:register_agent` | `contracts/gate/tests/sandbox.rs::sandbox_transfer_policy_and_protocol_boundary` | recorded RPC errors with no receipt: `fixtures/rpc/attacks/05-callback.json`, `fixtures/rpc/attacks/06-token-direct.json`, `fixtures/rpc/attacks/08-one-yocto.json` |
| "reputation" | Every label store assertion names a provenance class and a source, the rules return DENY for a receiver or token holding a denylist or incident label and cite the assertion id, and the registry records each verdict with its evidence hash. | `intel/labels.py:validate`, `gate/rules.py:judge`, `contracts/registry/src/lib.rs:record_batch` | `tests/test_labels.py::ValidationTest.test_no_row_without_a_source`, `tests/test_rules.py::TierOneTest.test_denylist_and_incident_labels_deny_with_the_assertion_id`, `contracts/registry/tests/sandbox.rs::registry_records_over_rpc_and_rotates_recorder` | [record_batch of three verdicts](https://testnet.nearblocks.io/txns/CW5TWeRyvLngggXiBY5Y7xSZTD3BPWerFtsXczJdPRhW) |
| "scam detection" | For a receiver off the allowlist the rules return DENY on a lookalike of an allowlisted or labelled account, an account younger than 7 days or a dust creation deposit with either of those, Unknown when a check cannot run, and the EVM leg returns DENY for a NEAR Intents recipient with GoPlus theft labels. | `gate/rules.py:judge`, `gate/intents.py:judge_quote` | `tests/test_rules.py::TierTwoTest.test_lookalike_young_dust_funded_gives_the_denied_line`, `tests/test_rules.py::OutputsTest.test_missing_fixture_is_unknown_never_a_pass`, `tests/test_intents.py::DemoRecipientTest.test_the_demo_recipient_is_deny_with_the_labels_and_the_fetch_time` | the lookalike's DENY, `fixtures/registry/testnet-lookalike.json`, recorded in [record_batch](https://testnet.nearblocks.io/txns/CW5TWeRyvLngggXiBY5Y7xSZTD3BPWerFtsXczJdPRhW) |
| "Give AI funds without giving it the keys to everything" | The Gate holds the funds and pays out only on an ALLOW signed by a verifier key registered on the Gate and bound to the exact transfer; a missing, forged, reused, expired or non ALLOW verdict is refused. | `contracts/gate/src/lib.rs:request_transfer`, `contracts/gate/src/verdict.rs:verify` | `contracts/gate/src/lib.rs::tests::missing_verdict_is_refused`, `contracts/gate/src/lib.rs::tests::flipped_signature_is_refused`, `contracts/gate/src/lib.rs::tests::repeated_verdict_id_is_refused`, `contracts/gate/src/lib.rs::tests::expired_vector_is_refused`, `contracts/gate/src/lib.rs::tests::non_allow_literal_is_refused` | [allowed payment, invoice 2](https://testnet.nearblocks.io/txns/7agsRsZgeRm2vhK1LXug2qRQ2qudbAMjsLncuxGpvdwn), [refused forged signature, REFUSED:bad_signature](https://testnet.nearblocks.io/txns/Gv9P2py5B2B6KjGP2g25TYPFJVnzyU2tqnYgp7rhb9A9) |

## The problem

A NEAR function call key cannot attach NEAR to a call ([access keys](https://docs.near.org/protocol/accounts-contracts/access-keys)), and a NEP-141 `ft_transfer` requires a deposit of 1 yoctoNEAR ([NEP-141](https://github.com/near/NEPs/blob/master/neps/nep-0141.md)), so an agent that moves tokens with its own key holds a full access key. The protocol does not check who the agent pays. Money that leaves NEAR through NEAR Intents lands on EVM chains where the recipient may already carry a theft label.

## What it does

Three layers on one data model:

- **Intelligence** (`intel/`, `gate/rules.py`, `gate/intents.py`): a label store with a provenance class and a source on every assertion (`docs/LABEL-RECORD.md`), incident pages, NEAR rules over read only view calls, and the EVM leg: a `dry: true` 1Click quote exposes the destination chain and recipient before anything is signed, and the recipient is checked against GoPlus `address_security` labels.
- **Protocol** (`docs/VERDICT-RECORD.md`, `docs/REGISTRY.md`, `contracts/registry/`): an open verdict and label record format with an on chain NEAR registry, and the offline verifier `python -m gate verify`.
- **Security** (`contracts/gate/`, `mcp/`, `gate/scan.py`): the guard contract, a stdio MCP server with three tools, and the key hygiene scanner `python -m gate scan`.

Every line below is written by `python scripts/counts.py --readme README.md` from committed files. The latency line covers live testnet verdicts for the allowlisted payee, tier 1 checks only (`docs/EVIDENCE.md`).

<!-- counts:start -->

On 84 recorded mainnet receivers the rules returned 43 ALLOW, 14 DENY, 27 Unknown.

Label store: 3266 assertions over 3009 subjects (onchain 2894, official_source 350, third_party 18, inferred 4).

Incidents: 3 pages.

Feed: since 2026-10-29, 300 blocks, 0 events (0 code_hash_matches_incident, 0 fresh_dust_funded_pays_labelled, 0 watch_account_key_added, 0 labelled_account_large_transfer, 0 deploy_on_labelled_contract).

Flows: 1 exit traces, 1708 hops, depth 2 of 3, stopped while reading an account 1 hop from the subject.

Latency: p50 1843 ms, p95 2300 ms over 20 verdict runs.

<!-- counts:end -->

The 52 receivers of the first line (`fixtures/receivers/set.json`, chosen by `intel/__main__.py`) are recent counterparties of the two contracts `v2.ref-finance.near` and `wrap.near` (the last distinct counterparties of each in the FastNEAR Transfers API), the further NEAR accounts named in the Rhea Lend incident page, and catalog tokens of the label store; `fixtures/receivers/set.json` gives each one with why it was chosen. Implicit accounts are left out of the counterparties, unless the label store holds them as a catalog token.

Consumers today are the guard, the MCP server and the demo agent; none from a third party yet.

## How NEAR is used

- FUNCTION CALL KEYS: the agent's only credential is a function call key the Gate adds to its own account in `register_agent`, with `receiver_id` the Gate, `method_names` `["request_transfer"]` and a non zero gas allowance. Its receiver is the Gate and it can attach no deposit, so it cannot call `ft_transfer` on the token, move NEAR, add keys or deploy code; the attack table shows the protocol rejecting the `on_transfer`, `ft_transfer` and 1 yoctoNEAR attempts.
- AGENT SECURITY: a signed ALLOW bound to the exact transfer, refusal of anything else, replay and expiry rules, the owner's allowlist, denylist and unknown payee caps in the contract, NEP-297 events, `python -m gate scan`.
- NEP-141 with the 1 yoctoNEAR rule (the Gate attaches it to `ft_transfer`), NEP-297 events (`standard` `shieldbot-near`), named sub accounts for the token, the registry and the recorder.

The testnet Gate's access keys, replayed offline from the recording of 2 Nov (`python -m gate scan kestrelridge-main.testnet`):

```
account: kestrelridge-main.testnet on https://test.rpc.fastnear.com, read at block 275865306
access keys: 2
full access keys: 1
  ed25519:7gxqnnhZbSBFRnYmTg8T3UZ4WUULUNS97f4jnf1oygp8
empty method_names: 0, these may call any method on their receiver
dead keys: 0, allowance "0"
unlimited allowance: 0, allowance null
agent keys: 1 scoped to kestrelridge-main.testnet, 0 with method_names other than exactly ["request_transfer"]
verifier keys: the TEST key is ed25519:36ZbCswnc8QBvUxFF1pEwKwj6PZwgRWGe4nqcdy9HCzL
  on chain current: ed25519:9SVNJQqQjSKFo6LXpSJJXWKEjV65V93mHUsXGhmokyb5, different from the TEST key
labels: 0
```

Why the guard verifies a signature and the registry is the record: NEAR cross contract calls are asynchronous, and a callback runs in a later block. A guard that consulted the registry would pay a promise plus a private callback, two receipts and one or two blocks, and would hold its own state across them. So the guard checks a signed verdict passed in its arguments, in one receipt, and the registry is the public record: the three verdicts of the 1 Nov run (the vendor's ALLOW, the lookalike's DENY, the aged payee's DENY) are recorded there with their evidence hashes by the recorder account, any reader calls `latest` or `is_allowed` over RPC without a key, and any contract can read it through a promise. Records on NEAR hash with sha256 (`env::sha256`); the EVM registries hash with keccak256.

## Try it offline

Python 3.11 or later, from the repository root. Steps 3, 5 and 6 show POSIX shell forms (`sha256sum`, the `*` in the evidence path, `printf`, `cut`), and each also says how to run it with Python alone; `python scripts/smoke.py` (step 9) runs all nine with Python alone. The outputs below are pasted from runs on this machine; in this file 64 character hex values are shortened to their first and last eight characters, which the repository's hygiene rule requires.

1. `python -m gate demo --offline` replays the recorded testnet responses through the rules: the allowlisted vendor (ALLOW, signed with the TEST key and verified back), the lookalike (DENY), the aged payee whose archival block is left out of the fixtures (UNKNOWN, the checks that could not run named), and the EVM leg (DENY). It exits 0. The first lines:

   ```
   ShieldBot for NEAR offline demo v1: testnet responses recorded on 1 and 2 Nov 2026, replayed from
   fixtures/demo with the TEST verifier key and no network. The TEST key is derived from a published
   seed and is never registered on testnet. The demo clock is pinned to 2026-11-02T07:00:00Z, inside
   the recording window, so the 7 day checks replay as recorded.

   The allowlisted vendor: 1000000 of token.kestrelridge-main.testnet to kestrelridge-shop.testnet, memo "invoice 1"
     pass    receiver_exists: kestrelridge-shop.testnet exists
     pass    label_denylist: no denylist or incident label on kestrelridge-shop.testnet or token.kestrelridge-main.testnet
     pass    token_in_catalog: token.kestrelridge-main.testnet is a cap asset of the agent
     pass    ft_metadata_sane: symbol "DMT", 6 decimals
     pass    storage_registered: kestrelridge-shop.testnet is registered on token.kestrelridge-main.testnet
     pass    memo_injection: local patterns: no match
     ALLOW, signed with the TEST key (key id cur); signature verified with gate.ed25519: True
   ```

   The lookalike's verdict line, and the EVM leg's last lines:

   ```
     DENIED. kestrelridge-sh0p.testnet is a lookalike of kestrelridge-shop.testnet. Created less than 7 d ago. Not on the owner's allowlist. Not registered on the token.
   ```

   ```
     DENIED. The recipient 0x1fcdb04d0c5364fbd92c73ca8af9baa72c269107 carries the GoPlus theft labels stealing_attack (GoPlus data_source "SlowMist,BlockSec", fetched 2026-10-29T11:48:40Z).
     Nothing was signed, nothing was deposited.
   ```

   Each verdict also prints its evidence sha256 and the time the checks took. These are replay timings from fixtures, not live latency; the verdict id is random, so the ALLOW's canonical message and evidence hash differ on every run.

2. `python -m unittest discover -s tests` runs the Python suite: the rules, the canonical vectors, the evidence hashing, `gate/ed25519.py` against the RFC 8032 section 7.1 vectors, the label store, the incidents, the EVM leg, `scan`, the MCP server, the README counts and tables. It ends with a line `OK`, with a skip count: the PyNaCl and py-near cross checks skip when those packages are absent, the four py-near skips each print a `skipped:` line with the reason in the default output, and the three PyNaCl skips show their reason only with `-v`; the wasm checks run on the committed files under `res/`.
3. `sha256sum -c res/gate.wasm.sha256 res/registry.wasm.sha256` checks the committed wasm files against their checksums, and `python -m gate codehash` prints the sha256 and the base58 `code_hash` to compare with the testnet `code_hash` in `docs/TESTNET.md`. The testnet `code_hash` equals base58 of the sha256 of the wasm. Without `sha256sum`, the `sha256:` line `python -m gate codehash` prints is compared with the `.sha256` file. Reproducing the hash from source needs Docker; checking it against testnet needs network.

   ```
   res/gate.wasm: OK
   res/registry.wasm: OK
   ```

   `python -m gate codehash res/gate.wasm`, then `python -m gate codehash res/registry.wasm`:

   ```
   sha256: f7a09b28...fd14a107
   code_hash: Hfdk3jGUFxQyP8wEEoejNUkZ81bE9gDmoUBfGRghR5iE
   sha256: 2a154191...f0ec0cfa
   code_hash: 3qGvVjTHVyvYVGQjJhqQGJMzmkUHwEbTCfWqHhXnW4VX
   ```

4. `python -m gate wasm-exports res/gate.wasm` and `python -m gate wasm-exports res/registry.wasm` parse the wasm export section and print the exported function names, sorted: each contract's methods plus `__contract_abi`.

   ```
   __contract_abi
   contract_source_metadata
   get_agent
   get_pending
   get_policy
   get_verifier_keys
   is_verdict_used
   new
   on_transfer
   register_agent
   request_transfer
   revoke_agent
   set_agent_enabled
   set_allowlist
   set_caps
   set_denylist
   set_max_ttl
   set_paused
   set_verifier_keys
   version
   withdraw
   ```

   ```
   __contract_abi
   contract_source_metadata
   get_label
   get_recorder
   is_allowed
   latest
   new
   record
   record_batch
   record_label
   set_recorder
   total_labels
   total_records
   total_subjects
   version
   ```

5. `python -m gate verify fixtures/registry/testnet-vendor.json fixtures/evidence/7e77187d*.json` recomputes the evidence hash, compares it and the verdict with the registry view, and prints the NEP-297 fields to compare with the explorer. The evidence file is named after the record's `evidence_hash`: `fixtures/evidence/7e77187d...c88bb2cc.json`, the only file there starting `7e77187d`; where the shell does not expand `*` (PowerShell, cmd), type its full name, which tab completion fills in:

   ```
   record shape: view
   evidence sha256: 7e77187d...c88bb2cc
   record evidence_hash: 7e77187d...c88bb2cc
   NEP-297 fields to compare with the explorer:
     standard shieldbot-near, version 1.0.0, event verdict_recorded
     subject kestrelridge-shop.testnet (from the evidence; the view shape has none)
     verdict ALLOW, evidence_hash 7e77187d...c88bb2cc
     observed_block 275799179, verdict_id 1b29052381ecf3fc1937b36db2539678
   ok   evidence sha256 equals the record's evidence_hash
   ok   evidence verdict equals the record's verdict
   verified
   ```

6. `python -m mcp.server` answers JSON-RPC on stdin. A scripted `initialize` and `tools/list`, cut to 150 characters a line, first in a POSIX shell, then with Python alone (any shell):

   ```
   printf '%s\n' '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"t","version":"0"}}}' '{"jsonrpc":"2.0","method":"notifications/initialized"}' '{"jsonrpc":"2.0","id":2,"method":"tools/list"}' | python -m mcp.server 2>/dev/null | cut -c1-150
   ```

   ```
   python -c "import json, subprocess, sys; msgs = [{'jsonrpc': '2.0', 'id': 1, 'method': 'initialize', 'params': {'protocolVersion': '2025-06-18', 'capabilities': {}, 'clientInfo': {'name': 't', 'version': '0'}}}, {'jsonrpc': '2.0', 'method': 'notifications/initialized'}, {'jsonrpc': '2.0', 'id': 2, 'method': 'tools/list'}]; out = subprocess.run([sys.executable, '-m', 'mcp.server'], input=''.join(json.dumps(m) + chr(10) for m in msgs), capture_output=True, text=True).stdout; [print(line[:150]) for line in out.splitlines()]"
   ```

   ```
   {"jsonrpc": "2.0", "id": 1, "result": {"protocolVersion": "2025-06-18", "capabilities": {"tools": {}}, "serverInfo": {"name": "shieldbot-near", "versi
   {"jsonrpc": "2.0", "id": 2, "result": {"tools": [{"name": "check_near_payment", "description": "Judge one NEAR agent payment (receiver, token, amount,
   ```

7. `python -m intel counts` and `python -m gate incident rhea` print the label counts and the Rhea Lend incident page from committed files:

   ```
   412 assertions over 262 subjects: onchain 40, official_source 350, third_party 18, inferred 4, unknown 0
   ```

   ```
   Rhea Lend margin trading route exploit (2026-04-16)
   Chains: near, eth

   Sources
     [rekt-rhea] rekt.news, https://rekt.news/rhea-finance-rekt
       sha256 1743d141...eb0e6c1f
       fetched 2026-10-29T11:26:35Z
   ```

8. `fixtures/canon/*.json` are read by both `python -m unittest tests.test_canon` and the Rust unit tests in `contracts/gate` (`cargo test --lib`, network once for crates.io), so the two implementations of the signed message are checked against the same bytes without a network.
9. `python -m intel receivers` replays the rules over the recorded mainnet receiver set from `fixtures/receivers/`, rewrites the committed judgement files under `fixtures/receivers/judgements/` from the replay, and prints the measured line; `git status` shows no change afterwards:

   ```
   On 52 recorded mainnet receivers the rules returned 32 ALLOW, 19 DENY, 1 Unknown.
   ```

   `python scripts/smoke.py` runs the nine steps above with Python alone and read only: each command in replay with the network refused, step 2 inside the smoke process without `tests/test_smoke.py` (which runs `smoke.py`), step 8's Python half (the Rust half needs crates.io), and step 9 into a temporary directory whose judgements it compares byte for byte with `fixtures/receivers/judgements/`. It prints PASS or FAIL with the reason for each step, then the summary, and exits 0 only on nine passes; every temporary file goes into one directory it removes at the end. Its last line:

   ```
   9 of 9 PASS
   ```

Fixtures under `fixtures/rpc/`, `fixtures/demo/`, `fixtures/scan/`, `fixtures/intents/`, `fixtures/goplus/` and `fixtures/evm/` are recorded JSON-RPC, NearBlocks, 1Click, GoPlus and ShieldBot responses, keyed by the sha256 of the request. A request with no fixture reads Unknown, never a pass.

| Command | Needs | Label |
|---|---|---|
| `python -m gate demo --offline`, `python -m unittest discover -s tests`, `python -m gate codehash`, `python -m gate wasm-exports`, `python -m gate verify`, `python -m mcp.server`, `python -m intel counts` | Python 3.11 or later | offline |
| `cargo test --lib` (in `contracts/gate`, `contracts/registry`, `contracts/test-token`) | Rust per `rust-toolchain.toml`, crates.io once | network once |
| `cargo test --test sandbox` | Linux or macOS, sandbox download | network, WSL |
| `cargo near build reproducible-wasm` | Docker, image pull | network, Docker |
| `python -m gate demo --live`, `python -m gate pay`, `python -m gate attack`, `python -m gate scan <account>` with `GATE_FIXTURES_MODE=off`, `python -m intel feed`, `python -m intel seed` | testnet or mainnet RPC, the live venv | network |

## Try it live (network)

Testnet accounts and deployments are listed in `docs/TESTNET.md`; the evidence documents in `docs/EVIDENCE.md`.

- The allowed payment, invoice 2, 10 DMT to `kestrelridge-shop.testnet`: [transaction](https://testnet.nearblocks.io/txns/7agsRsZgeRm2vhK1LXug2qRQ2qudbAMjsLncuxGpvdwn), with `transfer_scheduled`, the token's `ft_transfer` and `transfer_completed` in its logs.
- Invoice 3, 10 DMT to the vendor on 2 Nov after the redeploy, ALLOW: transaction `DU55HxkN...zXNwGh` (`docs/TESTNET.md`).
- Invoice 4, 10 DMT to the vendor on 2 Nov, an ALLOW the MCP server's `check_near_payment` signed with the live key (`GATE_LIVE_SIGNING=1`), paid with `python -m gate pay --verdict-file`: [transaction](https://testnet.nearblocks.io/txns/AADLzYbqhEm6D83osYfQQ5SMED86chiS77XGsA8d7rTJ), request id 5, with `transfer_scheduled`, the token's `ft_transfer` and `transfer_completed` in its logs.
- The 2 Nov redeploy with the reproducible wasm: Gate `GTHR62ER...Wik5sW`, registry `9GzMDrYe...UPBb66`, each `code_hash` MATCH (`docs/TESTNET.md`, `res/BUILD.md`).
- The refused lookalike `kestrelridge-sh0p.testnet`, sent through `python -m gate attack no-verdict`: [failed receipt `REFUSED:no_verdict`](https://testnet.nearblocks.io/txns/6kfxEFCR2uuoiM6cpj8CmGAfmRjKTYNtFq4igkv48qK7).
- The registry records for the vendor, the lookalike and the aged payee: [`record_batch`](https://testnet.nearblocks.io/txns/CW5TWeRyvLngggXiBY5Y7xSZTD3BPWerFtsXczJdPRhW); read one without a key with `near contract call-function as-read-only registry.kestrelridge-main.testnet latest json-args '{"subject":"kestrelridge-shop.testnet"}' network-config testnet now`.
- The refused unknown payee `examples.testnet`, an aged account not on the allowlist, 2 DMT through `python -m gate attack unknown-payee` with the ALLOW the service returned: [failed receipt `REFUSED:unknown_payee_cap`](https://testnet.nearblocks.io/txns/H6WqJYSbwBPECSVqADRHAGA1qV1a2ictujUiBqaLe416).
- The EVM leg: preflight, no transaction on any chain; the recorded dry quote and GoPlus answer are under `fixtures/intents/` and `fixtures/goplus/`.

To reproduce with your own testnet accounts, set the variables of `env.example` and run `scripts/setup_testnet.sh`, `scripts/register_agent.sh` and `scripts/set_allowlist.sh`; `DRY_RUN=1` prints every command line without reading a key or sending a transaction (`scripts/README.md`).

## Architecture

```
contracts/gate/       guard: holds the funds, verifies the signed ALLOW (lib.rs, verdict.rs), allowlist, denylist, caps (policy.rs)
contracts/registry/   registry: record, record_batch, latest, is_allowed, record_label, NEP-297 events
contracts/test-token/ NEP-141 test token (DMT) for the sandbox and testnet runs
gate/rules.py         tiered NEAR rules over view calls and the label store; ALLOW, DENY or UNKNOWN
gate/verdict.py       the verdict service: rules, evidence document (evidence.py), canonical message (canon.py), signature
gate/intents.py       the EVM leg: 1Click dry quote, GoPlus address labels, ShieldBot firewall for contracts
gate/agent.py         pay and attack over py-near; gate/scan.py key hygiene; gate/verify.py offline record check
intel/                label store (labels.py, labels.jsonl), seeds, incidents, feed, flow trace, static pages
mcp/server.py         stdio MCP server: check_near_payment, check_intents_quote, scan_near_account
fixtures/, tests/     recorded responses replayed offline; the Python tests
```

## Integrate

1. In Python: `gate.verdict.request(kind, token, receiver, amount, memo, gate=..., agent_pk_hex=..., policy=..., store=..., rpc=..., nearblocks=..., signer=...)` returns a dict with `outcome`, `verdict` (the signed Verdict object on ALLOW, else None), `request`, `checks` and `evidence_hash`.
2. On the command line: `python -m gate verdict --kind ft --token <token> --receiver <account> --amount <base units> --memo <memo> --gate <gate account>`.
3. As MCP tools (`check_near_payment`, `check_intents_quote`, `scan_near_account`), in Claude Code from the repository root: `claude mcp add shieldbot-near -- python -m mcp.server`. By default `check_near_payment` signs an ALLOW with the TEST key, which the testnet Gate does not trust (its verifier key is the live one), so it is for trying the rules. It signs with the live key only when the operator sets all five conditions: `GATE_LIVE_SIGNING=1`, `VERIFIER_KEY_FILE`, `GATE_FIXTURES_MODE=off`, a `.testnet` `GATE_ACCOUNT_ID` and `GATE_POLICY` unset, with `AGENT_PUB` naming the agent key; the verdict is then bound to that agent key and judged against the Gate's `get_agent` read at every call. With the flag set and any other condition missing the tool answers an error and signs nothing; it never falls back to the TEST key. The result's `signer` names the key that signed.
4. Other clients, the Codex configuration and the environment for live reads: `mcp/README.md`.
5. A verdict a Gate pays on comes from the operator's verdict service holding the live key: `python -m gate verdict --live` with `VERIFIER_KEY_FILE` set, or `python -m gate pay`. The agent passes its `request` and `verdict` objects through unchanged to `request_transfer(request, verdict)`.

## Trust model

- The live verifier key is held by the team's operator, outside the repository, in the file `VERIFIER_KEY_FILE` names. The TEST key is derived from the public seed `sha256(b"gate test verifier v1")`, signs the test vectors, the offline runs, `python -m gate demo --live` and the MCP server's ALLOWs by default, and is not registered on testnet, so the testnet Gate does not trust them: `scan` above shows the on chain key differs from it. An ALLOW a Gate pays on is signed with the live key, by `python -m gate verdict --live`, `python -m gate pay` without `--verdict-file`, or the MCP server when the operator sets its five live conditions (`mcp/README.md`), each reading `VERIFIER_KEY_FILE`.
- The MCP server is a stdio process with no caller authentication: whoever can start it and call its tools can obtain verdicts for the configured agent, so the operator controls access to it. Each live ALLOW is bounded by the Gate's caps and the 5 minute expiry. The evidence document of a live verdict is written to the temporary directory the server names on stderr, which the server removes when it exits unless `GATE_KEEP_EVIDENCE=1`; to record the verdict in the registry, the operator sets `GATE_KEEP_EVIDENCE=1` or copies the document before the server exits.
- The recorder key (`recorder.kestrelridge-main.testnet`), the agent function call key and the testnet Gate account key are held by the team's operator; the owner account key and every mainnet key are held by the owner. No private key is in the repository.
- Without `--verdict-file`, `python -m gate pay` runs the verdict service and the agent in one process on one machine.
- Verdict latency: the latency line of the counts block, live testnet verdicts for the allowlisted payee, tier 1 checks.
- A wrong ALLOW costs at most the per call cap, then the window cap; for a payee off the allowlist, the separate unknown payee caps (`per_call_cap_is_enforced`, `window_cap_is_enforced`, `unknown_payee_cap_is_enforced` in `contracts/gate/src/lib.rs`).
- The testnet Gate kept a full access key during the build (`scan` above).
- The testnet Gate and registry run the reproducible wasm of `res/`: on 2 Nov `scripts/redeploy.sh` deployed it without an init call, state kept, and each `code_hash` read back equals base58 of the sha256 of the wasm (MATCH, `docs/TESTNET.md`). The Gate's `contract_source_metadata` view returns the NEP-330 link to source commit `eb54c8e` (`res/BUILD.md`).

## Claims to code

Copied from `docs/CLAIMS.md`; `tests/test_readme_claims.py` asserts the two tables are identical and that every file, function and test named exists.

| Claim | File:function | Test | Evidence | idea |
|---|---|---|---|---|
| The receiver rules check available evidence and return Unknown when a required check cannot run. | `gate/rules.py:judge` | `tests/test_rules.py::OutputsTest.test_missing_fixture_is_unknown_never_a_pass` | offline | yes |
| The Gate requires a verdict and rejects a missing verdict. | `contracts/gate/src/lib.rs:request_transfer` | `contracts/gate/src/lib.rs::tests::missing_verdict_is_refused` | offline | yes |
| The Gate refuses a verdict with a forged signature. | `contracts/gate/src/lib.rs:request_transfer` | `contracts/gate/src/lib.rs::tests::flipped_signature_is_refused` | `fixtures/rpc/attacks/02-forged-signature.json` | yes |
| A non ALLOW verdict is refused by the Gate. | `contracts/gate/src/lib.rs:request_transfer` | `contracts/gate/src/lib.rs::tests::non_allow_literal_is_refused` | offline | yes |
| A valid ALLOW schedules a bounded transfer and emits the transfer event. | `contracts/gate/src/lib.rs:request_transfer` | `contracts/gate/src/lib.rs::tests::happy_path_debits_cap_and_emits_events` | offline | yes |
| The `pay` command always uses the verdict path and has no bypass. | `gate/agent.py:pay` | `tests/test_agent.py::NoBypassTest.test_the_source_has_no_bypass_word_and_pay_has_no_bypass_option` | offline | yes |
| The registry records verdicts and its latest view returns the evidence hash. | `contracts/registry/src/lib.rs:record_batch` | `contracts/registry/tests/sandbox.rs::registry_records_over_rpc_and_rotates_recorder` | offline | yes |
| The agent function call key is scoped to request_transfer. | `contracts/gate/tests/sandbox.rs:sandbox_transfer_policy_and_protocol_boundary` | `contracts/gate/tests/sandbox.rs::sandbox_transfer_policy_and_protocol_boundary` | offline | yes |
| The agent key calling `on_transfer` is refused by the protocol (transaction rejected), not by the contract. | `contracts/gate/tests/sandbox.rs:sandbox_transfer_policy_and_protocol_boundary` | `contracts/gate/tests/sandbox.rs::sandbox_transfer_policy_and_protocol_boundary` | `fixtures/rpc/attacks/05-callback.json` | yes |
| The agent key calling token `ft_transfer` is refused by the protocol (transaction rejected), not by the contract. | `contracts/gate/tests/sandbox.rs:sandbox_transfer_policy_and_protocol_boundary` | `contracts/gate/tests/sandbox.rs::sandbox_transfer_policy_and_protocol_boundary` | `fixtures/rpc/attacks/06-token-direct.json` | yes |
| The agent key attaching 1 yoctoNEAR is refused by the protocol (transaction rejected), not by the contract. | `contracts/gate/tests/sandbox.rs:sandbox_transfer_policy_and_protocol_boundary` | `contracts/gate/tests/sandbox.rs::sandbox_transfer_policy_and_protocol_boundary` | `fixtures/rpc/attacks/08-one-yocto.json` | yes |
| Reusing a verdict is refused with `REFUSED:verdict_used`. | `contracts/gate/src/lib.rs:request_transfer` | `contracts/gate/src/lib.rs::tests::repeated_verdict_id_is_refused` | `fixtures/rpc/attacks/03-replay.json` | yes |
| An expired ALLOW is refused with `REFUSED:expired`. | `contracts/gate/src/lib.rs:request_transfer` | `contracts/gate/src/lib.rs::tests::expired_vector_is_refused` | `fixtures/rpc/attacks/03-expired.json` | yes |
| A transfer over its per call cap is refused. | `contracts/gate/src/lib.rs:request_transfer` | `contracts/gate/src/lib.rs::tests::per_call_cap_is_enforced` | `fixtures/rpc/attacks/04-over-cap.json` | yes |
| The NEAR Intents EVM leg is preflight before signing; no transaction on any chain. | `gate/intents.py:judge_quote` | `tests/test_intents.py::DemoRecipientTest.test_the_demo_recipient_is_deny_with_the_labels_and_the_fetch_time` | `fixtures/intents/`, `fixtures/goplus/` | yes |
| The demo replays the recorded NEAR and EVM outcomes offline. | `gate/demo.py:run` | `tests/test_demo.py::DemoTest.test_four_outcomes` | `fixtures/demo/` | yes |
| `scripts/smoke.py` runs the nine offline steps read only and reports PASS or FAIL per step. | `scripts/smoke.py:main` | `tests/test_smoke.py::SmokeTest.test_smoke_runs_nine_steps_read_only_and_leaves_the_tree_unchanged` | offline | no |
| The MCP server exposes the guard, EVM quote and account scan tools. | `mcp/server.py:serve` | `tests/test_mcp.py::McpServerTest.test_a_scripted_session` | offline | yes |
| The MCP server signs with the live key only when every live variable is set and refuses a partial configuration. | `mcp/server.py:_live_signing` | `tests/test_mcp.py::McpLiveSigningTest.test_every_partial_live_configuration_is_refused_and_nothing_is_signed` | offline | no |
| Consumers today are the guard, the MCP server and the demo agent; none from a third party yet. | `mcp/server.py:serve` | `tests/test_mcp.py::McpServerTest.test_a_scripted_session` | offline | no |
| The mainnet receiver outcome line is generated from recorded receiver judgements. | `scripts/counts.py:receivers_line` | `tests/test_receivers.py::ReceiverSetTest.test_a_replay_rejudges_every_subject_to_the_committed_files_and_the_counts_line` | `fixtures/receivers/` | no |
| The label store count is generated from committed assertions and provenance. | `scripts/counts.py:labels_line` | `tests/test_counts.py::CountsTest.test_the_label_and_incident_lines_equal_a_recount` | `intel/labels.jsonl` | no |
| Incident page totals are generated from committed incident files. | `scripts/counts.py:incidents_line` | `tests/test_counts.py::CountsTest.test_the_label_and_incident_lines_equal_a_recount` | `intel/incidents/` | no |
| Feed and flow totals are generated from committed state, events and traces. | `scripts/counts.py:feed_line` | `tests/test_counts.py::CountsTest.test_the_feed_and_flows_lines_equal_a_recount` | `intel/feed/`, `intel/flows/` | no |
| Verdict latency percentiles are generated from the recorded run fixtures. | `scripts/counts.py:latency_line` | `tests/test_counts.py::CountsTest.test_latency_is_not_measured_without_files_and_nearest_rank_with_them` | `fixtures/latency/` | no |
| README count lines are generated by `scripts/counts.py` from committed files. | `scripts/counts.py:lines` | `tests/test_counts.py::CountsTest.test_the_lines_follow_the_fixed_format_in_order` | offline | no |
| A TEE is not built. | `tests/test_attack_evidence.py:unbuilt_limit_rows` | `tests/test_attack_evidence.py::AttackEvidenceTest.test_unbuilt_surfaces_are_stated_as_limits` | offline | no |
| An OutLayer approver is not built. | `tests/test_attack_evidence.py:unbuilt_limit_rows` | `tests/test_attack_evidence.py::AttackEvidenceTest.test_unbuilt_surfaces_are_stated_as_limits` | offline | no |
| A browser extension for NEAR is not built. | `tests/test_attack_evidence.py:unbuilt_limit_rows` | `tests/test_attack_evidence.py::AttackEvidenceTest.test_unbuilt_surfaces_are_stated_as_limits` | offline | no |
| A Telegram bot for NEAR is not built. | `tests/test_attack_evidence.py:unbuilt_limit_rows` | `tests/test_attack_evidence.py::AttackEvidenceTest.test_unbuilt_surfaces_are_stated_as_limits` | offline | no |

## Adversarial table

Copied from `docs/ATTACKS.md`. Each NEAR command is `python -m gate attack <name>`, the simulation switch; `python -m gate pay` always requires a verdict and has no way to skip it.

| # | Attack and command | What the agent sends | Where it is refused | Expected string | Fixture | Explorer link | Test for the same refusal |
|---:|---|---|---|---|---|---|---|
| 1 | no-verdict, `python -m gate attack no-verdict` | `request_transfer` to lookalike `kestrelridge-sh0p.testnet`, with no verdict | Gate contract | `REFUSED:no_verdict` | `fixtures/rpc/attacks/01-no-verdict.json` | [refused transaction](https://testnet.nearblocks.io/txns/6kfxEFCR2uuoiM6cpj8CmGAfmRjKTYNtFq4igkv48qK7) | `contracts/gate/src/lib.rs::tests::missing_verdict_is_refused` |
| 2 | forged-signature, `python -m gate attack forged-signature` | `request_transfer` with a signature from the TEST key, which the Gate does not trust | Gate contract | `REFUSED:bad_signature` | `fixtures/rpc/attacks/02-forged-signature.json` | [refused transaction](https://testnet.nearblocks.io/txns/Gv9P2py5B2B6KjGP2g25TYPFJVnzyU2tqnYgp7rhb9A9) | `contracts/gate/src/lib.rs::tests::flipped_signature_is_refused` |
| 3 | replay and expired, `python -m gate attack replay` and `python -m gate attack expired` | The first replay submission is the authorized use; submitting that verdict again within five minutes, and submitting a day old ALLOW | Gate contract | Second submission: `REFUSED:verdict_used`; day old ALLOW: `REFUSED:expired` | `fixtures/rpc/attacks/03-replay.json`, `fixtures/rpc/attacks/03-expired.json` | [refused replay](https://testnet.nearblocks.io/txns/2FFnYUrXYLLgAHA2vsNud2mawU1xttkCQaNXg2yZG6Pn), [expired verdict](https://testnet.nearblocks.io/txns/5KG3sPSqdvUP6y6a5iR1APzcHxb8pSsVxKiffWbNvQ7H) | `contracts/gate/src/lib.rs::tests::repeated_verdict_id_is_refused`, `contracts/gate/src/lib.rs::tests::expired_vector_is_refused` |
| 4 | over-cap, `python -m gate attack over-cap` | `request_transfer` for 11 DMT with a 10 DMT per call cap | Gate contract | `REFUSED:per_call_cap` | `fixtures/rpc/attacks/04-over-cap.json` | [refused transaction](https://testnet.nearblocks.io/txns/2vsukFAzVFCUFAxSRSHd39ubi3xavfSB86gcRdr8nWgU) | `contracts/gate/src/lib.rs::tests::per_call_cap_is_enforced` |
| 5 | callback, `python -m gate attack callback` | The agent function call key calls `on_transfer` | NEAR protocol boundary: refused by the protocol (transaction rejected), not by the contract | `MethodNameMismatch` | `fixtures/rpc/attacks/05-callback.json` | No receipt, no explorer link | `contracts/gate/tests/sandbox.rs::sandbox_transfer_policy_and_protocol_boundary` |
| 6 | token-direct, `python -m gate attack token-direct` | The agent function call key calls `ft_transfer` on the token | NEAR protocol boundary: refused by the protocol (transaction rejected), not by the contract | `ReceiverMismatch` | `fixtures/rpc/attacks/06-token-direct.json` | No receipt, no explorer link | `contracts/gate/tests/sandbox.rs::sandbox_transfer_policy_and_protocol_boundary` |
| 7 | service-down, `python -m gate attack service-down` | With the verdict signer unavailable, the agent submits `request_transfer` without a verdict | Gate contract | `REFUSED:no_verdict` | `fixtures/rpc/attacks/07-service-down.json` | [refused transaction](https://testnet.nearblocks.io/txns/CTm9yDGHNiXD94jsF8QdeXWeEKz7qxiYbLT1zzvt9wnk) | `contracts/gate/src/lib.rs::tests::missing_verdict_is_refused` |
| 8 | one-yocto, `python -m gate attack one-yocto` | The agent function call key attaches 1 yoctoNEAR to `request_transfer` | NEAR protocol boundary: refused by the protocol (transaction rejected), not by the contract | `DepositWithFunctionCall` | `fixtures/rpc/attacks/08-one-yocto.json` | No receipt, no explorer link | `contracts/gate/tests/sandbox.rs::sandbox_transfer_policy_and_protocol_boundary` |
| 9 | unknown-payee, `python -m gate attack unknown-payee` | `request_transfer` for 2 DMT to `examples.testnet`, an aged account not on the allowlist, with the ALLOW the service returned for it; the unknown payee cap is 1 DMT per call | Gate contract | `REFUSED:unknown_payee_cap` | `fixtures/rpc/attacks/09-unknown-payee.json` | [refused transaction](https://testnet.nearblocks.io/txns/H6WqJYSbwBPECSVqADRHAGA1qV1a2ictujUiBqaLe416) | `contracts/gate/src/lib.rs::tests::unknown_payee_cap_is_enforced` |
| preflight | EVM leg, `preflight, no transaction on any chain` | Recorded 1Click dry quote and GoPlus answer for the destination | Preflight verdict, before signing | `DENY: stealing_attack` from the recorded GoPlus answer | `fixtures/intents/`, `fixtures/goplus/` | No transaction on any chain | `tests/test_intents.py::DemoRecipientTest.test_the_demo_recipient_is_deny_with_the_labels_and_the_fetch_time` |

10 attempts, 10 refusals, 0 bypasses. The replay record contains two submissions: its first, authorized use succeeds, and only the second submission is the replay attempt counted above. The expired submission is a separate refused case. The three protocol refusals have stored RPC error JSON and no receipt, so they have no explorer links.

## Limits

- No TEE. The rules are view call heuristics; a check that cannot run reads Unknown, and Unknown is refused. Unknown is expected whenever a data source is down or holds no data: NearBlocks testnet has indexed nothing since 27 Sep, so testnet account age comes from the oldest access key nonce and an archival block read (`docs/SPIKES.md` S8).
- ALLOW is the absence of a known scam signal, not proof of identity; the unknown payee cap bounds what a patient attacker can take.
- The measured line of the counts block is the replay of a recorded mainnet receiver set, not a rate over all NEAR payees.
- `code_without_metadata` (contract code without `contract_source_metadata`) alone is a pass with a detail; it counts only with another tier 2 signal (`account_age`, `dust_funded` or `lookalike`). That moved three deployed protocol contracts of the receiver set from DENY to ALLOW (`grid.deltatrade.near`, `dclv2.ref-labs.near`, `aggregatedex.near`, `fixtures/receivers/judgements/`).
- The testnet Gate kept a full access key during the build.
- A used verdict stays used when the transfer then fails; the agent needs a fresh ALLOW. A failed transfer restores the debited cap (`failed_transfer_restores_the_debited_cap`), except when the agent was revoked, the cap removed or the window rolled in between (the four `failed_transfer_after_*_is_not_restored` tests). If the completion callback itself fails, the budget stays debited.
- Storage for used verdict ids, pending requests and agent keys is staked from the Gate's own balance; the registry pays storage for its records (`docs/REGISTRY.md`). `request_transfer` refuses a call whose gas cannot cover the transfer and its callback (`REFUSED:low_gas`).
- The agent key's allowance is a gas budget, charged at the gas buy price and credited back on refund, so size it at prepaid gas times expected calls; `register_agent` refuses an allowance of 0 (`REFUSED:zero_allowance`), which the host would read as unlimited.
- `register_agent` refuses a key that already has an entry (`REFUSED:agent_exists`); an `AddKey` failure for any other reason can leave an agent entry, which the owner removes with `revoke_agent`.
- NearBlocks free tier: 6 calls a minute and 333 a day, personal use, with attribution. `gate/rpc.py` spaces NearBlocks fetches at least 10 s apart and stops at 300 a day per process, and record mode reuses a fixture for 24 hours; a 429 or a missing fixture reads Unknown.
- The production engine checks contracts and approval spenders; recipient labels on a bare transfer come from the label provider through the NEAR rule (`gate/intents.py` calls the firewall only for a recipient with contract code).
- No mainnet deployment. The EVM leg is a preflight: nothing is signed and nothing is deposited, and its verdicts are not written to the NEAR registry.
- Reproducing the hash from source needs Docker; checking it against testnet needs network.
- The signature is pure Ed25519 over the sha256 digest, not Ed25519ph.
- Dependencies: NEAR RPC, NearBlocks, the archival RPC, GoPlus, 1Click and the ShieldBot API; each check that needs one reads Unknown when it is down.
- What waits: chain wide coverage, clustering, exchange labels, the NEAR Intents SHIELD feed, alerts, search, numeric risk ratings, billing, chain signatures linking, any trace of an unlabelled account. The browser extension and the Telegram bot do not cover NEAR.

## Relationship to other NEAR work

This project is not related to NEAR Intents SHIELD, the security policy layer behind NEAR Intents and 1Click (https://docs.near-intents.org/security-compliance/proactive-intents-security.md). SHIELD protects the protocol at settlement; this layer protects the agent before it signs, with evidence its owner can audit; and nothing screens a direct NEAR transfer at all.

OutLayer owns custody and static policy; this layer supplies the judgement. An OutLayer approver is not built. IronClaw reaches MCP servers over HTTPS through IronHub; this server is stdio and is not integrated with it.

Prior art, not claimed for NEAR: the ShieldBot verdict guard and registries on Robinhood Chain (4663) and BOT Chain (677), in the baseline repository below.

## New in this hackathon vs reused

- Baseline: the public repository [Ridwannurudeen/shieldbot](https://github.com/Ridwannurudeen/shieldbot/tree/9e2a6ac) at commit `9e2a6ac`: ShieldBot's EVM engine, its registries on Robinhood Chain and BOT Chain, its SDKs and its production MCP server are pre existing.
- Called over HTTPS, each optional and Unknown when `SHIELDBOT_API_URL` is empty or the call fails: `POST /api/firewall` (the EVM leg, contracts only), `POST /api/scan/injection` (the memo check) and `GET /api/health`. GoPlus `address_security` is called directly by the NEAR layer.
- Copied: the `THEFT_LABELS` constant from `services/counterparty_service.py:27-41`, kept in `gate/intents.py` with its origin in a comment. Designs ported as new code, each from the baseline repository: the verdict vocabulary (`core/verdicts.py`), coverage and failed sources (`core/policy.py`), canonical bytes plus hash evidence (`core/verdict_evidence.py`), the registry record shape (`contracts/base/src/ShieldBotVerdictRegistry.sol`), the rotatable recorder (`services/verdict_publisher.py`), and the verify without the API recipe (from the baseline's offline verification guide).
- Published code and packages: the public domain Ed25519 reference implementation (ported to Python 3 in `gate/ed25519.py`, origin and changes in its header), PyNaCl (the live signer), py-near (the live agent path), near-sdk and near-contract-standards 5.29.1, near-workspaces 0.23.0, cargo-near 0.22.0, and ed25519-dalek, sha2, tokio and serde_json as Rust dev dependencies only.
- New in the window: both contracts, the test token, the rules, the label store, the incidents, the EVM leg, the MCP server, `scan`, the fixtures, the agent script, the tests, the scripts and the docs.

AI coding tools wrote most of the code under the authors' direction; every test was run by the authors.

License: Apache-2.0 (`LICENSE`). Reproducible build: `res/gate.wasm` and `res/registry.wasm` were built with `cargo near build reproducible-wasm` from source commit `eb54c8e`, in the image `sourcescan/cargo-near:0.21.1-rust-1.96.0`, which compiles with Rust 1.96.0 (`rust-toolchain.toml` pins 1.99.1 for local builds, so a local non reproducible build produces a different hash). The recipe to rebuild them is in `res/BUILD.md`.

Business model: a metered verdict and label API for agent platforms; the approver seat in agent wallets; alerts later. No token.

Static pages: `demo/site/`, written by `python -m intel render` from the committed files. Hosted copy: linked here on 13 Nov.

## Environment and working agreements

Every variable the code and the scripts read is in `env.example`, with a comment each; the agreements for working in this repository are in `AGENTS.md`.

Data sources: NearBlocks (testnet explorer and account data, free tier, personal use, attribution), FastNEAR (transfers, transactions, account views), mainnet.neardata.xyz (blocks), GoPlus, 1Click.
