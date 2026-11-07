# API

## The verdict endpoint

`python -m gate http` serves `gate.verdict.request` over HTTP from `gate/http.py`, on the Python standard library server. It is local: it binds `127.0.0.1` by default (`--host`, `--port`, default port 8788) and speaks plain HTTP. It is not hosted anywhere and it is not paid. TLS is the job of a reverse proxy in front of it; the process itself has no certificate.

### The unsigned rule

The endpoint never signs. It reads no signing key, builds no verdict signer and calls `gate.verdict.request` with `signer=None` and `sign_actions=()`, so no outcome, not even an ALLOW whose action is `sign`, carries a Verdict object. Every answer has `verdict` and `message` null and the field `signing` with the text "this endpoint does not sign; a signed verdict needs the verifier key, which is never on this server". A Gate pays only on a signed ALLOW, so an answer from this endpoint alone pays nothing: a verdict a Gate pays on still comes from the operator's verdict service holding the live key (README, Integrate).

### Keys and rate

`SHIELDBOT_API_KEYS_FILE` names a file of API keys, one per line; blank lines and lines starting with `#` are skipped, and each line is stripped of surrounding whitespace. Without that variable, with a file that cannot be read or with a file that holds no key, the server prints `not started: ` and the reason and exits 2: there is no open mode. A caller sends `Authorization: Bearer <key>`; SHA-256 digests of the presented key and every key in the file are compared with `hmac.compare_digest`.

Each key gets `SHIELDBOT_API_RATE` requests a minute (default 60, at least 1), counted in the process in fixed 60 second windows; the count starts again when the process restarts. A request past the budget is answered 429 with `Retry-After` in whole seconds until the next window. Each connection has a 10 second socket timeout. At most eight requests are in flight; excess requests are answered 503 without reading their bodies. `GET /health` needs no key and is not counted.

### Mode

By default the endpoint judges over the recorded fixtures (`GATE_FIXTURES_MODE` unset or `replay`), the way the MCP server's `check_near_payment` does offline: bound to `GATE_ACCOUNT_ID` (default `gate.near`) and the TEST agent key, against `GATE_POLICY` (JSON in the `get_agent` shape; default the MCP server's `DEFAULT_POLICY`, no allowlist and no caps). Live reads need `GATE_FIXTURES_MODE=off` and a `GATE_ACCOUNT_ID` ending `.testnet`; they are still unsigned. `GATE_FIXTURES_MODE=record`, or `off` with any other Gate, refuses to start. There is no mainnet live path. `REPORTS_ACCOUNT_ID`, read at startup, enables the `community_report` check against that contract; when unset, the check is omitted. Evidence documents go to a temporary directory the server names on start, removed when it stops unless `GATE_KEEP_EVIDENCE=1`.

### POST /verdict

The request body is one JSON object of at most 65536 bytes with the fields `check_near_payment` takes, and no others:

| Field | Value |
|---|---|
| `kind` | `near` or `ft` |
| `token` | the NEP-141 token account, for kind `ft` |
| `receiver` | the receiving NEAR account id |
| `amount` | the amount in base units, as a decimal string |
| `memo` | the transfer memo, or absent |

```
curl -s http://127.0.0.1:8788/verdict -H "Authorization: Bearer $KEY" \
  -d '{"kind": "ft", "token": "wrap.near", "receiver": "intents.near", "amount": "1000", "memo": "invoice 7"}'
```

The answer is the `gate.verdict.request` result:

| Field | Value |
|---|---|
| `outcome` | `ALLOW`, `DENY` or `UNKNOWN` |
| `action` | `sign` for an ALLOW the Gate would not refuse on the visible policy checks, `refuse` for DENY, `ask` for UNKNOWN or an ALLOW the Gate would refuse |
| `ask` | the reasons a human must decide, as strings |
| `verdict` | always null |
| `request` | the TransferRequest: `kind`, `token_id`, `receiver_id`, `amount`, `memo` |
| `checks` | one object per check, as `gate/evidence.py` defines it: `id`, `status` (`pass`, `signal` or `unknown`), `detail`, and `source` (`provider`, `request_sha256`, `fetched_at`) |
| `reasons` | the reasons for the outcome, as strings |
| `evidence_hash` | the sha256 hex of the evidence document |
| `latency_ms` | the time the verdict took |
| `message` | always null |
| `signing` | the unsigned rule above |

An ALLOW over the recorded fixtures, with `intents.near` allowlisted and checks cut to one (the hashes shortened):

```
{"outcome": "ALLOW", "action": "sign", "ask": [], "verdict": null,
 "request": {"kind": "ft", "token_id": "wrap.near", "receiver_id": "intents.near", "amount": "1000", "memo": "invoice 7"},
 "checks": [{"id": "receiver_exists", "status": "pass", "detail": "intents.near exists",
             "source": {"provider": "free.rpc.fastnear.com", "request_sha256": "aeca5be4...", "fetched_at": "2026-10-29T09:20:50Z"}}],
 "reasons": ["Every check ran and none found a signal."], "evidence_hash": "56f5882f...",
 "latency_ms": 1, "message": null,
 "signing": "this endpoint does not sign; a signed verdict needs the verifier key, which is never on this server"}
```

### GET /health

`{"ok": true, "mode": "replay"}`, or `"mode": "live"` with live reads; no key needed.

### Errors

Every answer is JSON with `Cache-Control: no-store`; an error is `{"error": "<text>"}`.

| Status | When |
|---|---|
| 400 | the body is not JSON, not an object, has a field other than the five, or holds a transfer `gate/canon.py` refuses (`invalid request: ` and the `CanonError` text); a `Content-Length` that is not a byte count or is longer than 10 digits |
| 401 | no `Authorization: Bearer` header, or a key not in the file (with `WWW-Authenticate: Bearer`) |
| 404 | `GET` or `POST` to any path but `/verdict` and `/health` |
| 405 | any method other than `GET` and `POST` (with `Allow: GET, POST`), or `GET`/`POST` on a known path that does not accept it (with the route's `Allow`) |
| 411 | no `Content-Length` |
| 413 | a `Content-Length` over 65536, answered before the body is read |
| 429 | the key is past its budget for the minute (with `Retry-After`) |
| 500 | the verdict failed for another reason; the body is `{"error": "internal error"}`, and the exception type and text go to stderr, never a traceback in the body |
| 503 | all eight request slots are occupied; the request body is not read |

Unsupported methods are rejected before path, in-flight capacity and key checks. Supported requests then hit the in-flight cap, path, route method and, for `POST /verdict`, key, budget, `Content-Length` and body. The tests are in `tests/test_http.py`.
