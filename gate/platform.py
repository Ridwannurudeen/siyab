"""The three answers in NEAR's own words, and the platform field each one drives.

    The track asks for allow, warn or block. NEAR's Agent API names the controls that express them:
    owner_approval, destinations, limits, budget, timelock_ms and frozen. This module is a
    translation, not an integration: it states the mapping so a reader can see that a verdict has a
    place in a policy the platform already defines, and it never claims a control we have not built.

    docs/PLATFORM.md carries the same table with sources, and tests/test_platform.py fails if the
    two disagree or if a state stops having an answer.
"""

from intel import records

SOURCE = "https://docs.agentsonintents.com/integration/agent-api/policies-and-limits"

# What a payment does under NEAR's Agent API for each answer. The field names are the ones the
# platform documents for an agent policy, so a reader can wire us into one without reading our code.
POLICY = {
    "allow": "the transfer proceeds under the agent's own limits",
    "warn": "owner_approval holds the money action until the owner votes",
    "block": "destinations except refuses the account at dispatch",
    "no effect": "nothing applies",
}

# The order a page renders, and the verdict literal that produces each decision. A record with no
# effect has no literal: it is what an unreadable or a closed record answers.
ROWS = (("allow", "ALLOW"), ("warn", "UNKNOWN"), ("block", "DENY"), ("no effect", None))


def decide(verdict):
    """The track's word for a verdict literal, or None when the literal is not one of ours."""
    return {"ALLOW": "allow", "UNKNOWN": "warn", "DENY": "block"}.get(verdict)


def view(verdict):
    """The decision and the platform field that carries it, for one verdict literal."""
    decision = decide(verdict) or "no effect"
    return {"decision": decision, "policy": POLICY[decision], "source": SOURCE}


def for_summary(summary, *, allowlisted=False):
    """The same view, taken through the one function that judges a subject."""
    return view(records.verdict_for(summary, allowlisted=allowlisted))


def table():
    """The mapping as rows a page can render: decision, the verdict that produces it, the field."""
    return [{"decision": decision, "verdict": literal, "policy": POLICY[decision]}
            for decision, literal in ROWS]
