# LangChain reference adapter

A reference tool for a LangChain agent that needs to check a NEAR payment before it sends one. The tool calls the hosted check API, binds the answer to the exact transfer and returns DENY, ASK or ALLOW text for the agent. No detection logic is added here and no key is read.

Reference code in our repository. Not reviewed, adopted or acknowledged by LangChain. No contact made as of 21 Nov. No LangChain package is imported unless you call `as_tool()`.

## The call

```python
from integrations.langchain.adapter import preflight, tool_result

decision = preflight(receiver="intents.near", amount="1000000000000000000000000", token="near")
print(tool_result(decision))
```

`preflight` requests `GET /live/verdict?receiver=<account>&amount=<base units>&token=<near or token id>` from `docs/API.md`. With `amount=None` it requests `GET /live/account-check/<account>` instead, which also covers contract control. It verifies that the evidence string hashes to `evidence_hash`, that the evidence subject is this receiver, amount and token, that the evidence verdict matches the answer, and that a hosted ALLOW carries every check at `pass`, `action` `sign`, `signing` `never` and a null `verdict`. Anything else becomes ask, and a timeout, a 429 or a 503 becomes ask, never allow.

In LangChain:

```python
from integrations.langchain.adapter import as_tool

check_payment = as_tool()
agent = create_tool_calling_agent(llm, [check_payment], prompt)
```

`as_tool()` needs `langchain-core` and returns a `StructuredTool`. The agent must call it before its send tool. A DENY result is a refusal; an ASK result needs a person; an ALLOW result is advice, and only a signed verdict authorises a Gate payment.

## The messages

- deny: `DENY: do not send this payment. <reasons>`
- ask: `ASK: a person must decide before this payment. <reasons> Do not send it on your own.`
- allow: `ALLOW: the check found no refusal signal. <reasons> This is advice, not permission to pay; only a signed verdict authorises a Gate payment.`

## Recorded answers

The fixtures under `fixtures/` were recorded from the hosted API on 2026-11-21:

- `verdict-allow.json`: `intents.near`, 1 NEAR, ALLOW, `action` `sign`
- `verdict-deny.json`: the Rhea collector account in `intel/labels.jsonl`, 1 NEAR, DENY
- `account-check-ask.json`: `hello.near`, ASK, a full access key finding

`python -m unittest tests.test_langchain_adapter` replays them and covers an edited evidence string, a different receiver, a different amount, an HTTP 503 and an ALLOW whose action is `ask`.
