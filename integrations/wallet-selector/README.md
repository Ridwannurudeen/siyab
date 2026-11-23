# Wallet Selector reference guard

A reference guard for a dapp transfer flow built on NEAR Wallet Selector. It guards `signAndSendTransaction` and `signAndSendTransactions`, calls hosted account and transfer checks and maps the answer to allow, ask or deny. No detection logic is added here, no wallet code is modified and no key is read.

Reference code in our repository. Not reviewed, adopted or acknowledged by the Wallet Selector maintainers. No contact made as of 21 Nov. This adapter has not been tested inside a production wallet flow.

## The call

```js
import { guardWallet } from "./adapter.mjs";

const guarded = guardWallet(wallet, {
  baseUrl: "https://siyab.xyz",
  confirm: async decision => {
    // Show decision.message and decision.reasons, then return true only if the
    // person chooses to continue. A false or missing answer blocks the transfer.
    return await showConfirm(decision.message);
  },
});

await guarded.signAndSendTransaction({ receiverId: "intents.near", actions });
// or guarded.signAndSendTransactions({ transactions });
```

A dapp on another origin reads `GET /live/account-check/<account>` and `GET /live/verdict` through the public cross-origin policy: `Access-Control-Allow-Origin: *`, no credentials, and an optional Content-Type request header only (`intel/api.py:86-91,405-420,477-480`; `docs/API.md`, Cross-origin reads). The wrapper makes GET requests without setting credentials (`integrations/wallet-selector/adapter.mjs:149-154`). Since 22 Nov 2026, the hosted copy serving main includes this policy (`docs/lanes/docs-sync.md:7`); the proxy rollout checks are in `docs/RUNBOOK.md`, Public reverse proxy.

`guardWallet` supports the tagged `Transfer` and `FunctionCall` action shapes published in [Wallet Selector 8.10.2](https://unpkg.com/@near-wallet-selector/core@8.10.2/src/lib/wallet/transactions.types.d.ts). `Transfer.params` contains `deposit`; its recipient is the transaction's required `receiverId`. For a FunctionCall, `receiverId` is the called contract, which may forward funds to other accounts. The wrapper checks that contract with `GET /live/account-check/<account>`. Every positive Transfer deposit and FunctionCall attached deposit also needs `GET /live/verdict` with that exact yoctoNEAR amount, the transaction's `receiverId` as receiver and `token=near`. The native amount route is supported, so an account ALLOW cannot substitute for that amount check, even for one yoctoNEAR.

Both `ft_transfer` and `ft_transfer_call` read `receiver_id` and `amount` from the arguments and request `GET /live/verdict` with that recipient, amount and the transaction's `receiverId` as the token contract. Every FunctionCall also scans nested objects and arrays for recipient fields: `receiver_id`, `account_id`, `beneficiary_id`, `recipient`, `recipient_id`, `to`, `new_owner_id` and `owner_id`, their camelCase and plural forms, plus receiver, beneficiary, destination, payee, target and owner aliases. `owner_id` is conservatively treated as a possible transfer target. JSON objects and arrays encoded inside string fields are inspected too. Opaque non-empty `msg`, `payload`, `args`, `data`, `message` or `instructions` fields need confirmation. Every named account needs its own ALLOW or confirmed ASK, and any DENY blocks. Fields that cannot be read as account ids need confirmation. Only the two FT methods have a supported transfer shape. The allowlist of other methods known to have no recipients is empty. All other methods, including `mt_transfer`, `ft_withdraw`, `nft_transfer` and `nft_transfer_call`, need confirmation even after their named recipients pass.

Every transaction and action is parsed before any network check or signing. Missing or invalid transaction account ids, invalid transfer amounts or deposit fields, empty actions, unsupported action types and byte arguments backed by SharedArrayBuffer throw `SIYAB_PAYEE`, including when another action or transaction is valid. Arguments may be a plain object, JSON text, UTF-8 JSON bytes or base64 JSON text or bytes. Unreadable arguments, including arrays, ArrayBuffers and invalid UTF-8, require ASK confirmation after the known checks. The wrapper snapshots the transaction before awaiting checks, so later caller edits cannot change what it sends. Ordinary wallet methods remain bound to the wallet; tests cover `getAccounts`, `signOut` and `isSignedIn`. Other methods containing `sign` or `delegate`, plus `verifyOwner`, throw `SIYAB_PAYEE`. This includes message, raw transaction, ownership and delegate signing.

`signIn` is guarded too. [Core 8.10.2's published wallet types](https://unpkg.com/@near-wallet-selector/core@8.10.2/src/lib/wallet/wallet.types.d.ts) require `contractId` and accept optional `methodNames`. The wrapper snapshots these parameters, checks `contractId` with the account route and calls the original wallet only after ALLOW or an explicit ASK confirmation. DENY always blocks. Wallet specific sign-in fields are preserved in the snapshot.

`guardTransfer` runs one check. Its default preflight selects the account route, or the verdict route when an amount and token are supplied. It checks that the evidence string hashes to `evidence_hash` (an unsigned consistency check, not proof of who produced it), that the evidence subject matches the account or exact transfer, and that a hosted ALLOW carries every check at `pass`, `action` `sign`, `signing` `never` and a null `verdict`. A top-level DENY, an evidence DENY or any evidence signal refuses even if the outcome or subject otherwise disagrees. Displayed reasons come only from matching hashed evidence or fixed local messages.

- deny throws `SiyabRefusal` with code `SIYAB_DENY`.
- ask and every unexpected decision throw `SiyabConfirmationRequired` with code `SIYAB_ASK` unless `confirm(decision)` returns true. The decision includes `receiver`, `message` and `reasons`.
- allow resolves.

A timeout, a 429, a 503 or an unreadable body becomes ask, never allow.

## The messages

- deny: `Siyab refused this transfer to <account>. <reasons>`
- ask: `Siyab could not clear <account>. <reasons> Continue only if you know this account.`
- allow: `Siyab found no refusal signals for <account>. This is advice, not a guarantee that the transfer is what it appears to be.`

An answer is advice from an unsigned route. The wallet still signs its own transaction, and only a signed Gate verdict authorises a Gate payment.

The hosted verdict route has no memo query field and judges an absent memo. Every account a non-empty `ft_transfer_call.msg` names in the recipient fields above, or a msg that is itself an account id, is checked with the account route, and any DENY blocks. A non-empty msg then always requires confirmation, because a message can name onward recipients under fields the wrapper does not know. An empty string adds no onward recipient check; a missing or non-string message requires confirmation. The same inspection applies to a supplied `msg` on other FunctionCalls. The wrapper does not simulate arbitrary contract effects or inspect token memo text. A recipient check does not prove what the called contract will do.

## Runtime

The adapter is an ES module requiring `fetch`, `AbortController`, `structuredClone`, text encoding APIs and `globalThis.crypto.subtle`. Use a modern browser in a secure context or Node 19 or later. Node's global crypto became enabled without its flag in [Node 19](https://nodejs.org/download/release/v19.0.0/docs/api/globals.html#crypto). [Node 18](https://nodejs.org/download/release/v18.20.8/docs/api/globals.html#crypto) requires `--experimental-global-webcrypto`; plain Node 18 is not sufficient. Tests run against stub wallets and recorded HTTP answers, not a production browser wallet.

## Recorded answers

The three fixtures under `fixtures/` were recorded from the hosted API on 2026-11-21:

- `account-check-allow.json`: `intents.near`, ALLOW, `action` `sign`
- `account-check-ask.json`: `hello.near`, ASK, a full access key finding
- `account-check-deny.json`: the Rhea collector account, DENY

`python -m unittest tests.test_wallet_selector_adapter` runs 30 named JavaScript scenarios through one Python test, plus two tests for missing Node. A missing Node executable fails by default, including in CI. Set `SIYAB_SKIP_NODE_TESTS=1` to permit a skip explicitly when Node is unavailable. The harness hashes every fixture in both adapter directories and replays recorded native and token verdicts and the `aisthetic.near` ASK verdict. Coverage includes evidence binding, failure responses, every supported argument encoding, mixed batches, unsupported signing paths, mutation protection, recipient aliases and nested arrays, the drainer scenarios above, onward FT message recipients, unreadable or accountless messages, mandatory confirmation for unknown methods, exact native amounts and guarded sign-in. The wallet stub asserts twenty one sends, with refusal paths leaving the counter unchanged. These are offline checks, without a production wallet or transaction.
