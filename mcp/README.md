# MCP server

`python -m mcp.server`, run from the repository root, is a stdio MCP server with six listed tools, three of which require `SIYAB_CHECKS=1`. It uses the Python standard library only, except live signing, which imports PyNaCl through `gate/signer.py`. Messages are JSON-RPC 2.0, one per line on stdin and stdout; a client that sends `Content-Length` framed messages gets framed answers. Only JSON-RPC frames go to stdout; the server's own lines go to stderr.

The server replays recorded fixtures unless `GATE_FIXTURES_MODE` says otherwise (`record` or `off` opens the network for the read only calls the rules make).

Run it from the repository root: Python puts the current directory first on the import path for `python -m`, so the repository's `mcp` package is the one that loads even when a package of the same name is installed.

## Tools

Each successful answer includes `{"type": "text", "text": <JSON>}` in `content` and sets `isError: false`. The three unsigned NEAR checks also prepend a text result: "No flag found", "Flagged" or "Not enough data" for stored `outcome` ALLOW, DENY or ASK. An ASK whose checks include `account_age` with status `signal` reads "New account, confirm first" instead. Only a rules version 3 judgement produces that, and no MCP argument selects version 3, so the shipped tools never print it (`mcp/server.py:356-360`; `gate/account_check.py:148-149`; `gate/rules.py:1013-1050`). Account and token answers in replay mode plainly identify recorded fixtures (replay) and the recorded block; they are not live results. A call that ran and failed (an invalid account id, an amount with a leading zero, a recipient that is not an EVM address, a quote request that differs from the judged one) answers `isError: true` with the reason as the text. An UNKNOWN verdict is not an error: it answers `isError: false` with its checks, reasons and evidence hash. An unknown tool name is the JSON-RPC error `-32602`; an unknown method is `-32601`.

| Tool | Arguments | The JSON it returns |
|---|---|---|
| `check_near_payment` | `kind` (`near` or `ft`), `token` (for `ft`), `receiver`, `amount` (base units, decimal), `memo` | `outcome` (`ALLOW`, `DENY` or `UNKNOWN`), `action` (`sign`, `refuse` or `ask`), `ask` (the reasons a human decides; empty unless `action` is `ask`), `verdict` (the signed Verdict object on ALLOW, else null), `request`, `checks` (each with `id`, `status`, `detail`, `source`), `reasons`, `evidence_hash`, `evidence_path`, `latency_ms`, `message` (the canonical message on ALLOW), `gate`, `signer` (`kind` `TEST` or `live`, `key_id`, `public_key` as `ed25519:<base58>`, `public_key_hex`, and `note` for TEST only), `agent_pk` (the agent key in the message, hex), `policy_source` (`GATE_POLICY` or `get_agent`) |
| `check_intents_quote` | `origin_asset`, `destination_asset`, `amount`, `recipient`, `recipient_type` (default `DESTINATION_CHAIN`), `deposit_address` (optional, from a real quote), `refund_to` (that quote's refund account; required with `deposit_address`, default `intents.near`), `refund_type` (default `ORIGIN_CHAIN`), `app_fees` (that quote's app fees, each `{recipient, fee}`; none when left out) | `verdict` (`ALLOW`, `DENY` or `UNKNOWN`), `checks`, `reasons`, `quote` (the dry quote), `document` (the evidence document, with the judged quote request), `evidence_hash`, `deposit_address_check`; unsigned, nothing is deposited |
| `scan_near_account` | `account_id` | `findings` (the key lists, the verifier keys, the labels, and the reason for each read that could not run) and `lines` (the text `python -m gate scan` prints) |
| `check_near_account` | `account_id` (mainnet) | Unsigned account checks and evidence; requires `SIYAB_CHECKS=1` |
| `check_near_token` | `token_id` (mainnet) | Unsigned token checks and evidence; requires `SIYAB_CHECKS=1` |
| `check_near_transaction` | `transaction` with `signer_id`, `receiver_id`, and a nonempty `actions` array in NEAR RPC shape | Offline proposed-action checks and evidence; requires `SIYAB_CHECKS=1`; "No flag found" means "no modelled risk found" |

The three checks return the ten keys `outcome`, `action`, `reasons`, `checks`, `evidence_hash`, `freshness`, `verdict`, `signing`, `latency_ms` and `evidence`. They always return `verdict: null` and `signing: "never"`, never call the live signer, and do not submit a transaction. For `check_near_account`, `check_near_token` and `check_near_transaction`, an answer with `"signing": "never"` and `"verdict": null` keeps `"action": "sign"` on ALLOW. The action field is advice only and never permission to pay; only a signed Gate verdict authorises payment. Unknown or partial data gives ASK unless a signal gives DENY. The tools remain listed when disabled, but calls return `isError: true` with a clear disabled message. (`mcp/server.py:_require_checks`; `mcp/server.py:_check_near_account`; `mcp/server.py:_check_near_token`; `mcp/server.py:_check_near_transaction`; `mcp/server.py:handle`)

MCP account checks use fixed `rpc.MAINNET_RPC_URL`, `rpc.NEARBLOCKS_URL` and `rpc.ARCHIVAL_RPC_URL`, ignoring `NEAR_MAINNET_RPC_URL`, `NEARBLOCKS_API_URL` and `NEAR_ARCHIVAL_RPC_URL`. MCP token checks always use the fixed `rpc.MAINNET_RPC_URL` endpoint and ignore `NEAR_MAINNET_RPC_URL`; both inherit `GATE_FIXTURES_MODE`. (`mcp/server.py:_check_near_account`; `mcp/server.py:_check_near_token`)

The corresponding live routes are `GET /live/account-check/<account>` and `GET /live/token-check/<token>`, both 404 unless `SIYAB_CHECKS=1`, with the boolean exposed as `checks` on `/live/health`. Both use fixture mode `off`. The live account check honors `NEAR_MAINNET_RPC_URL` and `NEAR_ARCHIVAL_RPC_URL`; NearBlocks is disabled unless `SIYAB_VERDICT_NEARBLOCKS=1`, then honors `NEARBLOCKS_API_URL`. The live token check always uses fixed `rpc.MAINNET_RPC_URL` and ignores `NEAR_MAINNET_RPC_URL`, preserving the token check's endpoint restriction. The transaction check is MCP only. (`intel/live.py:handle`; `intel/live.py:_route`)

By default `check_near_payment` binds the verdict to the TEST agent key, judges against `GATE_POLICY` and signs an ALLOW with the TEST verifier key. Under the default policy (no unknown payee caps), every ALLOW for a payee off the allowlist answers `action: ask` with `REFUSED:not_allowlisted`. The TEST key is derived from a published seed and is never registered on testnet, so the testnet Gate does not pay on such a verdict. Unknown is never a pass.

The tool signs with the live verifier key only when the operator sets all five conditions: `GATE_LIVE_SIGNING=1`, `VERIFIER_KEY_FILE` set, `GATE_FIXTURES_MODE=off`, `GATE_ACCOUNT_ID` ending `.testnet`, and `GATE_POLICY` unset. `AGENT_PUB` must then hold the agent's public key as `ed25519:<base58>`: the verdict is bound to that key and judged against the Gate's `get_agent` for it, read live at every call, so the request and the verdict are the two arguments `request_transfer` takes. With the flag set and any condition missing, or the agent key not registered, or `get_agent` unreadable, the tool answers `isError: true` with the reason, ending "nothing was signed"; it never falls back to the TEST key. Without the flag, a set `VERIFIER_KEY_FILE` changes nothing: the TEST key signs. On a live ALLOW the server prints one stderr line with the Gate, the agent public key and the verifier public key. The live signer needs PyNaCl, so launch the server with the interpreter that has it (`~/.venvs/near/bin/python -m mcp.server`).

The server has no caller authentication: whoever can start it and call its tools can obtain verdicts for the configured agent, so the operator controls access to it. Each live ALLOW is bounded by the Gate's caps and the 5 minute expiry. The evidence document of a live verdict is written to the temporary directory the server names on stderr, which the server removes when it exits unless `GATE_KEEP_EVIDENCE=1`; to record the verdict in the registry, the operator sets `GATE_KEEP_EVIDENCE=1` or copies the document before the server exits.

## Environment

- `GATE_FIXTURES_MODE`: `replay` (default), `record` or `off`. Account and token checks in replay mode label their recorded fixture result and recorded block.
- `SIYAB_CHECKS`: `1` enables `check_near_account`, `check_near_token` and `check_near_transaction`; off by default. Disabled calls refuse without signing.
- `GATE_ACCOUNT_ID`: the Gate the payment verdict is bound to; default `gate.near`. A `.testnet` id selects the testnet RPC, NearBlocks and archival RPC endpoints.
- `REPORTS_ACCOUNT_ID`: the reports contract whose `subject_summary` is checked for the payee; unset disables community report checks.
- `GATE_POLICY`: the agent policy as JSON in the `get_agent` shape; default `{"allowlist": [], "caps": []}`, so every receiver also gets the checks for a payee off the allowlist. Must be unset for live signing.
- `GATE_LIVE_SIGNING`: `1` turns on live signing, which then needs every condition in the signing paragraph; any other value leaves the TEST key.
- `VERIFIER_KEY_FILE`: the live verifier key file (near-cli shape), kept outside the repository; read only with `GATE_LIVE_SIGNING=1`.
- `GATE_KEEP_EVIDENCE`: `1` keeps the evidence directory when the server exits; any other value, or none, removes it.
- `AGENT_PUB`: the agent's function call public key as `ed25519:<base58>`, the key the live verdict is bound to and `get_agent` is read for; the public key only, never the agent's secret.
- `NEAR_RPC_URL`, `NEAR_MAINNET_RPC_URL`, `NEARBLOCKS_API_URL`, `NEAR_ARCHIVAL_RPC_URL`: endpoint overrides for existing payment and scan clients; the new account and token checks use the fixed endpoints described above.
- `SHIELDBOT_API_URL`: the ShieldBot API for the memo and firewall checks; empty means off.

The payment tool writes its evidence documents into a temporary directory that the server names on stderr when it starts and removes when it exits, unless `GATE_KEEP_EVIDENCE=1`.

## Connect a client

The Claude Code command below and the Codex configuration were run on 2 Nov 2026 against the testnet Gate, each with `GATE_FIXTURES_MODE=off`, `GATE_ACCOUNT_ID=<gate>.testnet` and `GATE_POLICY=<get_agent JSON>` in the server's environment. Each client called all three tools once and got `isError: false` from each. The JSON snippet below was not part of that run; users can save it as `.mcp.json` in their repository root.

Claude Code, from the repository root:

```
claude mcp add siyab -s local -e GATE_FIXTURES_MODE=off -e GATE_ACCOUNT_ID=<gate>.testnet -e GATE_POLICY=<get_agent JSON> -- python -m mcp.server
```

Alternatively, save this snippet as `.mcp.json` in the repository root:

```json
{
  "mcpServers": {
    "siyab": {
      "command": "python",
      "args": ["-m", "mcp.server"]
    }
  }
}
```

Codex, in `~/.codex/config.toml`. `codex mcp add` has no working directory option, so `PYTHONPATH` names the repository root and `python -m mcp.server` finds this package from any directory. With `approval_policy = "never"`, each tool needs its own `approval_mode = "approve"` entry:

```toml
[mcp_servers.siyab]
command = "python"
args = ["-m", "mcp.server"]

[mcp_servers.siyab.env]
PYTHONPATH = "/path/to/shieldbot-near"
# for live reads, as in the 2 Nov run:
GATE_FIXTURES_MODE = "off"
GATE_ACCOUNT_ID = "<gate>.testnet"
GATE_POLICY = '<get_agent JSON>'

[mcp_servers.siyab.tools.check_near_payment]
approval_mode = "approve"

[mcp_servers.siyab.tools.check_intents_quote]
approval_mode = "approve"

[mcp_servers.siyab.tools.scan_near_account]
approval_mode = "approve"
```

## Check it by hand

```
printf '%s\n' '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"t","version":"0"}}}' '{"jsonrpc":"2.0","method":"notifications/initialized"}' '{"jsonrpc":"2.0","id":2,"method":"tools/list"}' | python -m mcp.server
```

prints two lines: the `initialize` answer with `"protocolVersion": "2025-06-18"` and `"serverInfo": {"name": "siyab", "version": "0.1.0"}`, then the six listed tools; the three unsigned NEAR checks refuse calls unless `SIYAB_CHECKS=1`. The server answers `protocolVersion` with the version the client asks for when it is `2024-11-05`, `2025-03-26` or `2025-06-18`, and with `2024-11-05` otherwise.
