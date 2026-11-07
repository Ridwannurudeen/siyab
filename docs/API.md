# API

This file is the consumer contract of the verdict interface: what `gate.verdict.request` returns, which checks can appear in it and what each status means, how `outcome` and `action` relate, a recorded answer for ALLOW and for DENY, and the local HTTP endpoint that serves it.

`tests/test_api_doc.py` checks the response field names and order, exact check ids, `Kind` rows against emitted near/ft check ids, and `Tier` rows against `TIER2_CHECKS` plus allowlisted and off-allowlist ft calls. It checks that `issuer_blacklist` appears only when an issuer table is passed and `community_report` only when `reports_account` is set; for CLI, MCP and HTTP it verifies `issuers_for("mainnet")` for Gate ids not ending `.testnet` and no issuer table for `.testnet`; and it checks the outcome/action rows: ALLOW is `ask` when `ask` is nonempty and otherwise `sign`, DENY is `refuse`, and UNKNOWN is `ask`. It also checks the ALLOW and DENY examples against their fixtures with 64-hex values shortened, and that no receiver judgement records UNKNOWN while the UNKNOWN example section cites no fixture. The meanings in the `pass`, `signal` and `unknown` check-table cells are documentation checked by review, not by a test.

## The verdict response

`gate.verdict.request` (`gate/verdict.py:61`) judges one transfer and returns a dict with exactly these keys, in this order (`gate/verdict.py:134-146`). Every adapter (the CLI `python -m gate verdict`, the MCP tool `check_near_payment`, the HTTP endpoint, the pay wrapper) starts from this dict.

### Fields

| Field | Type | Meaning | Code |
|---|---|---|---|
| `outcome` | string | `ALLOW`, `DENY` or `UNKNOWN`, the verdict of `gate.rules.judge` | `gate/verdict.py:135`, `gate/rules.py:734` |
| `action` | string | `sign`, `ask` or `refuse`: what the caller does with the outcome (see Outcome and action) | `gate/verdict.py:136`, `gate/verdict.py:104-105` |
| `ask` | list of strings | the reasons a human must decide before anything is signed or sent; empty for DENY | `gate/verdict.py:137`, `gate/verdict.py:20-58` |
| `verdict` | object or null | the Verdict object the Gate's `request_transfer` takes: `literal`, `issued_ms`, `expires_ms`, `verdict_id`, `evidence_hash`, `key_id`, `signature`; null unless the outcome is ALLOW and the action is in the caller's `sign_actions` | `gate/verdict.py:138`, `gate/verdict.py:106-109`, `gate/verdict.py:120-133` |
| `request` | object | the TransferRequest judged: `kind`, `token_id`, `receiver_id`, `amount` (the canonical decimal string), `memo` (string or null) | `gate/verdict.py:139`, `gate/verdict.py:96-98` |
| `checks` | list of objects | one object per check that ran, `{id, status, detail, source}`, in the order the rules ran them (see Checks) | `gate/verdict.py:140`, `gate/evidence.py:43` |
| `reasons` | list of strings | the reasons for the outcome | `gate/verdict.py:141`, `gate/rules.py:676-710` |
| `evidence_hash` | string | the sha256 hex of the canonical bytes of the evidence document, which is also its file name | `gate/verdict.py:142`, `gate/verdict.py:117-118` |
| `evidence_path` | string | where the evidence document was written, `<evidence_dir>/<evidence_hash>.json` | `gate/verdict.py:143`, `gate/evidence.py:93-100` |
| `latency_ms` | integer | whole milliseconds from just before the rules run until immediately after the conditional `verdict_id` assignment, following the action and signing decision. An id is drawn only for ALLOW when its action is in `sign_actions`; DENY, UNKNOWN and unsigned ALLOW get no id. The interval excludes request validation before it and evidence-document construction, evidence write and signature afterward. | `gate/verdict.py:144`, `gate/verdict.py:99-110` |
| `message` | string or null | the canonical message the signature covers (`docs/VERDICT-RECORD.md`), or null when `verdict` is null | `gate/verdict.py:145`, `gate/verdict.py:120-124` |

The adapters change the dict in two places only. The HTTP endpoint drops `evidence_path`, sets `verdict` and `message` to null and adds `signing` (`gate/http.py:163-164`). The MCP tool adds `gate`, `signer`, `agent_pk` and `policy_source` (`mcp/server.py:179`).

### Checks

Each check is `{"id", "status", "detail", "source"}` (`gate/evidence.py:43-47`). `status` is `pass`, `signal` or `unknown` (`gate/evidence.py:27`); `evidence.check` raises on any other value, and there is no `not_run` status: a check that did not run is absent from `checks`. `detail` is text for a human. `source` is `{"provider", "request_sha256", "fetched_at"}` (`docs/VERDICT-RECORD.md`): the RPC or HTTP host with the replay key of the request and its fetch time, or for a local read a local provider name (`label store`, `local patterns`, `allowlist and label store`, or `nearblocks` when no request was made) with `fetched_at` null (`gate/rules.py:110-116`).

Tiers (`gate/rules.py:713-730`): tier 1 runs for every receiver; tier 2 runs only for a receiver that is not on the agent's `allowlist` in the policy (`TIER2_CHECKS`, `gate/rules.py:37`). Kind `near` runs no token check; kind `ft` adds the three token checks and also looks the token up in the label store (`gate/rules.py:344-349`, `gate/rules.py:358-378`).

### Check table

| Check id | Kind | Tier | Enabled by | Network | pass | signal | unknown |
|---|---|---|---|---|---|---|---|
| `receiver_exists` | near, ft | 1 | always | any | `view_account` answered (`gate/rules.py:331-332`) | the RPC answers `UNKNOWN_ACCOUNT`: the receiver does not exist (`gate/rules.py:334-337`) | any other RPC error, or a read that could not run (`gate/rules.py:333-340`) |
| `label_denylist` | near, ft | 1 | always | any | no current `denylist` or `incident` label on the receiver, or on the token for ft (`gate/rules.py:351-353`) | a current `denylist` or `incident` label on the receiver or, for ft, the token (`gate/rules.py:345-349`) | never: the label store is local |
| `issuer_blacklist` | near, ft | 1 | the caller passes the issuer table of `intel/issuers.json` (`gate/rules.py:355-356`) | mainnet Gate only: the CLI, the MCP tool and the HTTP endpoint pass `issuers_for("mainnet")` for a Gate not ending `.testnet` (`gate/__main__.py:70`, `mcp/server.py:166`, `gate/http.py:150`), and the testnet table is empty | every issuer view answers a value other than its banned one, or the token has no such view (`gate/rules.py:305-314`) | an issuer's view answers its banned value for the receiver (`gate/rules.py:305-309`) | a read could not run and no issuer answered banned (`gate/rules.py:311-317`) |
| `token_in_catalog` | ft | 1 | always for ft | any | the token is a cap asset of the agent or a `catalog_token` in the label store (`gate/rules.py:358-363`) | neither (`gate/rules.py:364-366`) | never: the policy and the label store are local |
| `ft_metadata_sane` | ft | 1 | always for ft | any | `ft_metadata` has a non empty name and symbol and decimals from 0 to 24 (`gate/rules.py:408-415`) | the token has no code or no `ft_metadata` method, or the metadata fails that shape (`gate/rules.py:400-413`) | any other RPC error, or a read that could not run (`gate/rules.py:400-407`) |
| `storage_registered` | ft | 1 | always for ft | any | `storage_balance_of` for the receiver is not null (`gate/rules.py:369-375`) | `storage_balance_of` is null: the receiver is not registered on the token (`gate/rules.py:370-372`) | the read could not run (`gate/rules.py:376-378`) |
| `memo_injection` | near, ft | 1 | always; `SHIELDBOT_API_URL` adds the ShieldBot injection scan to the local patterns (`gate/__main__.py:48-49`) | any | no memo, or no local pattern matched and the scan, when set, answered clean (`gate/rules.py:420-440`) | a local pattern matched, or the scan flagged the memo (`gate/rules.py:422-439`) | only with the scan set: the scan could not run and no local pattern matched (`gate/rules.py:433-440`) |
| `community_report` | near, ft | 1 | `REPORTS_ACCOUNT_ID` names the reports contract (`gate/http.py:102`, `gate/__main__.py:71`, `mcp/server.py:159`); unset, the check is absent (`gate/rules.py:382-393`) | any | no report, only pending reports, or accepted social reports for a payee on the allowlist (`gate/rules.py:382-393`, `intel/reports.py:45-48`, `intel/reports.py:51-54`) | an accepted deny report (`intel/reports.py:39-42`) | accepted social reports for a payee off the allowlist, so a human decides; or the summary is unreadable or `subject_summary` could not be read (`gate/rules.py:382-393`, `intel/reports.py:35-50`) |
| `account_age` | near, ft | 2 | always for tier 2; when NearBlocks gives no creation time, the oldest access key nonce is dated on the archival RPC (`gate/rules.py:576-578`); a receiver with no access key or none with a creation height, such as a locked contract, is instead read with `view_account` on the archival RPC at the block `7 * BLOCKS_PER_DAY_MAX` blocks before the block its own `view_account` was read at (`BLOCKS_PER_DAY_MAX`, `gate/rules.py:46`, `gate/rules.py:518-523`, `gate/rules.py:466-472`) | any | the oldest key was created at least 7 days ago, the oldest nonce predates block height nonces or dates to a block at least 7 days old, or `view_account` finds the receiver at a block at least 7 days old (`gate/rules.py:568-571`, `gate/rules.py:526-528`, `gate/rules.py:540-541`, `gate/rules.py:491-492`) | the account is younger than 7 days by its NearBlocks creation time or its oldest nonce (`MIN_AGE_DAYS`, `gate/rules.py:35`, `gate/rules.py:568-571`, `gate/rules.py:537-539`); the `view_account` read never gives a signal | no creation time could be read from NearBlocks or the archival fallbacks; the block counted back to is younger than 7 days; or the receiver did not exist at that block, which shows only that it was created after the block (`gate/rules.py:556-562`, `gate/rules.py:572-575`, `gate/rules.py:514-516`, `gate/rules.py:532-534`, `gate/rules.py:466-490`) |
| `dust_funded` | near, ft | 2 | always for tier 2 | any | the creation deposit is at least 0.1 NEAR, or is below it with no lookalike or account_age signal, or the access key nonce or `view_account` age passed with no lookalike so no deposit can change the verdict (`gate/rules.py:611-630`) | the creation deposit is below 0.1 NEAR together with a lookalike or account_age signal (`DUST_YOCTO`, `gate/rules.py:40`, `gate/rules.py:622-624`) | the creation transaction is not known or could not be read (`gate/rules.py:583-595`) |
| `lookalike` | near, ft | 2 | always for tier 2 | any | no allowlisted name is contained and no allowlisted or labelled reference account is within the length scaled distance, or the receiver is itself a labelled reference account (`gate/rules.py:597-609`) | the receiver contains an allowlisted name, or is within the distance of an allowlisted or labelled reference account after confusable normalisation (`gate/rules.py:223-237`, `gate/rules.py:597-602`) | never: the allowlist and the label store are local |
| `code_without_metadata` | near, ft | 2 | always for tier 2 | any | no contract code, code with `contract_source_metadata`, or code without it and no other tier 2 signal (`gate/rules.py:641-657`) | contract code without `contract_source_metadata` together with another tier 2 signal (`gate/rules.py:650-657`, `gate/rules.py:633-636`) | the receiver account could not be read, or `contract_source_metadata` could not be read (`gate/rules.py:641-643`, `gate/rules.py:658-660`) |

Line numbers without a file are `gate/rules.py`. The test compares the table's check ids with the literal first arguments of `evidence.check` calls in `gate/rules.py`. It does not validate the prose in the `pass`, `signal` and `unknown` columns; those descriptions are checked by review.

### Outcome and action

`outcome` comes from the statuses alone (`gate/rules.py:733-734`): DENY when any check is `signal`, else UNKNOWN when any check is `unknown`, else ALLOW. A signal wins over a check that could not run.

`action` comes from `outcome` and `ask` (`gate/verdict.py:103-105`):

| `outcome` | `ask` | `action` |
|---|---|---|
| `DENY` | always empty (`gate/verdict.py:28-29`) | `refuse` |
| `UNKNOWN` | each check that could not run, as `<id> could not run: <detail>`, then any Gate policy refusal below (`gate/verdict.py:38-40`) | `ask` |
| `ALLOW` | a Gate policy refusal the verdict service can see: the Gate is paused, the agent is disabled, the receiver is on the Gate denylist, or the receiver is off the allowlist with no unknown payee caps, no cap for the asset, or an amount over that cap's per call limit (`gate/verdict.py:32-58`) | `ask` when `ask` is not empty, else `sign` |

`sign_actions` decides only which actions are signed, never the action itself (`gate/verdict.py:61-63`, `gate/verdict.py:104-106`): the default `("sign",)` signs an ALLOW whose action is `sign`; ordinary pay leaves that default in place (`gate/agent.py:208-225`, `gate/agent.py:286-361`), and the HTTP endpoint passes `()` (`gate/http.py:160`). DENY and UNKNOWN are never signed. The Gate itself still checks the window part of every cap, the caps of an allowlisted payee, and the verdict signature, expiry and replay (`contracts/gate/src/lib.rs:194-210,227-237`; `contracts/gate/src/verdict.rs:66-95`).

Adapters translate `action` into their host's vocabulary (pay or not, a tool result, an HTTP body) and never decide again: `mcp/server.py:178`, `gate/http.py:163-164` and `gate/agent.py:311` read `result["action"]` as it came back.

## Examples

One recorded answer for ALLOW and one for DENY, from committed fixtures; UNKNOWN has none (below). Each block is the fixture's JSON with every 64 hex value shortened to its first and last 8 hex; the fixture holds the full values. `tests/test_api_doc.py` checks that each block equals its fixture after that shortening and that the fixture records the outcome its heading names, and that no receiver judgement records UNKNOWN while the UNKNOWN section cites no fixture.

### ALLOW

Fixture: `fixtures/runs/mcp-live-check_near_payment-20261102T134819Z.json`, the `result` of the live MCP `check_near_payment` call of 2 Nov on testnet, signed with the live verifier key. It was recorded before `action` and `ask` were added to the response (5 Nov), so it lacks them; the same answer today carries `"action": "sign"` and `"ask": []`. Its keys `gate`, `signer`, `agent_pk` and `policy_source` are the MCP tool's (`mcp/server.py:178`). The fixture itself already shortens the signature.

```json
{
 "outcome": "ALLOW",
 "verdict": {"literal": "ALLOW", "issued_ms": 1793627303425, "expires_ms": 1793627603425, "verdict_id": "652c1e0ebd165a413dd0489dea6a75ea", "evidence_hash": "4b99f7ed...fc84bf29", "key_id": "cur", "signature": "7231c2b4...103bd30d"},
 "request": {"kind": "ft", "token_id": "token.kestrelridge-main.testnet", "receiver_id": "kestrelridge-shop.testnet", "amount": "10000000", "memo": "invoice 4"},
 "checks": [
   {"id": "receiver_exists", "status": "pass", "detail": "kestrelridge-shop.testnet exists", "source": {"provider": "test.rpc.fastnear.com", "request_sha256": "91918b33...38f7836c", "fetched_at": "2026-11-02T13:48:22Z"}},
   {"id": "label_denylist", "status": "pass", "detail": "no denylist or incident label on kestrelridge-shop.testnet or token.kestrelridge-main.testnet", "source": {"provider": "label store", "request_sha256": "7e09fcc1...00959782", "fetched_at": null}},
   {"id": "token_in_catalog", "status": "pass", "detail": "token.kestrelridge-main.testnet is a cap asset of the agent", "source": {"provider": "label store", "request_sha256": "7e09fcc1...00959782", "fetched_at": null}},
   {"id": "ft_metadata_sane", "status": "pass", "detail": "symbol \"DMT\", 6 decimals", "source": {"provider": "test.rpc.fastnear.com", "request_sha256": "1f6d0279...79a6e27c", "fetched_at": "2026-11-02T13:48:22Z"}},
   {"id": "storage_registered", "status": "pass", "detail": "kestrelridge-shop.testnet is registered on token.kestrelridge-main.testnet", "source": {"provider": "test.rpc.fastnear.com", "request_sha256": "e0bfb9ac...a9881575", "fetched_at": "2026-11-02T13:48:23Z"}},
   {"id": "memo_injection", "status": "pass", "detail": "local patterns: no match", "source": {"provider": "local patterns", "request_sha256": null, "fetched_at": null}}
 ],
 "reasons": ["Every check ran and none found a signal."],
 "evidence_hash": "4b99f7ed...fc84bf29",
 "evidence_path": "/tmp/shieldbot-near-mcp-s_iuokre/4b99f7ed...fc84bf29.json",
 "latency_ms": 1972,
 "message": "GATEv1|kestrelridge-main.testnet|fc5b49a7...06e5bba9|ft|token.kestrelridge-main.testnet|kestrelridge-shop.testnet|10000000|040ed37c...ca267acd|1793627303425|1793627603425|652c1e0ebd165a413dd0489dea6a75ea|4b99f7ed...fc84bf29|cur|ALLOW",
 "gate": "kestrelridge-main.testnet",
 "signer": {"kind": "live", "key_id": "cur", "public_key": "ed25519:9SVNJQqQjSKFo6LXpSJJXWKEjV65V93mHUsXGhmokyb5", "public_key_hex": "7d651730...172eb230"},
 "agent_pk": "fc5b49a7...06e5bba9",
 "policy_source": "get_agent"
}
```

The receiver is on the allowlist, so only tier 1 ran; the Gate is on testnet, so `issuer_blacklist` is absent; `REPORTS_ACCOUNT_ID` was unset, so `community_report` is absent.

### DENY

Fixture: `fixtures/evidence/c5d02152*.json` (`c5d02152...c94297b7`, also in `docs/EVIDENCE.md`), the evidence document of the testnet lookalike `kestrelridge-sh0p.testnet`, a receiver off the allowlist. An evidence document is not the response: it keys the outcome as `verdict`, keeps the memo only as its hash and has no `action`, `ask` or `request`. Its file name is the response's `evidence_hash`, and the response carried the same `checks` and `reasons` with `"action": "refuse"` and `"ask": []`.

```json
{
 "checks": [
   {"detail": "kestrelridge-sh0p.testnet exists", "id": "receiver_exists", "source": {"fetched_at": "2026-10-29T20:08:57Z", "provider": "test.rpc.fastnear.com", "request_sha256": "5832df73...bf1a70a2"}, "status": "pass"},
   {"detail": "no denylist or incident label on kestrelridge-sh0p.testnet or token.kestrelridge-main.testnet", "id": "label_denylist", "source": {"fetched_at": null, "provider": "label store", "request_sha256": "7e09fcc1...00959782"}, "status": "pass"},
   {"detail": "token.kestrelridge-main.testnet is a cap asset of the agent", "id": "token_in_catalog", "source": {"fetched_at": null, "provider": "label store", "request_sha256": "7e09fcc1...00959782"}, "status": "pass"},
   {"detail": "symbol \"DMT\", 6 decimals", "id": "ft_metadata_sane", "source": {"fetched_at": "2026-10-29T20:08:58Z", "provider": "test.rpc.fastnear.com", "request_sha256": "1f6d0279...79a6e27c"}, "status": "pass"},
   {"detail": "receiver_not_registered: kestrelridge-sh0p.testnet is not registered on token.kestrelridge-main.testnet", "id": "storage_registered", "source": {"fetched_at": "2026-10-29T20:08:59Z", "provider": "test.rpc.fastnear.com", "request_sha256": "f6304cd5...98c23d1f"}, "status": "signal"},
   {"detail": "local patterns: no match", "id": "memo_injection", "source": {"fetched_at": null, "provider": "local patterns", "request_sha256": null}, "status": "pass"},
   {"detail": "no key of kestrelridge-sh0p.testnet carries a creation time", "id": "account_age", "source": {"fetched_at": "2026-10-29T20:09:01Z", "provider": "api-testnet.nearblocks.io", "request_sha256": "0f644f90...ab936275"}, "status": "unknown"},
   {"detail": "the creation transaction is not known", "id": "dust_funded", "source": {"fetched_at": null, "provider": "nearblocks", "request_sha256": null}, "status": "unknown"},
   {"detail": "lookalike of kestrelridge-shop.testnet: first label kestrelridge-sh0p against kestrelridge-shop, distance 0 after confusable normalisation", "id": "lookalike", "source": {"fetched_at": null, "provider": "allowlist and label store", "request_sha256": "7e09fcc1...00959782"}, "status": "signal"},
   {"detail": "no contract code", "id": "code_without_metadata", "source": {"fetched_at": "2026-10-29T20:08:57Z", "provider": "test.rpc.fastnear.com", "request_sha256": "5832df73...bf1a70a2"}, "status": "pass"}
 ],
 "expires_ms": null,
 "issued_ms": null,
 "labels": [],
 "latency_ms": 4712,
 "observed": {"block_height": 275347643, "timestamp_ms": 1793304536793},
 "reasons": ["kestrelridge-sh0p.testnet is a lookalike of kestrelridge-shop.testnet.", "Not on the owner's allowlist.", "Not registered on the token."],
 "subject": {"agent_pk": "fc5b49a7...06e5bba9", "amount": "10000000", "gate": "kestrelridge-main.testnet", "kind": "ft", "memo_sha256": "ca602dac...bb4ac33c", "receiver": "kestrelridge-sh0p.testnet", "token": "token.kestrelridge-main.testnet"},
 "verdict": "DENY",
 "verdict_id": null,
 "version": "1"
}
```

Two checks could not run and two found a signal; the signals win, so the outcome is DENY, not UNKNOWN.

### UNKNOWN

No committed fixture records an UNKNOWN answer or judgement on its own, so this section has no block. The example was `fixtures/receivers/judgements/liver.pool.near.json`, a mainnet staking pool of the recorded receiver set that read UNKNOWN on `account_age` and `dust_funded`, until the archival `view_account` read dated the receivers with no access key (`docs/SPIKES.md` S8); it now records ALLOW, and no judgement under `fixtures/receivers/judgements/` records UNKNOWN. The UNKNOWN judgements left in committed files are `rules_today` entries inside the incident page `intel/incidents/rhea.json`, which is judged without the archival client, not records of their own. An UNKNOWN answer has the fields of The verdict response with `"outcome": "UNKNOWN"`, `"action": "ask"`, an `ask` that names each check that could not run as `<id> could not run: <detail>` (Outcome and action), and `verdict` and `message` null. `tests/test_api_doc.py` fails once a receiver judgement records UNKNOWN again, so that an example is cited from it.

`python -m gate demo --offline` prints an answer of each outcome, an UNKNOWN among them, over the recorded testnet fixtures in `fixtures/demo/`. Its evidence goes to a temporary directory, and its latency, and so its evidence hash, changes from run to run, so it is not cited here.

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

The answer is the `gate.verdict.request` result described in The verdict response, with three changes (`gate/http.py:163-164`): `evidence_path` is removed, `verdict` and `message` are always null, and `signing` is added with the unsigned rule above. `action` is passed through unchanged, so an ALLOW whose action is `sign` still says `sign` although nothing was signed. The default Gate `gate.near` does not end `.testnet`, so the endpoint runs `issuer_blacklist` (`gate/http.py:150`, `gate/rules.py:321`, `gate/rules.py:666`); with `intents.near` allowlisted and a `wrap.near` cap in `GATE_POLICY`, the body above answers ALLOW with the seven tier 1 checks `receiver_exists`, `label_denylist`, `issuer_blacklist`, `token_in_catalog`, `ft_metadata_sane`, `storage_registered` and `memo_injection`. The recorded examples are under Examples.

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

## The quote preflight

`gate.intents.judge_quote`, the CLI `python -m gate intents` and the MCP tool `check_intents_quote` judge where a NEAR Intents 1Click quote sends money on an EVM chain. The MCP answer has the keys `verdict`, `checks`, `reasons`, `quote`, `document`, `evidence_hash` and `deposit_address_check`. It is unsigned on every outcome.

- Dry only. The request body always carries `"dry": true`, and `dry_quote` raises before any request for another value. A well formed answer that echoes `dry: true` passes `quote_dry`, whether or not it contains `depositAddress`. 1Click documents that a dry quote is simulated without generating a deposit address. Nothing can be deposited from that dry quote.
- Verdict binding. The verdict binds to the quote request it judged through `BOUND_FIELDS`: `originAsset`, `destinationAsset`, `amount`, `recipient` and `recipientType`. The answer's `quoteRequest` must echo each field as sent, or `quote_dry` is a signal and the verdict is DENY. The deadline is not compared because 1Click echoes it with milliseconds added. `verify_binding` checks the evidence hash and verdict and compares these five fields with the caller's request. The quote timestamp has a separate age bound of 300000 ms; a missing, invalid, future or older timestamp makes `quote_age` UNKNOWN.
- OFAC sanctions. `ofac_sanctioned` reads the committed local SDN address fixture for the destination recipient and never uses the network. A match is a signal and makes the verdict DENY with the SDN uid, entry name, programs, list date and source XML hash in its reason. A no-match pass records the list date and number of distinct addresses checked for the destination chain code. A chain with no OFAC address code passes with detail that the list publishes no address for that chain; `docs/SANCTIONS.md` lists the mapped codes and the 1Click chains without one. A missing or unreadable fixture is UNKNOWN. Its evidence source names the fixture and carries the hash of its JSON bytes.
- The NEAR payment path (`gate/rules.py`) is unchanged: OFAC publishes no NEAR account.
- Deposit address. A dry quote has no deposit address. Only when the caller supplies the address from a real quote with `--deposit-address` or MCP `deposit_address`, the CLI or MCP tool makes a read only `GET /v0/status?depositAddress=<address>` on the same 1Click base URL. The status answer's `quoteResponse.quoteRequest` must match the judged request on every `BOUND_FIELDS` value. A differing field is refused by name. A 404 means 1Click does not know the address and is refused. A failed read, including a network error, 401 or 5xx, is UNKNOWN with its reason and is refused. No other endpoint is called, and nothing is signed, deposited or submitted.
- Without a supplied address, the CLI says the address was not checked because dry quotes have no deposit address. The MCP answer's `deposit_address_check` says the same. With an address, the result records the status lookup check in its evidence.
- Signature. The recorded 1Click answer has a `signature` field. It is not verified: no 1Click verifying key is recorded in this repository, and the committed fixture replaces the signature with a note (`tests/test_intents.py:1-3`).
- Replay. Quote and status requests use `gate.rpc.Client` and the `replay`, `record` and `off` fixture modes. Status GET parameters are part of the fixture key, and replay reads a matching recorded response from `fixtures/intents/`. The status answers in the tests are synthetic and marked `synthetic: true`; no live status answer is committed. The tests are in `tests/test_intents.py` and `tests/test_rpc.py`.
