# MCP server

`python -m mcp.server`, run from the repository root, is a stdio MCP server with three tools. It uses the Python standard library only, except live signing, which imports PyNaCl through `gate/signer.py`. Messages are JSON-RPC 2.0, one per line on stdin and stdout; a client that sends `Content-Length` framed messages gets framed answers. Only JSON-RPC frames go to stdout; the server's own lines go to stderr.

The server replays recorded fixtures unless `GATE_FIXTURES_MODE` says otherwise (`record` or `off` opens the network for the read only calls the rules make).

Run it from the repository root: Python puts the current directory first on the import path for `python -m`, so the repository's `mcp` package is the one that loads even when a package of the same name is installed.

## Tools

Each answer is `{"content": [{"type": "text", "text": <JSON>}], "isError": false}`. A call that ran and failed (an invalid account id, an amount with a leading zero, a recipient that is not an EVM address) answers `isError: true` with the reason as the text. An unknown tool name is the JSON-RPC error `-32602`; an unknown method is `-32601`.

| Tool | Arguments | The JSON it returns |
|---|---|---|
| `check_near_payment` | `kind` (`near` or `ft`), `token` (for `ft`), `receiver`, `amount` (base units, decimal), `memo` | `outcome` (`ALLOW`, `DENY` or `UNKNOWN`), `action` (`sign`, `refuse` or `ask`), `ask` (the reasons a human decides; empty unless `action` is `ask`), `verdict` (the signed Verdict object on ALLOW, else null), `request`, `checks` (each with `id`, `status`, `detail`, `source`), `reasons`, `evidence_hash`, `evidence_path`, `latency_ms`, `message` (the canonical message on ALLOW), `gate`, `signer` (`kind` `TEST` or `live`, `key_id`, `public_key` as `ed25519:<base58>`, `public_key_hex`, and `note` for TEST only), `agent_pk` (the agent key in the message, hex), `policy_source` (`GATE_POLICY` or `get_agent`) |
| `check_intents_quote` | `origin_asset`, `destination_asset`, `amount`, `recipient`, `recipient_type` (default `DESTINATION_CHAIN`) | `verdict`, `checks`, `reasons`, `quote` (the dry quote), `evidence_hash`; unsigned, nothing is deposited |
| `scan_near_account` | `account_id` | `findings` (the key lists, the verifier keys, the labels, and the reason for each read that could not run) and `lines` (the text `python -m gate scan` prints) |

By default `check_near_payment` binds the verdict to the TEST agent key, judges against `GATE_POLICY` and signs an ALLOW with the TEST verifier key. That key is derived from a published seed and is never registered on testnet, so the testnet Gate does not pay on such a verdict. Unknown is never a pass.

The tool signs with the live verifier key only when the operator sets all five conditions: `GATE_LIVE_SIGNING=1`, `VERIFIER_KEY_FILE` set, `GATE_FIXTURES_MODE=off`, `GATE_ACCOUNT_ID` ending `.testnet`, and `GATE_POLICY` unset. `AGENT_PUB` must then hold the agent's public key as `ed25519:<base58>`: the verdict is bound to that key and judged against the Gate's `get_agent` for it, read live at every call, so the request and the verdict are the two arguments `request_transfer` takes. With the flag set and any condition missing, or the agent key not registered, or `get_agent` unreadable, the tool answers `isError: true` with the reason, ending "nothing was signed"; it never falls back to the TEST key. Without the flag, a set `VERIFIER_KEY_FILE` changes nothing: the TEST key signs. On a live ALLOW the server prints one stderr line with the Gate, the agent public key and the verifier public key. The live signer needs PyNaCl, so launch the server with the interpreter that has it (`~/.venvs/near/bin/python -m mcp.server`).

The server has no caller authentication: whoever can start it and call its tools can obtain verdicts for the configured agent, so the operator controls access to it. Each live ALLOW is bounded by the Gate's caps and the 5 minute expiry. The evidence document of a live verdict is written to the temporary directory the server names on stderr, which the server removes when it exits unless `GATE_KEEP_EVIDENCE=1`; to record the verdict in the registry, the operator sets `GATE_KEEP_EVIDENCE=1` or copies the document before the server exits.

## Environment

- `GATE_FIXTURES_MODE`: `replay` (default), `record` or `off`.
- `GATE_ACCOUNT_ID`: the Gate the payment verdict is bound to; default `gate.near`. A `.testnet` id selects the testnet RPC, NearBlocks and archival RPC endpoints.
- `GATE_POLICY`: the agent policy as JSON in the `get_agent` shape; default `{"allowlist": [], "caps": []}`, so every receiver also gets the checks for a payee off the allowlist. Must be unset for live signing.
- `GATE_LIVE_SIGNING`: `1` turns on live signing, which then needs every condition in the signing paragraph; any other value leaves the TEST key.
- `VERIFIER_KEY_FILE`: the live verifier key file (near-cli shape), kept outside the repository; read only with `GATE_LIVE_SIGNING=1`.
- `GATE_KEEP_EVIDENCE`: `1` keeps the evidence directory when the server exits; any other value, or none, removes it.
- `AGENT_PUB`: the agent's function call public key as `ed25519:<base58>`, the key the live verdict is bound to and `get_agent` is read for; the public key only, never the agent's secret.
- `NEAR_RPC_URL`, `NEAR_MAINNET_RPC_URL`, `NEARBLOCKS_API_URL`, `NEAR_ARCHIVAL_RPC_URL`: endpoint overrides.
- `SHIELDBOT_API_URL`: the ShieldBot API for the memo and firewall checks; empty means off.

The payment tool writes its evidence documents into a temporary directory that the server names on stderr when it starts and removes when it exits, unless `GATE_KEEP_EVIDENCE=1`.

## Connect a client

The Claude Code command below and the Codex configuration were run on 2 Nov 2026 against the testnet Gate, each with `GATE_FIXTURES_MODE=off`, `GATE_ACCOUNT_ID=<gate>.testnet` and `GATE_POLICY=<get_agent JSON>` in the server's environment. Each client called all three tools once and got `isError: false` from each. The `.mcp.json` file declares the same server in a file; it was not part of that run.

Claude Code, from the repository root:

```
claude mcp add shieldbot-near -s local -e GATE_FIXTURES_MODE=off -e GATE_ACCOUNT_ID=<gate>.testnet -e GATE_POLICY=<get_agent JSON> -- python -m mcp.server
```

or a `.mcp.json` file in the repository root:

```json
{
  "mcpServers": {
    "shieldbot-near": {
      "command": "python",
      "args": ["-m", "mcp.server"]
    }
  }
}
```

Codex, in `~/.codex/config.toml`. `codex mcp add` has no working directory option, so `PYTHONPATH` names the repository root and `python -m mcp.server` finds this package from any directory. With `approval_policy = "never"`, each tool needs its own `approval_mode = "approve"` entry:

```toml
[mcp_servers.shieldbot_near]
command = "python"
args = ["-m", "mcp.server"]

[mcp_servers.shieldbot_near.env]
PYTHONPATH = "/path/to/shieldbot-near"
# for live reads, as in the 2 Nov run:
GATE_FIXTURES_MODE = "off"
GATE_ACCOUNT_ID = "<gate>.testnet"
GATE_POLICY = '<get_agent JSON>'

[mcp_servers.shieldbot_near.tools.check_near_payment]
approval_mode = "approve"

[mcp_servers.shieldbot_near.tools.check_intents_quote]
approval_mode = "approve"

[mcp_servers.shieldbot_near.tools.scan_near_account]
approval_mode = "approve"
```

## Check it by hand

```
printf '%s\n' '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"t","version":"0"}}}' '{"jsonrpc":"2.0","method":"notifications/initialized"}' '{"jsonrpc":"2.0","id":2,"method":"tools/list"}' | python -m mcp.server
```

prints two lines: the `initialize` answer with `"protocolVersion": "2025-06-18"` and `"serverInfo": {"name": "shieldbot-near", "version": "0.1.0"}`, then the three tools. The server answers `protocolVersion` with the version the client asks for when it is `2024-11-05`, `2025-03-26` or `2025-06-18`, and with `2024-11-05` otherwise.
