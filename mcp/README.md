# MCP server

`python -m mcp.server`, run from the repository root, is a stdio MCP server with three tools. It uses the Python standard library only. Messages are JSON-RPC 2.0, one per line on stdin and stdout; a client that sends `Content-Length` framed messages gets framed answers. Only JSON-RPC frames go to stdout; the server's own lines go to stderr.

The server replays recorded fixtures unless `GATE_FIXTURES_MODE` says otherwise (`record` or `off` opens the network for the read only calls the rules make).

Run it from the repository root: Python puts the current directory first on the import path for `python -m`, so the repository's `mcp` package is the one that loads even when a package of the same name is installed.

## Tools

Each answer is `{"content": [{"type": "text", "text": <JSON>}], "isError": false}`. A call that ran and failed (an invalid account id, an amount with a leading zero, a recipient that is not an EVM address) answers `isError: true` with the reason as the text. An unknown tool name is the JSON-RPC error `-32602`; an unknown method is `-32601`.

| Tool | Arguments | The JSON it returns |
|---|---|---|
| `check_near_payment` | `kind` (`near` or `ft`), `token` (for `ft`), `receiver`, `amount` (base units, decimal), `memo` | `outcome` (`ALLOW`, `DENY` or `UNKNOWN`), `verdict` (the signed Verdict object on ALLOW, else null), `request`, `checks` (each with `id`, `status`, `detail`, `source`), `reasons`, `evidence_hash`, `evidence_path`, `latency_ms`, `message` (the canonical message on ALLOW), `gate`, `signer` |
| `check_intents_quote` | `origin_asset`, `destination_asset`, `amount`, `recipient`, `recipient_type` (default `DESTINATION_CHAIN`) | `verdict`, `checks`, `reasons`, `quote` (the dry quote), `evidence_hash`; unsigned, nothing is deposited |
| `scan_near_account` | `account_id` | `findings` (the key lists, the verifier keys, the labels, and the reason for each read that could not run) and `lines` (the text `python -m gate scan` prints) |

`check_near_payment` signs an ALLOW with the TEST verifier key. That key is derived from a published seed and is never registered on testnet, so the testnet Gate does not pay on a verdict from this tool. Unknown is never a pass.

## Environment

- `GATE_FIXTURES_MODE`: `replay` (default), `record` or `off`.
- `GATE_ACCOUNT_ID`: the Gate the payment verdict is bound to; default `gate.near`. A `.testnet` id selects the testnet RPC, NearBlocks and archival RPC endpoints.
- `GATE_POLICY`: the agent policy as JSON in the `get_agent` shape; default `{"allowlist": [], "caps": []}`, so every receiver also gets the checks for a payee off the allowlist.
- `NEAR_RPC_URL`, `NEAR_MAINNET_RPC_URL`, `NEARBLOCKS_API_URL`, `NEAR_ARCHIVAL_RPC_URL`: endpoint overrides.
- `SHIELDBOT_API_URL`: the ShieldBot API for the memo and firewall checks; empty means off.

The payment tool writes its evidence documents into a temporary directory that the server names on stderr when it starts.

## Connect a client

The Claude Code command and the Codex config below were run on 2 Nov 2026 against the testnet Gate, each with `GATE_FIXTURES_MODE=off`, `GATE_ACCOUNT_ID=<gate>.testnet` and `GATE_POLICY=<get_agent JSON>` in the server's environment: each client called the three tools once and got `isError: false` from each. The `.mcp.json` file is the same server declared in a file; it was not part of that run.

Claude Code, from the repository root (add `-e GATE_FIXTURES_MODE=off -e GATE_ACCOUNT_ID=<gate>.testnet -e GATE_POLICY=<get_agent JSON>` before `--` for live reads):

```
claude mcp add shieldbot-near -- python -m mcp.server
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

Codex, in `~/.codex/config.toml`. `codex mcp add` has no working directory option, so `PYTHONPATH` names the repository root and `python -m mcp.server` finds this package from any directory. With `approval_policy = "never"` a tool call fails with "MCP tool call requires approval" unless the tool is approved in the config:

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
