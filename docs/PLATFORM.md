# Where this fits in a NEAR agent policy

This page is for the person wiring an agent to a wallet on NEAR. It says which of our answers lands on which control the platform already defines, and it is as clear about what we have not built as about what we have.

The platform reference is the Agent API policies and limits page: https://docs.agentsonintents.com/integration/agent-api/policies-and-limits

## The three answers, in the words the track uses

| Our verdict | The track's word | What a payment does under an agent policy |
|---|---|---|
| `ALLOW` | allow | the transfer proceeds under the agent's own limits |
| `UNKNOWN` | warn | owner_approval holds the money action until the owner votes |
| `DENY` | block | destinations except refuses the account at dispatch |
| no verdict | no effect | nothing applies |

`gate/platform.py` is the one place this mapping lives, and `tests/test_platform.py` fails if this page and that module disagree, or if a verdict ever stops agreeing with the function that judges a subject.

The owner's allowlist can turn our `warn` into no effect, but only for an ask that nothing is disputing: a pending record, or an accepted `social` report. A `contested` record asks anyway, because a dispute is an open moderation question that a person has to answer. Our `block` always keeps its effect.

## The controls the platform names, and what we do with each

- `frozen`: the platform's stop for every action. We do not set it. Our equivalent is the Gate's pause, which revokes the agent key and moves the balance out; the two are complementary, and an integrator should use ours for a transfer and theirs for the wallet.
- `owner_approval`: the human step. This is what our warn maps onto, and it is the reason an ask is acceptable: the money waits for a person.
- `destinations` with modes `only`, `except` and `any`: the address rule. Our block maps onto `except`, and our allowlist is the same idea as `only`. We do not write the platform's policy; we answer a question beside it.
- `limits` and `budget`: per asset and in USD. We do not replace them. The Gate keeps its own per call and window caps, so an integrator has two independent fences, which is the point.
- `timelock_ms` and `execution_cancel`: a delay before money moves. We do not implement a timelock. It is the one control in the platform list we would most like to add, and it belongs on the roadmap rather than in this build.
- `max_actions_per_hour`: not implemented on our side.

## OutLayer: the approver seat

OutLayer owns custody and static policy for agent wallets. An approver seat means an agent wallet cannot pay without our signature, which is the strongest version of what this project is for. It is not built. What it needs, in order: a service account, the approval request shape from their documentation, a signed verdict returned inside their flow, and a failure path that refuses when we are unreachable. Until that exists, this page says so, and no README sentence should imply otherwise.

## NEAR AI Cloud verification: attestation

The platform can return evidence that a request ran on hardware its operator does not control: a gateway attestation before a completion, model attestations, a TLS connection, image provenance and a response signature. The official verifier is at https://github.com/nearai/nearai-cloud-verifier, with a worked example at https://github.com/near-examples/nearai-cloud-verification-example.

Our service does not run in a TEE, and we do not claim it does. The rules read public chain data on an ordinary host and sign a verdict with an Ed25519 key. What we can attest instead is narrower and true: the contract build is pinned by `res/BUILD.md` and the code hash the testnet accounts run, the verdict is a signature over a canonical message, and the evidence document is stored under its own sha256. A reader can check the last two without trusting us.

What integrating the platform's verification would need: a request path through the gateway, a policy that names the measurements we accept, verification of each attestation candidate, and a recorded fixture for every check so the result replays offline. That is a project, not a paragraph, which is why it is on the roadmap with a size rather than in this build.

## How an integrator wires us today

1. Read the receiver's summary and our verdict before signing, through the rules or the MCP tool.
2. On block, do not sign. On warn, put the payment behind the owner's approval step and say which checks could not run.
3. On allow, sign with the function call key scoped to the Gate, carrying the verdict bound to that exact transfer.
4. Have the Gate pay, and keep the evidence hash. It is the only artefact that lets a third party replay the decision.

## What this page will not say

That we are an approver, that we run attested, or that we replace the platform's policy engine. Each of those would be a claim a reader could check in an afternoon.
