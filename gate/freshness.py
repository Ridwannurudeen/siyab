"""The checks rules version 2 adds: code_changed_recently and label_stale.

rules.judge runs them only when called with rules_version=2. Version 1, the default, is the rule set the
committed judgements were judged with.

code_changed_recently (tier 2): a receiver with contract code now is read with archival view_account at the
block _state_age reads, min_age_days times BLOCKS_PER_DAY_MAX blocks before its own view_account, with that
block's time read back. Other code there than now, or no receiver at that block, is a change. Honest
contracts upgrade too, so a change is a signal only together with another tier 2 signal, as
code_without_metadata is; alone it passes. A read that cannot run, a block younger than min_age_days and a
global contract named by its publisher's account id read unknown, never pass. No contract code now passes
without a read.

label_stale (tier 1): a current onchain label from a view_account read of the receiver, in one of the
templates intel/seed.py and intel/registries.py write, is compared with the receiver's live view_account.
It passes whether or not a label no longer holds, so it never changes a verdict; its detail names each
label that no longer holds, without the check id prefix a signal's detail carries. It is unknown only when
the live read failed, when receiver_exists is unknown too.
"""

import re

from gate import evidence, rules
from gate.rpc import EMPTY_CODE_HASH, RpcError, request_deadline

# Seconds the two archival reads of code_changed_recently may take together.
CODE_READS_S = 5
_BASE58 = "[1-9A-HJ-NP-Za-km-z]+"
# The label texts intel/seed.py (_rhea_reads) and intel/registries.py write from a view_account read.
LABEL_CLAIMS = (
    (re.compile("account exists with no contract code"), "no_code"),
    (re.compile("account exists; no contract code; balance class [A-Za-z0-9 ]+"), "no_code"),
    (re.compile("account exists; contract code present; balance class [A-Za-z0-9 ]+"), "code"),
    (re.compile(f"account exists with contract code hash ({_BASE58})"), "code_hash"),
    (re.compile(r"account does not exist at this block \(deleted or never created\)"), "absent"),
)


def _has_code(account):
    return (account.get("code_hash") != EMPTY_CODE_HASH or account.get("global_contract_hash") is not None
            or account.get("global_contract_account_id") is not None)


def _code(account):
    """The code a view_account answer shows, in words."""
    if account.get("global_contract_account_id") is not None:
        return f"the global contract published by {account['global_contract_account_id']}"
    if account.get("global_contract_hash") is not None:
        return f"global contract hash {account['global_contract_hash']}"
    if account.get("code_hash") == EMPTY_CODE_HASH:
        return "no contract code"
    return f"code hash {account.get('code_hash')}"


def _changed(before, block, now, other_signal, source):
    if other_signal:
        return evidence.check("code_changed_recently", "signal", f"code_changed_recently: {before} at {block}, {now} "
                              "now", source)
    return evidence.check("code_changed_recently", "pass", f"{before} at {block}, {now} now; not a signal without "
                          "another tier 2 signal", source)


def code_changed(archival, receiver, account, account_source, now_ms, min_age_days, other_signal):
    """The code_changed_recently check for the receiver's live view_account (account, or None); other_signal is
    True when another tier 2 check signals."""
    if account is None:
        return evidence.check("code_changed_recently", "unknown", "the receiver account could not be read",
                              account_source)
    if not _has_code(account):
        return evidence.check("code_changed_recently", "pass", "no contract code now", account_source)
    if account.get("global_contract_account_id") is not None:
        return evidence.check("code_changed_recently", "unknown", f"runs the global contract published by "
                              f"{account['global_contract_account_id']}, which its publisher can replace without a "
                              "change to this account", account_source)
    if archival is None:
        return evidence.check("code_changed_recently", "unknown",
                              "no archival client to read the code an old block held", account_source)
    reference = account.get("block_height")
    if type(reference) is not int:
        return evidence.check("code_changed_recently", "unknown", "the receiver's view_account names no block "
                              "height", account_source)
    height = reference - min_age_days * rules.BLOCKS_PER_DAY_MAX
    with request_deadline(CODE_READS_S):
        try:
            block_ms, iso = rules._block_time(archival, height)
        except rules.UNAVAILABLE as error:
            return evidence.check("code_changed_recently", "unknown",
                                  f"archival block_at could not run: {rules._why(error)}", rules._source(archival))
        block = f"block {height} ({iso}) from {rules._source(archival)['provider']}"
        if now_ms - block_ms < min_age_days * rules.DAY_MS:
            return evidence.check("code_changed_recently", "unknown", f"{block} is younger than {min_age_days} days",
                                  rules._source(archival))
        try:
            state = archival.view_account_at(receiver, height)
            if state.get("block_height") != height:
                raise rules.MalformedResponse(f"view_account at block {height} came back at block "
                                              f"{state.get('block_height')}")
        except rules.UNAVAILABLE as error:
            info = (error.error["cause"].get("info") if isinstance(error, RpcError)
                    and error.cause == "UNKNOWN_ACCOUNT" else None)
            if (isinstance(info, dict) and type(info.get("block_height")) is int and info["block_height"] == height
                    and info.get("requested_account_id") == receiver):
                return _changed(f"{receiver} did not exist", block, _code(account), other_signal,
                                rules._source(archival))
            return evidence.check("code_changed_recently", "unknown",
                                  f"archival view_account could not run: {rules._why(error)}", rules._source(archival))
    before, now = _code(state), _code(account)
    if before == now:
        return evidence.check("code_changed_recently", "pass", f"{before} at {block}, the same as now",
                              rules._source(archival))
    return _changed(before, block, now, other_signal, rules._source(archival))


def label_claim(text):
    """(claim, code hash or None) for a label text in a generated view_account template, else None."""
    for pattern, claim in LABEL_CLAIMS:
        match = pattern.fullmatch(text)
        if match:
            return claim, match.group(1) if pattern.groups else None
    return None


def _no_longer(claim, code_hash, receiver, account, missing):
    """Why the live read contradicts the claim, or None when it agrees."""
    if claim == "absent":
        return None if missing else f"view_account now finds {receiver}"
    if missing:
        return f"view_account now finds no {receiver}"
    if claim == "no_code" and _has_code(account) or claim == "code_hash" and _code(account) != f"code hash {code_hash}":
        return f"view_account now shows {_code(account)}"
    if claim == "code" and not _has_code(account):
        return "view_account now shows no contract code"
    return None


def label_stale(rows, receiver, account, missing, account_source):
    """The label_stale check over the current label rows of the receiver, or None when none states a
    view_account fact in a generated template; missing is True when RPC answered that the receiver does not exist."""
    compared = []
    for row in rows:
        source = row.source if isinstance(row.source, dict) else {}
        claim = label_claim(row.label)
        if (row.provenance == "onchain" and source.get("method") == "view_account"
                and source.get("args") == {"account_id": receiver} and claim is not None):
            compared.append((row, claim))
    if not compared:
        return None
    count = len(compared)
    if account is None and not missing:
        return evidence.check("label_stale", "unknown", f"the receiver account could not be read, so {count} "
                              f"on-chain label{'s were' if count > 1 else ' was'} not compared", account_source)
    stale = []
    for row, (claim, code_hash) in compared:
        why = _no_longer(claim, code_hash, receiver, account, missing)
        if why is not None:
            stale.append(f"{rules._quoted(row.label)} (assertion {row.id}, view_account at block "
                         f"{row.source.get('block')}) no longer holds: {why}")
    if not stale:
        return evidence.check("label_stale", "pass", f"{count} on-chain label{'s agree' if count > 1 else ' agrees'} "
                              "with the live view_account", account_source)
    return evidence.check("label_stale", "pass", f"{'; '.join(stale)}; informational, it changes no verdict",
                          account_source)
