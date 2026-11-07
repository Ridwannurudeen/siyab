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
