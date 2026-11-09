"""Reference preflights that delegate to the existing NEAR and Intents verdict services."""

import time

from gate import intents, verdict


def preflight_near(kind, token, receiver, amount, memo, *, gate, agent_pk_hex, policy, store, rpc, nearblocks,
                   evidence_dir, injection_scanner=None, archival=None, clock=time.time, issuers=None,
                   reports_account=None):
    return verdict.request(kind, token, receiver, amount, memo, gate=gate, agent_pk_hex=agent_pk_hex,
                           policy=policy, store=store, rpc=rpc, nearblocks=nearblocks, signer=None, sign_actions=(),
                           injection_scanner=injection_scanner, archival=archival, evidence_dir=evidence_dir,
                           clock=clock, issuers=issuers, reports_account=reports_account)


def preflight_intents(origin_asset, destination_asset, amount, recipient, recipient_type="DESTINATION_CHAIN", *,
                      mode="replay", fixtures_root=intents.FIXTURES_ROOT, firewall_url=None, clock=time.time,
                      deadline=intents.RECORDED_DEADLINE):
    return intents.judge_quote(origin_asset, destination_asset, amount, recipient, recipient_type, mode=mode,
                               fixtures_root=fixtures_root, firewall_url=firewall_url, clock=clock, deadline=deadline)
