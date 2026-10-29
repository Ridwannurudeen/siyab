"""Access key hygiene for one NEAR account, over view_access_key_list and get_verifier_keys.

scan() reports, one heading per entry of HEADINGS and in that order: the key count, the full access
keys, function call keys with an empty method_names (they may call any method on their receiver),
dead keys (allowance "0"), keys with no allowance limit (allowance null), the agent keys of a Gate
(an account that answers get_verifier_keys: function call keys scoped to the account itself) whose
method_names is not exactly ["request_transfer"], the Gate's verifier keys next to the TEST key,
labelled different or, when equal, SAME in capitals, and the account's label assertions. A read that
could not run is reported as unknown, never as a clean result. The client records and replays like
every other client; its fixtures live under fixtures/scan/.
"""

import re
from pathlib import Path

from gate import base58, ed25519, rpc, rules

FIXTURES_DIR = Path(__file__).resolve().parent.parent / "fixtures" / "scan"
AGENT_METHODS = ["request_transfer"]
TEST_KEY = ed25519.public_from_seed(ed25519.TEST_VERIFIER_SEED)
_HEX64 = re.compile(r"[0-9a-f]{64}")
HEADINGS = ("account", "access keys", "full access keys", "empty method_names", "dead keys",
            "unlimited allowance", "agent keys", "verifier keys", "labels")


def _key_findings(keys, account_id):
    calls = [(k["public_key"], k["access_key"]["permission"]["FunctionCall"]) for k in keys
             if k["access_key"]["permission"] != "FullAccess"]
    return {
        "count": len(keys),
        "full_access": [k["public_key"] for k in keys if k["access_key"]["permission"] == "FullAccess"],
        "empty_method_names": [key for key, call in calls if not call["method_names"]],
        "dead": [key for key, call in calls if call["allowance"] == "0"],
        "unlimited": [key for key, call in calls if call["allowance"] is None],
        "agent_keys": [key for key, call in calls if call["receiver_id"] == account_id],
        "agent_keys_not_request_transfer": [key for key, call in calls if call["receiver_id"] == account_id
                                            and call["method_names"] != AGENT_METHODS],
    }


def _verifier_lines(verifier):
    keys = [("current", verifier["current"])]
    if verifier.get("previous"):
        keys.append((f"previous until {verifier['previous']['valid_until_ms']} ms", verifier["previous"]["key"]))
    lines = [f"verifier keys: the TEST key is {base58.encode_key(TEST_KEY)}"]
    for name, key_hex in keys:
        key = bytes.fromhex(key_hex)
        relation = "SAME as the TEST key" if key == TEST_KEY else "different from the TEST key"
        lines.append(f"  on chain {name}: {base58.encode_key(key)}, {relation}")
    return lines


def scan(account_id, *, client, store):
    """{"findings": {...}, "lines": [...]}; each line starts with a heading of HEADINGS, in order,
    or with two spaces when it lists an item under the heading above it."""
    findings = {"account": account_id, "rpc": client.url, "block_height": None, "keys": None,
                "keys_error": None, "verifier": None, "verifier_error": None}
    try:
        result = client.view_access_key_list(account_id)
        findings["block_height"] = result.get("block_height")
        try:
            findings["keys"] = _key_findings(result["keys"], account_id)
        except (KeyError, TypeError) as error:
            raise rpc.MalformedResponse(f"a key entry lacks the fields scan reads: {error!r}") from error
    except rules.UNAVAILABLE as error:
        findings["keys_error"] = rules._why(error)
    try:
        verifier = client.call_function(account_id, "get_verifier_keys", {})
        previous = verifier.get("previous") if isinstance(verifier, dict) else None
        if not isinstance(verifier, dict) or not _HEX64.fullmatch(str(verifier.get("current"))) or (
                previous is not None and not _HEX64.fullmatch(str(previous.get("key")))):
            raise rpc.MalformedResponse("get_verifier_keys did not return its keys as 64 hex")
        findings["verifier"] = verifier
    except rules.UNAVAILABLE as error:
        findings["verifier_error"] = rules._why(error)
    findings["labels"] = [{"id": a.id, "label": a.label, "category": a.category, "provenance": a.provenance}
                          for a in store.by_subject(account_id, "near")]

    keys = findings["keys"]
    block = findings["block_height"]
    lines = [f"account: {account_id} on {client.url}" + (f", read at block {block}" if block is not None else "")]
    if keys is None:
        lines.append(f"access keys: unknown, view_access_key_list could not run: {findings['keys_error']}")
        lines += [f"{heading}: unknown" for heading in HEADINGS[2:7]]
    else:
        lines.append(f"access keys: {keys['count']}")
        for heading, name, found in (
                ("full access keys", "", keys["full_access"]),
                ("empty method_names", ", these may call any method on their receiver", keys["empty_method_names"]),
                ("dead keys", ', allowance "0"', keys["dead"]),
                ("unlimited allowance", ", allowance null", keys["unlimited"])):
            lines.append(f"{heading}: {len(found)}{name}")
            lines += [f"  {key}" for key in found]
        if findings["verifier"] is None:
            lines.append(f"agent keys: unknown, {account_id} did not answer get_verifier_keys, so it is not "
                         "read as a Gate")
        else:
            wrong = keys["agent_keys_not_request_transfer"]
            lines.append(f"agent keys: {len(keys['agent_keys'])} scoped to {account_id}, {len(wrong)} with "
                         'method_names other than exactly ["request_transfer"]')
            lines += [f"  {key}" for key in wrong]
    if findings["verifier"] is None:
        lines.append(f"verifier keys: unknown, get_verifier_keys could not run: {findings['verifier_error']}")
    else:
        lines += _verifier_lines(findings["verifier"])
    lines.append(f"labels: {len(findings['labels'])}")
    lines += [f"  {rules._quoted(row['label'])} from the label store, category {row['category']}, provenance "
              f"{row['provenance']}, assertion {row['id'][:12]}" for row in findings["labels"]]
    return {"findings": findings, "lines": lines}
