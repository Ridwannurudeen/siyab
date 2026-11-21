# LangChain reference adapter

A reference tool for a LangChain agent that checks a NEAR payment before it sends one. The tool calls the hosted check API, reads the answer and its evidence, and returns DENY, ASK or ALLOW text for the agent. `guard_payment` is the enforcement point: it refuses on deny and needs a confirmation on ask. No detection logic is added here and no key is read.

Reference code in our repository. Not reviewed, adopted or acknowledged by LangChain. No contact made as of 21 Nov. No LangChain package is imported unless you call `as_tool()`.

## The call

```python
from integrations.langchain.adapter import preflight, tool_result

decision = preflight(receiver="intents.near", amount="1000000000000000000000000", token="near")
print(tool_result(decision))
```

`preflight` requests `GET /live/verdict?receiver=<account>&amount=<base units>&token=<near or token id>` from `docs/API.md`. With `amount=None` it requests `GET /live/account-check/<account>` instead. Arguments are keyword only, and the receiver, amount and token are validated before any request.

The answer is read like this:

- A DENY is honoured before any binding check, because a denial blocks a payment.
- The evidence string must hash to `evidence_hash`. That is an unsigned consistency check, not proof of who produced it. The reasons and checks shown to the agent come from that evidence document, not from the top level fields.
- The evidence subject must be this receiver, amount and token. The kind is `near` for a NEAR transfer and `ft` for a token transfer.
- A hosted ALLOW must carry every evidence check at `pass`, `action` `sign`, `signing` `never` and a null `verdict`.
- A timeout, a 429, a 503, a malformed body or an unknown outcome becomes ask, never allow.

## Enforcing it on the send tool

An agent can ignore a tool result, so the send tool must call `guard_payment`:

```python
from integrations.langchain.adapter import ConfirmationRequired, PaymentRefused, guard_payment

def send_payment(receiver, amount, token="near", confirm=None):
    return guard_payment(send=lambda: wallet.send(receiver, amount, token),
                         receiver=receiver, amount=amount, token=token, confirm=confirm)
```

`guard_payment` raises `PaymentRefused` on deny. On ask it raises `ConfirmationRequired` unless `confirm(decision)` returns true. Before that, `preflight` is the read-only check, and `as_tool()` wraps it as a LangChain `StructuredTool` when `langchain-core` is installed. Without `guard_payment` in the send path, the tool result is advice only.

## The messages

- deny: `DENY: do not send this payment. <reasons>`
- ask: `ASK: a person must decide before this payment. <reasons> Do not send it on your own.`
- allow: `ALLOW: the check found no refusal signal. <reasons> This is advice, not permission to pay; only a signed verdict authorises a Gate payment.`

## Recorded answers

The fixtures under `fixtures/` were recorded from the hosted API on 2026-11-21:

- `verdict-allow.json`: `intents.near`, 1 NEAR, ALLOW
- `verdict-deny.json`: the Rhea collector account, 1 NEAR, DENY
- `verdict-token-allow.json`: `intents.near` receiving USDT, ALLOW, kind `ft`
- `verdict-token-deny.json`: `hello.near` receiving USDT, DENY, kind `ft`
- `account-check-ask.json`: `hello.near`, ASK

`python -m unittest tests.test_langchain_adapter` replays them and covers a deny with a different subject, an edited evidence string, a malformed body, an HTTP 429, a timeout, an unknown outcome, reasons taken from the evidence rather than the top level, validation before the network call, and `guard_payment` on deny, ask and allow.
