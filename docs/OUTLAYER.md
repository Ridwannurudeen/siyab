# OutLayer approver adapter

`gate/outlayer.py` lets an OutLayer Agent Custody wallet use the verdict service as one of its policy approvers. It is built and tested offline; there is no live OutLayer integration, and no OutLayer endpoint was called while building it.

## How OutLayer approvals work

Every fact below was read on 2026-11-07 from the public sources named with it. The API specification is an alpha (`0.1.0-alpha.4`), and its README says minor releases may break compatibility.

Sources:

- [A] Agent Custody documentation, https://app.outlayer.ai/docs/agent-custody (https://outlayer.fastnear.com/docs/agent-custody answers 301 to it)
- [B] OutLayer HTTP API specification, https://raw.githubusercontent.com/out-layer/api-spec/main/openapi.yaml, `info.version` `0.1.0-alpha.4`; its README (https://github.com/out-layer/api-spec) calls it the source of truth for the wire format
- [C] Agent Connect documentation, https://app.outlayer.ai/docs/agent-connect
- [D] TypeScript SDK approvals guide, https://raw.githubusercontent.com/out-layer/sdk-js/main/docs/approvals.md

| Fact | Source |
|---|---|
| OutLayer documents no outbound policy hook: it never calls an outside service to decide an operation. An approver pulls pending approvals and votes. | [A], [B], [C] |
| On a wallet whose policy has an approval threshold, a fund moving operation answers `status: pending_approval` with `approval_id`, `required`, `approved` and `request_hash`, and runs automatically once `required` approve votes are in. | [A], [B] `CallResponse`, `WithdrawResponse` |
| Approvers are listed in the policy under `approval.approvers`, each with `id` (a NEAR account), `role` (`admin` or `signer`, default `signer`) and an optional `pubkey` that pins the approver to one key. | [B] `ApprovalConfig`, `Approver` |
| A vote from an account the policy does not list, or from a key other than a pinned one, is refused with `403 not_approver` and never counts. | [A], [B] |
| `GET /wallet/v1/approval/{id}` (no auth) answers an `ApprovalDetail`: `id`, `wallet_id`, `request_type`, `request_data` (an object), `status` (`pending`, `approved`, `rejected`, `expired`), `required_approvals`, `request_hash`, `op` (optional), `wallet_pubkey`, `expires_at`, `created_at`, `approvers`. | [B] `getApprovalDetail`, `ApprovalDetail` |
| `GET /wallet/v1/pending_approvals` (wallet key) and `GET /wallet/v1/pending_approvals_by_pubkey` (owner session) list `PendingApproval` entries with `approval_id`, `type`, `request_data`, `request_hash`, `required`, `approved`, `expires_at`. | [B] |
| `request_type` is one of `call`, `transfer`, `withdraw`, `cross_chain_withdraw`, `limit_order`, `deposit`, `swap`, `confidential`, `policy_update`. | [B] `RequestType` |
| `request_hash` is described as sha256 of the canonical JSON of the operation; approvers sign over it. | [B] `CallResponse.request_hash` |
| A vote is `POST /wallet/v1/approve/{id}` or `POST /wallet/v1/reject/{id}` with exactly one proof: a NEP-413 signature (`signature`, `public_key`, `account_id`, `nonce`) by a full access key, or a `ContractVoteAuth` (`account_id`, `authorization`) for a contract approver without keys. Both proofs, or neither, is a 400. The signature is the authentication; no API key is needed. | [B] `ApproveRequest`, `RejectRequest`; [D] |
| The signed vote message is `approve:{approval_id}:{wallet_pubkey}:{request_hash}`, or the same with `reject:`. | [A], [B] |
| `RejectRequest` takes an optional `reason`. A reject vote from a listed approver cancels the request (`RejectResponse.status` `rejected`); otherwise the vote is stored and the request stays pending (`reject_vote_recorded`). | [B] |
| A pending approval lives 24 hours and carries `expires_at`. | [B], [D] |
| The TEE evaluates the policy again at execution, before it signs. | [A] |

## What the adapter does

`vote(detail, judge)` takes one `ApprovalDetail` and a judge function, and returns `approval_id`, `vote`, `message`, `reason` and `judgement`. `make_judge(config, evidence_dir, store)` builds the judge the way `gate/http.py` builds its own: `gate.verdict.request` bound to the configured Gate and policy, with no signer and an empty `sign_actions`, so no verdict is ever signed.

It reads only two operations:

- `request_type` `transfer`, with `to` (or its alias `receiver_id`, never both) and `amount`, and `chain` absent or `near`: judged as a NEAR transfer.
- `request_type` `call` with `method_name` `ft_transfer`, `receiver_id` the token, `args` holding `receiver_id`, `amount` and an optional `memo`, and `deposit` absent or `1`: judged as a token transfer.

It translates only the verdict's `action`; it never decides anything itself:

| `action` | Vote | `message` |
|---|---|---|
| `sign` (ALLOW) | `approve` | `approve:{id}:{wallet_pubkey}:{request_hash}` |
| `refuse` (DENY) | `reject` | `reject:{id}:{wallet_pubkey}:{request_hash}` |
| `ask` (UNKNOWN, or an ALLOW the Gate would refuse) | `abstain` | none |

OutLayer has no review vote. Its human review path is the approvals page, where a pending approval waits for the other approvers until `expires_at`. So `ask` abstains: the adapter casts no vote and the decision stays with the people the policy lists. It is not mapped to reject, because a reject vote from a listed approver cancels the request outright.

Every other `request_type`, any `ft_transfer` deposit other than 1 yoctoNEAR (the verdict does not judge attached NEAR), any field in `request_data` or `args` the adapter does not read (a call's `gas` is accepted without being judged, since gas moves no funds), an approval whose `status` is not `pending`, and a transfer whose fields have no canonical form all abstain without a verdict. A detail that is not an object, or lacks `id`, `request_type`, `request_data`, `status`, `request_hash` or `wallet_pubkey`, raises `ApprovalError`.

The adapter signs nothing, sends nothing and reads no key. It opens no connection of its own; the verdict it asks for reads the chain through the same clients `gate/http.py` uses, so with `GATE_FIXTURES_MODE=off` those reads go to the network, as the HTTP endpoint's do. The returned `message` is for the approver's own key to sign as a NEP-413 message and post, outside this repository.

## Tests

`tests/test_outlayer_adapter.py` runs offline with the network refused, over the recorded replay fixtures `tests/test_http.py` uses, with the approvals in `fixtures/outlayer/approvals.json`. Those approvals are synthetic: built by hand from the `ApprovalDetail` schema, with placeholder identifiers and hashes, not recorded from OutLayer. The tests cover approve (ALLOW, `sign`), reject (DENY, `refuse`), abstain for UNKNOWN and for an ALLOW whose action is `ask`, the operations it does not judge, a non canonical amount, malformed approvals, and a source check that the module imports no signer or network client and calls `gate.verdict.request` with no signer and no signing action.

## Not verified

- The shape of `request_data`. The specification types it only as an object; the fixtures assume it mirrors the `TransferRequest` and `CallRequest` bodies. If OutLayer sends other fields, the adapter abstains on every approval rather than guessing.
- That `request_data` describes the same operation as the `op` whose canonical JSON `request_hash` covers. The canonical JSON form is not specified, so the adapter does not recompute `request_hash`.
- The vote message. The specification and the docs page give `approve:{approval_id}:{wallet_pubkey}:{request_hash}`; the SDK guide [D] shows `approve:{approval_id}:{request_hash}`. The adapter follows the specification.
- Whether NEAR Intents transfers (`/wallet/v1/intents/transfer`) arrive with `request_type` `transfer` or another type; the adapter judges only the shapes above.
- The `request_completed` webhook the docs page mentions: the specification has no such event, and the adapter does not use it.
- Nothing here was run against OutLayer: no wallet, no approval, no vote. The verdict outcomes come from the recorded replays.
- Whether OutLayer counts a second vote from the same approver once, and whether it refuses a vote once `expires_at` has passed. `vote_once` casts at most one vote per approval id for as long as its caller keeps `seen`; `vote` alone keeps no memory.

## Cases

The 27 cases below use the 28 approvals in `fixtures/outlayer/approvals.json` (the changed amount case uses two) and have one test each in `tests/test_outlayer_adapter.py` (`CaseTest`). They are constructed from the specification and run offline: every approval was written by hand from the `ApprovalDetail` schema and is marked synthetic in the file, none was recorded from OutLayer, and no OutLayer endpoint was called. Each test passes its approval through `gate/outlayer.py` and the real verdict path, `gate.verdict.request` over the recorded mainnet RPC replays the other offline tests use, with the network refused; no verdict is mocked.

The judges, all built by `make_judge` for the Gate `gate.near` with the test policy of `tests/test_http.py`:

- replays: the recorded replays as they are.
- RPC outage: the NEAR RPC, NearBlocks and archival URLs point at a closed local port, so no read finds a recorded response, as when those providers are down.
- scan outage: `SHIELDBOT_API_URL` points at that port, so the ShieldBot injection scan of the memo cannot run.
- reports outage: `reports_account` is `reports.gate.near`, whose `subject_summary` has no recorded response.
- paused Gate: the policy carries `gate_paused` true.
- not judged: the test's judge fails the test if it is called.

| Case | Fixture | Judged with | Vote | Why |
|---|---|---|---|---|
| Allow, `ft_transfer` call | `ft_allow` | replays | approve | ALLOW, action `sign` |
| Allow, NEAR transfer | `near_allow` | replays | approve | ALLOW, action `sign` |
| Allow, call with `gas` and no `deposit` | `ft_gas_allow` | replays | approve | `gas` is read and not judged; the adapter reads an absent `deposit` as 1 yoctoNEAR |
| Deny, missing receiver | `ft_deny` | replays | reject | `receiver_exists` signal: `no-such-account-example.near` does not exist |
| Deny, incident labelled receiver | `near_incident_deny` | replays | reject | `label_denylist` signal: incident labels on `rhea000453.multica.near` |
| Deny, call for a token off the catalog | `ft_unlisted_token_deny` | replays | reject | `token_in_catalog` signal: `game.hot.tg` is neither a cap asset nor a catalog token |
| Deny, instruction shaped memo | `ft_memo_injection_deny` | replays | reject | `memo_injection` signal from the local patterns |
| Unknown, read not recorded | `near_unknown` | replays | abstain | `issuer_blacklist` could not run for `relay.aurora` |
| Unknown, `receiver_id` alias | `near_alias_unknown` | replays | abstain | the same transfer, named by `receiver_id` with `chain` `near` |
| Outage, NEAR RPC | `near_rpc_outage` | RPC outage | abstain | `receiver_exists` and `issuer_blacklist` could not run |
| Outage, injection scan | `ft_scanner_outage` | scan outage | abstain | `memo_injection` could not run |
| Outage, community reports | `near_reports_outage` | reports outage | abstain | `community_report` could not run |
| ALLOW the Gate would refuse | `near_gate_paused` | paused Gate | abstain | action `ask`: `REFUSED:paused` |
| Changed amount | `changed_amount_first`, `changed_amount_second` | replays, through `vote_once` | approve, then abstain | one id and one `request_hash`, amount 1000 then 10^24; `vote` alone approves the second with the same message as the first |
| Replay | `replay` | replays, through `vote_once` | approve, then abstain | a second sighting after an approve casts no vote |
| Retry after an outage | `retry_after_outage` | RPC outage, then replays, through `vote_once` | abstain, approve, then abstain | an abstain casts no vote, so the approval is voted once its checks run, and only once |
| Stale chain data | `stale_reads` | replays | approve | every read is more than 24 hours old; the adapter has no freshness bound |
| Approval past `expires_at` | `past_expires_at` | replays | approve | still `pending`; the adapter reads `status`, not `expires_at` |
| Expired approval | `expired` | not judged | abstain | `status` `expired` |
| Approval already approved | `approved` | not judged | abstain | `status` `approved` |
| Withdraw | `withdraw` | not judged | abstain | `request_type` `withdraw` is not judged |
| Call to another method | `other_method` | not judged | abstain | `storage_deposit` is not `ft_transfer` |
| Attached deposit | `ft_large_deposit` | not judged | abstain | a 1 NEAR `deposit`, which the verdict does not judge |
| Argument the adapter does not read | `ft_extra_arg` | not judged | abstain | `args` carries `msg` |
| Transfer on another chain | `other_chain` | not judged | abstain | `chain` `ethereum` |
| Amount with no canonical form | `amount_leading_zero` | replays, refused before any read | abstain | the amount `05` has a leading zero |
| Malformed approval | `malformed_missing_hash` | not judged | none: raises `ApprovalError` | no `request_hash` |

## Replay and changed content

`vote` keeps no memory: shown one approval twice, it judges it twice and returns the same vote and the same message both times. `vote_once(detail, judge, seen)` (`gate/outlayer.py::vote_once`) is for an approver that is shown approvals more than once. `seen` is a dict the caller keeps; for each approval id it holds what the first sighting showed (the transfer the verdict judged, the `request_hash` and the `wallet_pubkey`) and the vote cast for the id. Every sighting is judged as `vote` judges it, and an approve or reject is cast only when the sighting matches the first one and no vote was cast for the id yet. So a replayed approval is voted once, and an approval whose amount, receiver, token, memo or `request_hash` changed under its id gets no vote at all, approve or reject. Both abstain rather than reject, because a reject vote from a listed approver cancels the request. After an abstain the same approval is voted again, so one that waited on a check that could not run is decided once the check runs. The memory lasts as long as the caller keeps `seen`: a new dict, or a restarted approver, remembers nothing, and the adapter writes it nowhere.

## Stale data

The adapter has no freshness bound, and none was added. `vote` reads `status` and never `expires_at`, and `gate.verdict.request` records the block height its reads were observed at with no age limit. In replay mode a recorded read is answered at any age (`MAX_AGE_S` in `gate/rpc.py` only decides when record mode fetches again); with `GATE_FIXTURES_MODE=off` every read goes to the network when the verdict is asked for. An approval OutLayer reports as `expired` abstains like any approval that is not `pending`; a `pending` approval whose `expires_at` has passed is judged and voted.

## Outages

Three sources the verdict reads have an outage case: the NEAR RPC (`near_rpc_outage`, which also cuts the issuer blacklist views read over it), the ShieldBot injection scan (`ft_scanner_outage`) and the community reports contract (`near_reports_outage`). Each time the check reads `unknown`, the verdict is UNKNOWN with action `ask`, and the adapter abstains. `label_denylist` has no outage case: the label store is local, so that check never reads `unknown` (its row in the check table of `docs/API.md`).

## How an approver learns the approval id

The adapter's open question is answered by the facts table above, from the specification: the operation's own answer carries `approval_id` to whoever submitted it (the `pending_approval` row), the two `pending_approvals` listings need the wallet key or an owner session, and with the id `GET /wallet/v1/approval/{id}` needs no auth. So the agent that submitted the operation, or the wallet's owner, hands the id to the approver; none of those rows lets an approver account list its pending approvals under its own identity. This is read from the specification and was not run.
