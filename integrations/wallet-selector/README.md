# Wallet Selector reference preflight

A reference guard for a dapp transfer flow built on NEAR Wallet Selector. Before the wallet signs, the dapp calls the hosted account check and maps the answer to allow, ask or deny. No detection logic is added here, no wallet code is modified and no key is read.

Reference code in our repository. Not reviewed, adopted or acknowledged by the Wallet Selector maintainers. No contact made as of 21 Nov. This adapter has not been tested inside a production wallet flow.

## The call

```js
const { guardTransfer } = require("./adapter");

try {
  const decision = await guardTransfer({
    receiver: "intents.near",
    baseUrl: "https://siyab.xyz",
  });
  if (decision.decision === "ask") {
    // Show decision.message and decision.reasons to the person, and let them
    // cancel. Do not continue on their behalf.
    return;
  }
  await wallet.signAndSendTransaction({ receiverId: "intents.near", actions });
} catch (error) {
  if (error.code === "SIYAB_DENY") {
    showBanner(error.message);
    return;
  }
  throw error;
}
```

`guardTransfer` requests `GET /live/account-check/<account>` from `docs/API.md`. It checks that the evidence string hashes to `evidence_hash`, that the evidence subject is this account, and that a hosted ALLOW carries every check at `pass`, `action` `sign`, `signing` `never` and a null `verdict`. Anything else becomes ask. A deny throws `SiyabRefusal` with code `SIYAB_DENY`, so a caller that forgets to branch still cannot sign.

## The messages

- deny: `Siyab refused this transfer to <account>. <reasons>`
- ask: `Siyab could not clear <account>. <reasons> Continue only if you know this account.`
- allow: `Siyab found no refusal signals for <account>. This is advice, not a guarantee that the transfer is what it appears to be.`

An answer is advice from an unsigned route. The wallet still signs its own transaction, and only a signed Gate verdict authorises a Gate payment. A timeout, a 429 or a 503 becomes ask, never allow.

## Recorded answers

The three fixtures under `fixtures/` were recorded from the hosted API on 2026-11-21. The account and the outcome:

- `account-check-allow.json`: `intents.near`, ALLOW, `action` `sign`
- `account-check-ask.json`: `hello.near`, ASK, a full access key finding
- `account-check-deny.json`: the Rhea collector account in `intel/labels.jsonl`, DENY, incident and issuer labels

`python -m unittest tests.test_wallet_selector_adapter` replays them through the adapter. The test also covers an edited evidence string, a different account, an HTTP 503 and an ALLOW whose action is `ask`.
