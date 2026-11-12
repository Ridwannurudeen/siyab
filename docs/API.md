# API

This file is the consumer contract of the verdict interface: what `gate.verdict.request` returns, which checks can appear in it and what each status means, how `outcome` and `action` relate, a recorded answer for ALLOW and for DENY, and the local HTTP endpoint that serves it.

`tests/test_api_doc.py` checks the response field names and order, exact check ids, `Kind` rows against emitted near/ft check ids, and `Tier` rows against `TIER2_CHECKS` plus allowlisted and off-allowlist ft calls. It checks that `issuer_blacklist` appears only when an issuer table is passed and `community_report` only when `reports_account` is set; for CLI, MCP and HTTP it verifies `issuers_for("mainnet")` for Gate ids not ending `.testnet` and no issuer table for `.testnet`; and it checks the outcome/action rows: ALLOW is `ask` when `ask` is nonempty and otherwise `sign`, DENY is `refuse`, and UNKNOWN is `ask`. It also checks the ALLOW and DENY examples against their fixtures with 64-hex values shortened, and that the committed UNKNOWN fixtures are exactly the top receiver judgements whose verdict is UNKNOWN while the UNKNOWN example section cites no fixture. The meanings in the `pass`, `signal` and `unknown` check-table cells are documentation checked by review, not by a test.

## The verdict response

`gate.verdict.request` (`gate/verdict.py:61`) judges one transfer and returns a dict with exactly these keys, in this order (`gate/verdict.py:134-146`). Every adapter (the CLI `python -m gate verdict`, the MCP tool `check_near_payment`, the HTTP endpoint, the pay wrapper) starts from this dict.

### Fields

| Field | Type | Meaning | Code |
|---|---|---|---|
| `outcome` | string | `ALLOW`, `DENY` or `UNKNOWN`, the verdict of `gate.rules.judge` | `gate/verdict.py:135`, `gate/rules.py:863` |
| `action` | string | `sign`, `ask` or `refuse`: what the caller does with the outcome (see Outcome and action) | `gate/verdict.py:136`, `gate/verdict.py:104-105` |
| `ask` | list of strings | the reasons a human must decide before anything is signed or sent; empty for DENY | `gate/verdict.py:137`, `gate/verdict.py:20-58` |
| `verdict` | object or null | the Verdict object the Gate's `request_transfer` takes: `literal`, `issued_ms`, `expires_ms`, `verdict_id`, `evidence_hash`, `key_id`, `signature`; null unless the outcome is ALLOW and the action is in the caller's `sign_actions` | `gate/verdict.py:138`, `gate/verdict.py:106-109`, `gate/verdict.py:120-133` |
| `request` | object | the TransferRequest judged: `kind`, `token_id`, `receiver_id`, `amount` (the canonical decimal string), `memo` (string or null) | `gate/verdict.py:139`, `gate/verdict.py:96-98` |
| `checks` | list of objects | one object per check that ran, `{id, status, detail, source}`, in the order the rules ran them (see Checks) | `gate/verdict.py:140`, `gate/evidence.py:43` |
| `reasons` | list of strings | the reasons for the outcome | `gate/verdict.py:141`, `gate/rules.py:805-839` |
| `evidence_hash` | string | the sha256 hex of the canonical bytes of the evidence document, which is also its file name | `gate/verdict.py:142`, `gate/verdict.py:117-118` |
| `evidence_path` | string | where the evidence document was written, `<evidence_dir>/<evidence_hash>.json` | `gate/verdict.py:143`, `gate/evidence.py:93-100` |
| `latency_ms` | integer | whole milliseconds from just before the rules run until immediately after the conditional `verdict_id` assignment, following the action and signing decision. An id is drawn only for ALLOW when its action is in `sign_actions`; DENY, UNKNOWN and unsigned ALLOW get no id. The interval excludes request validation before it and evidence-document construction, evidence write and signature afterward. | `gate/verdict.py:144`, `gate/verdict.py:99-110` |
| `message` | string or null | the canonical message the signature covers (`docs/VERDICT-RECORD.md`), or null when `verdict` is null | `gate/verdict.py:145`, `gate/verdict.py:120-124` |

The adapters change the dict in two places only. The HTTP endpoint drops `evidence_path`, sets `verdict` and `message` to null and adds `signing` (`gate/http.py:163-164`). The MCP tool adds `gate`, `signer`, `agent_pk` and `policy_source` (`mcp/server.py:179`).

### Checks

Each check is `{"id", "status", "detail", "source"}` (`gate/evidence.py:43-47`). `status` is `pass`, `signal` or `unknown` (`gate/evidence.py:27`); `evidence.check` raises on any other value, and there is no `not_run` status: a check that did not run is absent from `checks`. `detail` is text for a human. `source` is `{"provider", "request_sha256", "fetched_at"}` (`docs/VERDICT-RECORD.md`): the RPC or HTTP host with the replay key of the request and its fetch time, or for a local read a local provider name (`label store`, `local patterns`, `allowlist and label store`, or `nearblocks` when no request was made) with `fetched_at` null (`gate/rules.py:117-122`).

Tiers (`gate/rules.py:852-860`): tier 1 runs for every receiver; tier 2 runs only for a receiver that is not on the agent's `allowlist` in the policy (`TIER2_CHECKS`, `gate/rules.py:37`). Kind `near` runs no token check; kind `ft` adds the three token checks and also looks the token up in the label store (`gate/rules.py:422-427`, `gate/rules.py:436-456`).

### Check table

| Check id | Kind | Tier | Enabled by | Network | pass | signal | unknown |
|---|---|---|---|---|---|---|---|
| `receiver_exists` | near, ft | 1 | always | any | `view_account` answered (`gate/rules.py:409-410`) | the RPC answers `UNKNOWN_ACCOUNT`: the receiver does not exist (`gate/rules.py:412-415`) | any other RPC error, or a read that could not run (`gate/rules.py:411-418`) |
| `label_denylist` | near, ft | 1 | always | any | no current `denylist` or `incident` label on the receiver, or on the token for ft (`gate/rules.py:429-431`) | a current `denylist` or `incident` label on the receiver or, for ft, the token (`gate/rules.py:423-427`) | never: the label store is local |
| `issuer_blacklist` | near, ft | 1 | the caller passes the issuer table of `intel/issuers.json` (`gate/rules.py:433-434`) | mainnet Gate only: the CLI, the MCP tool and the HTTP endpoint pass `issuers_for("mainnet")` for a Gate not ending `.testnet` (`gate/__main__.py:70`, `mcp/server.py:166`, `gate/http.py:150`), and the testnet table is empty | every issuer view answers a value other than its banned one, or the token has no such view (`gate/rules.py:383-392`) | an issuer's view answers its banned value for the receiver (`gate/rules.py:383-387`) | a read could not run and no issuer answered banned (`gate/rules.py:389-395`) |
| `token_in_catalog` | ft | 1 | always for ft | any | the token is a cap asset of the agent or a `catalog_token` in the label store (`gate/rules.py:436-441`) | neither (`gate/rules.py:442-444`) | never: the policy and the label store are local |
| `ft_metadata_sane` | ft | 1 | always for ft | any | `ft_metadata` has a non empty name and symbol and decimals from 0 to 24 (`gate/rules.py:486-493`) | the token has no code or no `ft_metadata` method, or the metadata fails that shape (`gate/rules.py:478-491`) | any other RPC error, or a read that could not run (`gate/rules.py:478-485`) |
| `storage_registered` | ft | 1 | always for ft | any | `storage_balance_of` for the receiver is not null (`gate/rules.py:447-453`) | `storage_balance_of` is null: the receiver is not registered on the token (`gate/rules.py:448-450`) | the read could not run (`gate/rules.py:454-456`) |
| `memo_injection` | near, ft | 1 | always; `SHIELDBOT_API_URL` adds the ShieldBot injection scan to the local patterns (`gate/__main__.py:48-49`) | any | no memo, or no local pattern matched and the scan, when set, answered clean (`gate/rules.py:498-518`) | a local pattern matched, or the scan flagged the memo (`gate/rules.py:500-517`) | only with the scan set: the scan could not run and no local pattern matched (`gate/rules.py:511-518`) |
| `community_report` | near, ft | 1 | `REPORTS_ACCOUNT_ID` names the reports contract (`gate/http.py:102`, `gate/__main__.py:71`, `mcp/server.py:159`); unset, the check is absent (`gate/rules.py:460-471`) | any | no report, only pending reports, or accepted social reports for a payee on the allowlist (`gate/rules.py:460-471`, `intel/reports.py:45-48`, `intel/reports.py:51-54`) | an accepted deny report (`intel/reports.py:39-42`) | accepted social reports for a payee off the allowlist, so a human decides; or the summary is unreadable or `subject_summary` could not be read (`gate/rules.py:460-471`, `intel/reports.py:35-50`) |
| `account_age` | near, ft | 2 | always for tier 2; first reads the oldest eligible access key nonce and dates it on archival RPC, or uses archival `view_account` when no usable nonce exists or the oldest key block is younger than 7 days; NearBlocks is consulted only when that archival path cannot date the account (`gate/rules.py:535-625`, `gate/rules.py:628-700`) | any | the oldest key nonce or archival `view_account` dates the account at least 7 days ago, or the oldest key creation the NearBlocks fallback lists is at least 7 days old, with every listed key dated, fewer than 25 listed, and archival `view_account` not having found the receiver missing at the historical block (`gate/rules.py:535-625`, `gate/rules.py:628-700`) | on the NearBlocks fallback, the oldest key creation NearBlocks lists is younger than 7 days, every listed key carries a creation time, fewer than 25 keys are listed (25 is a full page that may leave out older ones), and that key's transaction creates the account: the NearBlocks answer names the requested hash, its outcomes status is `true` (an attempted creation proves nothing), and it is a `CREATE_ACCOUNT` action sent to the account, which succeeds only for an account that does not exist yet, or a `create_account` call on the account's own parent naming it as `new_account_id`, since only the parent can create a sub account and the parent cannot add a key to a sub account that already exists, though a sub account running its own contract could add a key to itself during such a transaction, so an old cooperating sub account whose older keys were deleted could read young, which affects only that receiver; an account id with no dot has no parent (`gate/rules.py:270-340`, `gate/rules.py:628-700`); a key block younger than 7 days only shows the account existed by then, so neither it nor `view_account` gives a signal (`gate/rules.py:576-625`) | neither the archival path nor the NearBlocks fallback dates the account (a listed key without a creation time, 25 or more keys listed, a malformed creation time, or a young oldest key whose transaction does not show the account being created, including one whose outcomes status is false, null, missing or not a boolean, or whose answer names another hash), the archival block is younger than 7 days, or the receiver did not exist at the historical block, which an older NearBlocks key creation does not override (`gate/rules.py:535-625`, `gate/rules.py:270-340`, `gate/rules.py:628-700`) |
| `dust_funded` | near, ft | 2 | always for tier 2; on the archival default path it does not read the creation transaction; on the NearBlocks fallback path it reads the creation transaction only when the keys response supplies a creation hash, and takes its deposit only when that transaction shows the account being created, by the same test the age check applies (`gate/rules.py:666-683`, `gate/rules.py:702-759`) | any | the creation deposit is at least 0.1 NEAR, or is below it with no lookalike or account_age signal, or account age passed by any path with no lookalike, so no deposit, read or not, can change the verdict (`gate/rules.py:702-759`) | the creation deposit is below 0.1 NEAR together with a lookalike or account_age signal (`DUST_YOCTO`, `gate/rules.py:40`, `gate/rules.py:749-759`) | with a lookalike, or without an account age pass: the creation transaction is not known or could not be read, the transaction of an oldest listed key at least 7 days old does not show the account being created, or the deposit is not a u128 amount (an integer from 0 to below 2**128 yoctoNEAR, given as a JSON number or a string of ASCII digits); on the default path, a lookalike leaves it unknown without a NearBlocks request (`gate/rules.py:702-721`, `gate/rules.py:737-759`) |
| `lookalike` | near, ft | 2 | always for tier 2 | any | no allowlisted name is contained and no allowlisted or labelled reference account is within the length scaled distance, or the receiver is itself a labelled reference account (`gate/rules.py:723-735`) | the receiver contains an allowlisted name, or is within the distance of an allowlisted or labelled reference account after confusable normalisation (`gate/rules.py:230-243`, `gate/rules.py:723-728`) | never: the allowlist and the label store are local |
| `code_without_metadata` | near, ft | 2 | always for tier 2 | any | no contract code, code with `contract_source_metadata`, or code without it and no other tier 2 signal (`gate/rules.py:769-789`) | contract code without `contract_source_metadata` together with another tier 2 signal (`gate/rules.py:782-786`, `gate/rules.py:764-765`) | the receiver account could not be read, or `contract_source_metadata` could not be read (`gate/rules.py:769-772`, `gate/rules.py:787-789`) |

With an archival client and a readable receiver, tier 2 reads `view_access_key_list` first, then dates its oldest usable nonce with archival `block_at`; when there is no usable nonce, it reads the archival block and then `view_account` in sequence. An archival pass is final, and the archival path never signals. If archival age is unknown, `account_age` falls back to NearBlocks account keys; when the oldest listed key is younger than 7 days it reads that key's transaction to see whether it created the account, and `dust_funded` reuses that read. An implicit account created by a plain transfer is never proven created, so when its oldest listed key is younger than 7 days its age reads unknown, never a signal. When the oldest listed key is older, `dust_funded` requests that transaction only when the keys response includes its hash, and reads its deposit only when the same test shows it created the account. On either path, when account age passes and nothing is a lookalike, `dust_funded` passes without a deposit it did not or could not read, since a deposit is a signal only with a lookalike or a young account. On the default path, `dust_funded` stays unknown without a NearBlocks request when a lookalike is found; a resulting DENY keeps its deciding lookalike reason but loses the sentence naming a 0.01 NEAR creation deposit. These are the only tier 2 checks that request NearBlocks on the fallback path. Any check that cannot run remains `unknown`, and UNKNOWN is refused.

NearBlocks is a fallback source whose terms are under review. Its free plan is for personal use, and its terms prohibit third party apps from interacting with the service without prior written consent; none was sought or given. On testnet, NearBlocks API v1 was retired on 2026-11-05 and returns HTTP 410 with `API v1 was retired on 2026-11-05. Use /v3`; mainnet v1 still answers.

FastNEAR supplies the RPC, archival RPC and transfers reads used here. It publishes no terms of service or rate limits for those interfaces; this dependency is unverified on those two points.

The issuer source records current token blacklist views as `onchain` `denylist` assertions with `python -m intel issuers seed`; `label_denylist` reads them from the store, while `issuer_blacklist` remains a separate view check.

Line numbers without a file are `gate/rules.py`. The test compares the table's check ids with the literal first arguments of `evidence.check` calls in `gate/rules.py`. It does not validate the prose in the `pass`, `signal` and `unknown` columns; those descriptions are checked by review.

### Outcome and action

`outcome` comes from the statuses alone (`gate/rules.py:862-863`): DENY when any check is `signal`, else UNKNOWN when any check is `unknown`, else ALLOW. A signal wins over a check that could not run.

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

The committed UNKNOWN verdicts are exactly the top receiver judgements whose verdict is UNKNOWN, under `fixtures/receivers/top/judgements/`; they reflect incomplete data for real mainnet receivers. The 84 receiver judgements under `fixtures/receivers/judgements/` record no UNKNOWN. The example was `fixtures/receivers/judgements/liver.pool.near.json`, a mainnet staking pool of the recorded receiver set that read UNKNOWN on `account_age` and `dust_funded`, until the archival `view_account` read dated the receivers with no access key (`docs/SPIKES.md` S8); it now records ALLOW. The incident page `intel/incidents/rhea.json` also contains UNKNOWN `rules_today` entries, judged without the archival client; these are not separate fixture records. An UNKNOWN answer has the fields of The verdict response with `"outcome": "UNKNOWN"`, `"action": "ask"`, an `ask` that names each check that could not run as `<id> could not run: <detail>` (Outcome and action), and `verdict` and `message` null. `tests/test_api_doc.py` checks that committed UNKNOWN fixtures are exactly those top receiver judgements and that this section cites no fixture.

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

The answer is the `gate.verdict.request` result described in The verdict response, with three changes (`gate/http.py:163-164`): `evidence_path` is removed, `verdict` and `message` are always null, and `signing` is added with the unsigned rule above. `action` is passed through unchanged, so an ALLOW whose action is `sign` still says `sign` although nothing was signed. The default Gate `gate.near` does not end `.testnet`, so the endpoint runs `issuer_blacklist` (`gate/http.py:150`, `gate/rules.py:399`, `gate/rules.py:795`); with `intents.near` allowlisted and a `wrap.near` cap in `GATE_POLICY`, the body above answers ALLOW with the seven tier 1 checks `receiver_exists`, `label_denylist`, `issuer_blacklist`, `token_in_catalog`, `ft_metadata_sane`, `storage_registered` and `memo_injection`. The recorded examples are under Examples.

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
- OFAC sanctions. `ofac_sanctioned` reads the committed local SDN address fixture for the destination recipient and never uses the network. A `0x` plus 40 hex recipient is matched by its normalized EVM key across all listed digital-currency codes, independent of whether the destination chain has a published code. Other recipient formats use only the asset codes mapped to that destination chain; a chain with no mapped code passes with detail that the list publishes no address for that chain. A match is a signal and makes the verdict DENY with the SDN uid, entry name, programs, list date and source XML hash in its reason. A no-match pass records the list date and the number of distinct comparison keys checked. `docs/SANCTIONS.md` lists the mapped codes and explains the EVM rule. A missing or unreadable fixture is UNKNOWN. Its evidence source names the fixture and carries the hash of its JSON bytes.
- The NEAR payment path (`gate/rules.py`) is unchanged: OFAC publishes no NEAR account.
- Deposit address. A dry quote has no deposit address. Only when the caller supplies the address from a real quote with `--deposit-address` or MCP `deposit_address`, the CLI or MCP tool makes a read only `GET /v0/status?depositAddress=<address>` on the same 1Click base URL. The status answer's `quoteResponse.quoteRequest` must match the judged request on every `BOUND_FIELDS` value. A differing field is refused by name. A 404 means 1Click does not know the address and is refused. A failed read, including a network error, 401 or 5xx, is UNKNOWN with its reason and is refused. No other endpoint is called, and nothing is signed, deposited or submitted.
- Without a supplied address, the CLI says the address was not checked because dry quotes have no deposit address. The MCP answer's `deposit_address_check` says the same. With an address, the result records the status lookup check in its evidence.
- Signature. The recorded 1Click answer has a `signature` field. It is not verified: no 1Click verifying key is recorded in this repository, and the committed fixture replaces the signature with a note (`tests/test_intents.py:1-3`).
- Replay. Quote and status requests use `gate.rpc.Client` and the `replay`, `record` and `off` fixture modes. Status GET parameters are part of the fixture key, and replay reads a matching recorded response from `fixtures/intents/`. The status answers in the tests are synthetic and marked `synthetic: true`; no live status answer is committed. The tests are in `tests/test_intents.py` and `tests/test_rpc.py`.

## Opt-in live API

`python -m intel serve --live` mounts the keyless routes in `intel/live.py`; without `--live`, `/live/...` answers 404 and the hosted JSON API keeps its no-network behavior (`intel/api.py:make_server`, `intel/__main__.py:main`). The live routes never build a signer or read a signing key. The verdict, scan, Intents and Reports routes write nothing to disk; when the server runs with a state directory, the account routes retain the public observations they read, as the archive paragraph below describes. Reads use `gate.rules.judge`, `gate.scan.scan`, `gate.intents.judge_quote`, and the read-only Reports views (`intel/live.py:LiveAPI`, `gate/rpc.py:request_deadline`).

| Method and route | Inputs | Network | Response |
|---|---|---|---|
| `GET /live/verdict` | `receiver`, `amount`, `token` | Mainnet | Unsigned verdict shape |
| `GET /live/scan/<near account>` | Account in path | Mainnet | Access key counts, key lists, findings and read block |
| `GET /live/intents` | `destination_asset`, `recipient`, `amount` | Mainnet 1Click and EVM checks | Unsigned verdict shape, including OFAC check |
| `GET /live/reports` | `subject` | Testnet Reports contract | Subject summary and reports |
| `GET /live/reports/recent` | None | Testnet Reports contract | Newest 20 reports |
| `GET /live/health` | None | None | Network label and rate limit |

Routes 1 to 3 read mainnet. A `.testnet` NEAR account is refused for the verdict and scan routes. The Intents destination is a 1Click asset id parsed by `gate.intents.parse_destination`; its recipient must be an EVM address. Reports read the testnet contract `reports.kestrelridge-main.testnet` (`intel/live.py:REPORTS_ACCOUNT`, `contracts/reports/src/lib.rs:subject_summary`). The report list route pages `reports_by_subject` and then reads each row with `get_report`; the recent route reads the newest 20 report ids from `totals` with `get_report` (`contracts/reports/src/lib.rs:reports_by_subject`, `contracts/reports/src/lib.rs:totals`).

The verdict and Intents responses have these fields: `outcome`, `action`, `reasons`, `checks`, `evidence_hash`, `freshness`, `verdict`, `signing`, and `latency_ms`. `UNKNOWN` maps to `ASK`; checks use `{name, status, detail}`. `freshness` is the existing observed block and timestamp object, or the timestamp object for an Intents quote. `verdict` is always null and `signing` is always `never`; an ALLOW action still says `sign`, which describes the judgement action only (`intel/live.py:LiveAPI._verdict_shape`).

`/live/verdict` accepts `receiver`, `amount`, and `token`; `token` is `near` or an account id. The contract has no memo query field, so this route judges an absent memo. Amounts are canonical positive decimal strings within the NEAR u128 range. `/live/intents` accepts `destination_asset`, `recipient`, and `amount`, but 1Click also requires an origin asset. Since the route contract omits it, the route uses the recorded source asset `nep141:wrap.near` (`intel/live.py:INTENTS_ORIGIN_ASSET`, `tests/test_intents.py:quote_for`).

The live routes answer JSON with `Cache-Control: no-store`. They allow 60 requests per client IP in a fixed minute, trust `X-Forwarded-For` only when the socket peer is `127.0.0.1`, allow eight requests in flight, and bound each request to 25 seconds. Successful answers are cached in memory for 60 seconds in a 128-entry cache, keyed by the full path and query. Errors have `{error, message}`; statuses are 400 for invalid input, 429 with `Retry-After`, 503 when all slots are occupied or a required Reports view is unreadable, 504 on timeout, and 500 for an unexpected failure (`intel/live.py:LiveAPI.handle`).

When the server runs with a state directory, account reads also retain what they read: profile, holdings, transaction and transfer pages go to the history archive, a graph answer gains a `snapshot` reference, and a portfolio answer gains a `snapshot` reference in the portfolio archive (`intel/live.py:LiveAPI._account_read`). A profile, transaction or transfer page whose rows are all retained already, unchanged apart from their `observed_at`, `fetched_at` and `request_sha256` provenance and the block they were read at (a profile's `native.block_height` and `native.block_hash`), adds no page, whichever retained pages hold those rows: it cites the first of those rows' retained pages that holds every row unchanged, and when none does, each row cites the retained page that last stored it. A transaction or transfer page with no rows adds no page when it matches the account's latest retained page of that kind apart from that provenance and its `pagination.limit`. A holdings page is a window of the account's full token inventory, so it adds no page only when the newest retained holdings page with the same `pagination.offset` and `pagination.limit` holds exactly the same tokens, none missing and none added, each retained unchanged apart from that provenance and a holding's `metadata_source.block_height`, and has the same pagination `total` and `next_offset`; it then cites that page. An empty holdings page adds none only when that newest page is empty too and matches it apart from that provenance. A token that has gone from or been added to that window, or a changed inventory `total` or `next_offset`, therefore adds a page, unless the archive is full and the read answers with `archive_paused` as described below. A current portfolio adds no observation when it matches the account's latest current observation apart from its `observed_at`, `effective_at` and `fetched_at` times and the blocks its native balance and token metadata were read at (an asset's `details.block_height` and `details.metadata_source.block_height` or `block_hash`); a historical portfolio adds none when it matches the latest observation with the same effective time apart from its `observed_at` and `fetched_at` times. A transaction or transfer page is a window of the account's history, so only its rows are compared: a page holding a subset of retained rows, or one that differs only in its pagination, `source` or `coverage`, adds no page and cites the retained pages that hold its rows. For a holdings page that holds tokens, its tokens and its pagination `total` and `next_offset` are compared, not its `source` or `coverage`. A profile page is one row, so the whole profile is compared. Any other difference in a row, or in an empty page, adds a new page, unless the archive rejects the row, as it does a transaction or transfer whose timestamp changed. For a portfolio any other difference, including a balance, a price or its timestamp, or the token inventory, adds a new observation. Retained ids and bytes never change, and a reused page or observation holds every row it is cited for. Investigation jobs cite those retained pages for their rows and copy each cited page's `source` and `observed_at`, so one job read can list more than one observation (`intel/history.py:AccountHistory.record_page`, `intel/portfolio.py:PortfolioHistory.save`, `intel/investigation_jobs.py:InvestigationJobs.tick`). A full archive or a reached budget never fails a read that succeeded. Every retained history page and portfolio observation is checked before it is reused, cited or returned: its bytes must hash to its id and be canonical JSON of the expected shape, naming the mainnet network, and each asset of a portfolio observation must hold its `balance_base_units`, `decimals` and `balance`, each null or of its supported type, with `balance` matching the other two. A retained history row must be canonical JSON naming its own row id and, for a transaction or transfer, its retained timestamp, and the retained page it cites must hold it byte for byte; that check passes before a row's timestamp can reject a read as changed or position an archive cursor. When an account read is archived, a retained page, row or observation that fails that check is neither reused nor cited: the read is saved as a new page or observation, which later unchanged reads reuse. Only a read whose own id equals that of a retained page or observation that fails the check fails with 503, and the watch monitor and investigation jobs record that failure as a failed attempt and keep running. Input the archive rejects, such as a transaction or transfer whose timestamp changed, fails the read with 400. Archive browsing, and fetching a retained portfolio observation by id, answer 503 when a row, page or observation they would return or cite, or the row an archive cursor names, fails its check. When the archive is full, the public share of it is used up, or the portfolio capture budget is reached, the route still answers 200 with the live result, adds `"archive_paused": true` and leaves out `snapshot`. Only the backfill route, which exists to archive, answers 503 with `{error: "archive_capacity", message, coverage}` and `Retry-After: 60`.

The history and portfolio databases are written only by Siyab, and these checks are defence in depth against damage to a retained record, not a defence against someone who can rewrite a database together with its hashes. They guarantee that a damaged page, row or observation is never reused or cited, that archive browsing which would return or cite it answers 503 instead of serving it, and that a fresh read of the same history rows replaces a damaged history row and its citation. Portfolio observations are never replaced: a damaged one stays retained while later reads are saved as new observations. A damaged history row that no later read covers, and a damaged portfolio observation, keep archive browsing that reaches them for that account and kind at 503 until an operator repairs them.

The backfill route (`/live/accounts/<account>/backfill`) and team investigation jobs have separate global provider page budgets, 12 pages per minute in all. The backfill route is anonymous: it reserves its `max_pages` up front and answers 503 `archive_capacity` with `Retry-After: 60` when that minute's public count plus `max_pages` would exceed 4 (`intel/history.py:PUBLIC_BACKFILL_PAGES`). Investigation jobs of all teams together read at most 8 pages per minute (`intel/history.py:JOB_BACKFILL_PAGES`); a job past that waits with `last_error` `history_capacity`. Job pages never count toward the public 4 and public pages never count toward the job 8, so anonymous callers cannot take pages from jobs and jobs cannot close the route. Watch polling uses neither budget. Each backfill and job read keeps one checkpoint per account and kind, holding its lease and the one read per account per minute. A job read for a new account waits with `history_capacity` while 20,000 checkpoints are kept; it first deletes every checkpoint whose lease has ended and whose minute has passed, so finished reads never hold job reads shut (`intel/history.py:AccountHistory.claim_job_read`). Public backfills keep their cursors in at most 10,000 account and kind checkpoints; a backfill for a new account and kind that finds all 10,000 kept first removes one that holds no lease and whose minute has passed, an exhausted one before others and otherwise the one least recently advanced, together with its lease row, and is refused only when none can be removed, so the removed account's next backfill starts over without a cursor from its newest page while the pages and rows already archived for it stay (`intel/history.py:AccountHistory.backfill_account`).

Team investigation jobs (`/workspace-api/teams/<team>/investigation-jobs`) answer 503 `job_capacity` with `Retry-After: 60` and change nothing when a create, or a retry of a `failed` or `cancelled` job, would give the team more than 10 active jobs (`queued`, `running` or `retry_wait`; `intel/investigation_jobs.py:MAX_ACTIVE_JOBS`). A team retains at most 100 jobs (`intel/investigation_jobs.py:MAX_TEAM_JOBS`): a create at that limit first removes the team's oldest finished jobs (`completed`, `cancelled` or `failed`, by `created_ms`). Job bodies count against a 16 MiB share per team (`intel/investigation_jobs.py:MAX_TEAM_JOB_BYTES`) inside a 64 MiB bound across all teams (`intel/investigation_jobs.py:MAX_JOB_BYTES`). When a create or a running job's next page would take the team past its share, that team's own oldest finished jobs, by `created_ms`, are removed until it fits. When it would take all teams past the 64 MiB bound, the oldest finished jobs of any team are removed, oldest first by `updated_ms`, the time the job last became `completed`, `cancelled` or `failed`, until it fits. Every finished job is also removed 30 days after that time (`intel/investigation_jobs.py:RETENTION_MS`), oldest first and at most 1,000 per create or worker tick (`intel/investigation_jobs.py:MAX_EXPIRED_PER_WRITE`), whether or not its team returns; cancelling a job that is already `cancelled` does not change that time. `queued`, `running` and `retry_wait` jobs are never removed. If nothing finished is left to remove, the create answers `job_capacity`. Every fit check also keeps 10 bytes free for each active job counted against that bound (`intel/investigation_jobs.py:CAPACITY_STOP_BYTES`), the growth of a job that ends with `stop_reason` `job_capacity`, so that ending never takes usage past the team share or the 64 MiB bound; a retry of a `failed` or `cancelled` job that would leave less than that room after removals answers `job_capacity` and changes nothing. Each worker tick that takes a job first checks, before the archive, page budget and watch checks and before any provider read, that the team share and the 64 MiB bound have room for the larger of 2,048 bytes (`intel/investigation_jobs.py:MIN_JOB_ROOM`, more than the largest create) and the size of the job's last page that did not fit, removing finished jobs as above. With no room the job does not read the provider, and a page it read that does not fit is not added to the job. Either way the job stays `queued` with `last_error` `history_capacity` and `retry_at_ms` 60 seconds later and does not count it as a failed attempt. After three such waits in a row (`intel/investigation_jobs.py:MAX_CAPACITY_WAITS`), counting the first, the job ends `completed` with `stop_reason` `job_capacity` and `last_error` null, keeping the pages it already saved; a graph job ended this way answers `graph` null and keeps its retained transfers. A job whose final result does not fit ends the same way. A tick that finds room, or a saved page, starts the count again. A job ended this way cannot be retried. A job created before that 10-byte room was kept, whose ending finds no such room even after removing finished jobs as above, ends `failed` with `last_error` `job_capacity` and an unchanged body instead, keeping its saved pages; its retry answers `job_capacity` until that room exists. A storage error in a worker tick of `investigation-follow` is written to the service log and the worker tries again after its polling interval; with `--once` the command exits with the error. The count advances only when the job is due, which a provider cooldown can postpone, and a running worker takes it. While the worker runs, active jobs that lack that room therefore end after three waits whether or not the archive is also full, even when their team never returns, and as finished jobs they can then be removed. Provider, page budget and archive waits never count toward this limit and never end a job. While the archive, at the limits job pages use, has no room for one more page (`intel/history.py:MAX_ARCHIVE_PAGES`), for 100 more rows (`intel/history.py:MAX_ARCHIVE_ROWS`), the most one page holds, or for 4 MiB more bytes (`intel/history.py:MAX_ARCHIVE_BYTES`, `intel/history.py:JOB_PAGE_BYTES`), twice the 2 MiB document limit (`intel/history.py:MAX_DOCUMENT_BYTES`) and so the most one accepted page and its rows take, no job reads the provider, even for a page the archive already holds, and each waits with `history_capacity`. Every job sees the same archive check, so no job takes a page to the job store while others wait on the archive. Another team's finished job can make room under the 64 MiB bound, but under the team share only the team's own finished jobs can. A removal deletes the job's rows and observations and releases their bytes in the same transaction, after which the job id answers 404 `not_found`. Removal is silent: it adds no audit entry and no job record, either for the team that held the job or for the team whose create or page caused it. A job yields to a due watched account only while the history worker heartbeat is at most 30 seconds old. A yield is shown as `state` `queued` with `last_error` `watch_priority` and `retry_at_ms` 10 seconds later; after three yields in a row the next read does not yield, and a saved page sets `last_error` back to null. Other provider and archive budget waits show `last_error` `history_capacity` with `retry_at_ms` at least 60 seconds later.

### Stubbed route examples

These examples are built from offline test stubs. They show response fields, not live reads.

#### `GET /live/verdict?receiver=alice.near&amount=1&token=near`

```json
{
  "outcome": "ALLOW",
  "action": "sign",
  "reasons": ["Every check ran and none found a signal."],
  "checks": [{"name": "receiver_exists", "status": "pass", "detail": "alice.near exists"}],
  "evidence_hash": "stubbed-evidence-hash",
  "freshness": {"block_height": 123, "timestamp_ms": 456},
  "verdict": null,
  "signing": "never",
  "latency_ms": 12
}
```

#### `GET /live/scan/agent.near`

```json
{
  "access_keys": 1,
  "full_access_keys": [],
  "empty_method_names": [],
  "dead_keys": [],
  "unlimited_allowance": [],
  "findings": ["The scan read 1 access key entries.", "The scan found 0 full access keys.", "The scan found 0 keys with empty method names.", "The scan found 0 keys with zero allowance.", "The scan found 0 keys with no allowance limit."],
  "read_at_block": 99
}
```

#### `GET /live/intents?destination_asset=nep141%3Abase-0x833589fcd6edb6e08f4c7c32d4f71b54bda02913.omft.near&recipient=0x0000000000000000000000000000000000000001&amount=5`

```json
{
  "outcome": "ALLOW",
  "action": "sign",
  "reasons": ["Every check ran and none found a signal."],
  "checks": [{"name": "ofac_sanctioned", "status": "pass", "detail": "no match"}],
  "evidence_hash": "stubbed-evidence-hash",
  "freshness": {"timestamp_ms": 789},
  "verdict": null,
  "signing": "never",
  "latency_ms": 12
}
```

#### `GET /live/reports?subject=vendor.testnet`

```json
{
  "subject_summary": {"accepted": 1, "pending": 0, "contested": 0, "latest_accepted_id": 7, "latest_deny_id": null, "deny": false},
  "reports": [{"id": 7, "subject": "vendor.testnet", "kind": "theft", "outcome": "near_miss", "status": "accepted", "evidence_url": "https://evidence.example/report", "evidence_sha256": "stubbed-report-hash", "submitter": "reporter.testnet", "moderator": "moderator.testnet"}]
}
```

#### `GET /live/reports/recent`

```json
{
  "reports": [{"id": 1, "subject": "vendor-1.testnet", "kind": "theft", "outcome": "near_miss", "status": "accepted", "evidence_url": "https://evidence.example/report", "evidence_sha256": "stubbed-report-hash", "submitter": "reporter.testnet", "moderator": "moderator.testnet"}, {"id": 0, "subject": "vendor-0.testnet", "kind": "theft", "outcome": "near_miss", "status": "accepted", "evidence_url": "https://evidence.example/report", "evidence_sha256": "stubbed-report-hash", "submitter": "reporter.testnet", "moderator": "moderator.testnet"}]
}
```

#### `GET /live/health`

```json
{"ok": true, "network": "mainnet", "rate_per_minute": 60}
```
