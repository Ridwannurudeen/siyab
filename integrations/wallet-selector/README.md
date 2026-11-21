# Wallet Selector reference guard

A reference guard for a dapp transfer flow built on NEAR Wallet Selector. It wraps the wallet's two signing methods, reads the real payee from the actions, calls the hosted account check and maps the answer to allow, ask or deny. No detection logic is added here, no wallet code is modified and no key is read.

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

`guardWallet` reads the payee from each action: the receiver of a `Transfer`, or `receiver_id` inside `ft_transfer` and `ft_transfer_call` arguments, and the contract for any other function call. It checks every distinct payee in a batch before the wallet is called. Actions it cannot read produce `SIYAB_PAYEE` rather than a guessed account.

`guardTransfer` is the single check. It requests `GET /live/account-check/<account>` from `docs/API.md`, then checks that the evidence string hashes to `evidence_hash` (an unsigned consistency check, not proof of who produced it), that the evidence subject is this account, and that a hosted ALLOW carries every check at `pass`, `action` `sign`, `signing` `never` and a null `verdict`.

- deny throws `SiyabRefusal` with code `SIYAB_DENY`.
- ask throws `SiyabConfirmationRequired` with code `SIYAB_ASK` unless `confirm(decision)` returns true, so an ask cannot be skipped by a caller that only handles allow.
- allow resolves.

A timeout, a 429, a 503 or an unreadable body becomes ask, never allow.

## The messages

- deny: `Siyab refused this transfer to <account>. <reasons>`
- ask: `Siyab could not clear <account>. <reasons> Continue only if you know this account.`
- allow: `Siyab found no refusal signals for <account>. This is advice, not a guarantee that the transfer is what it appears to be.`

An answer is advice from an unsigned route. The wallet still signs its own transaction, and only a signed Gate verdict authorises a Gate payment.

## Runtime

The adapter is an ES module and uses `TextEncoder` and `crypto.subtle`, so it runs in a browser and in Node 18 or later. It needs `fetch` and `AbortController`, both present in modern browsers and Node 18 or later.

## Recorded answers

The three fixtures under `fixtures/` were recorded from the hosted API on 2026-11-21:

- `account-check-allow.json`: `intents.near`, ALLOW, `action` `sign`
- `account-check-ask.json`: `hello.near`, ASK, a full access key finding
- `account-check-deny.json`: the Rhea collector account, DENY

`python -m unittest tests.test_wallet_selector_adapter` replays them. It also covers a deny with a different subject, an edited evidence string, a malformed answer, an unknown outcome, an HTTP 503, an ask with and without confirmation, payee extraction for transfer and ft_transfer actions, and a batch that checks both payees before signing.
