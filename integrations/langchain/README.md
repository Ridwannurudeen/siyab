# LangChain reference adapter

A reference tool for a LangChain agent that checks a NEAR payment before it sends one. The tool calls the hosted check API, reads the answer and its evidence, and returns DENY, ASK or ALLOW text for the agent. `guard_payment` is the enforcement point: it refuses on deny and needs a confirmation for every decision other than an explicit allow bound to the payment amount. No detection logic is added here and no key is read.

Reference code in our repository. Not reviewed, adopted or acknowledged by LangChain. No contact made as of 21 Nov. No LangChain package is imported unless you call `as_tool()`. Hosted routes judge with rules version 1 (`intel/live.py:410-424,558-565`; `gate/account_check.py:148-149`; `gate/verdict.py:62-64`); the holdout in `docs/HOLDOUT.md:21` scores rules version 3, which no HTTP parameter selects (`intel/live.py:397-420`).

## The call

```python
from integrations.langchain.adapter import preflight, tool_result

decision = preflight(receiver="intents.near", amount="1000000000000000000000000", token="near")
print(tool_result(decision))
```

`preflight` requests `GET /live/verdict?receiver=<account>&amount=<base units>&token=<near or token id>` from `docs/API.md`. With `amount=None` it requests `GET /live/account-check/<account>` instead. Arguments are keyword only, and the receiver, amount and token are validated before any request. Account ids use the NEAR separator grammar and lengths of 2 to 64 characters. Amounts must be canonical positive decimal strings within the u128 range; their length is capped before integer conversion.

The API checks one receiver and amount, using `token="near"` for native NEAR in yoctoNEAR. This adapter accepts no transaction actions, FunctionCall arguments, memo or `ft_transfer_call` message. The send callback must perform only the transfer described by these inputs. A transfer to any additional payee or a contract call needs its own guarded checks, including confirmation when a recipient cannot be checked.

The answer is read like this:

- A top level DENY, a DENY in hash matching evidence, or any signal check in that evidence blocks before outcome or subject comparisons. An edited or missing top level outcome cannot turn that evidence into a confirmable ask.
- The evidence string must hash to `evidence_hash`. That is an unsigned consistency check, not proof of who produced it. The reasons shown to the agent and the checks validated by the adapter come from that evidence document, not from the top level fields. The tool text shows reasons only.
- The evidence subject must be this receiver, amount and token, with an explicitly null `memo_sha256`. A missing or nonnull memo hash needs confirmation. The kind is `near` for a NEAR transfer and `ft` for a token transfer.
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

`guard_payment` raises `PaymentRefused` on deny. On ask it raises `ConfirmationRequired` unless `confirm(decision)` returns the literal Python `True`. The confirmation decision includes `receiver`. A missing or unexpected decision also needs this confirmation. Before that, `preflight` is the read-only check, and `as_tool()` wraps it as a LangChain `StructuredTool` when `langchain-core` is installed. Without `guard_payment` in the send path, the tool result is advice only.

`guard_payment(amount=None)` always needs confirmation unless the account check denies, which remains a hard refusal. An account ALLOW does not bind any amount and cannot by itself authorise the send callback.

## The messages

- deny: `DENY: do not send this payment. <reasons>`
- ask: `ASK: a person must decide before this payment. <reasons> Do not send it on your own.`
- allow: `ALLOW: the check found no refusal signal. <reasons> This is advice, not permission to pay; only a signed verdict authorises a Gate payment.`

## Recorded answers

The fixtures under `fixtures/` were recorded from the hosted API on 2026-11-21, except `verdict-ask.json`, recorded on 2026-11-22:

- `verdict-allow.json`: `intents.near`, 1 NEAR, ALLOW
- `verdict-deny.json`: the Rhea collector account, 1 NEAR, DENY
- `verdict-token-allow.json`: `intents.near` receiving USDT, ALLOW, kind `ft`
- `verdict-token-deny.json`: `hello.near` receiving USDT, DENY, kind `ft`
- `account-check-ask.json`: `hello.near`, ASK
- `verdict-ask.json`: `aisthetic.near`, 1 yoctoNEAR, ASK because the source metadata read could not run

`python -m unittest tests.test_langchain_adapter` recomputes every fixture evidence hash and replays the recorded answers. It covers denial and signal precedence even with edited outcomes or mismatched subjects, empty evidence reasons without a top level fallback, malformed and deeply nested evidence, malformed response bodies, HTTP 429, timeouts, unknown outcomes, account grammar, canonical u128 amount bounds, and validation before network access. Guard tests cover denial, explicit allow, missing or unexpected decisions, receiver in confirmation, literal `True` confirmation and exactly one send after approval. These are offline adapter checks; they do not prove wallet execution, LangChain agent compliance or signed Gate authorisation.
