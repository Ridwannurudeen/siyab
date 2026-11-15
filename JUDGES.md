# For judges

Siyab is security intelligence for NEAR in which every label names its source, and its enforced case is a Gate contract on testnet that pays an agent's transfer only on a signed ALLOW bound to that exact transfer.

The judged state is the tag `hackathon-submission`, commit `84b09958c` of 14 Nov 2026, 22:39 UTC. Work after the deadline is dated in the README section "Changes after the 14 Nov 23:59 UTC deadline".

## Allow, warn, block

| Verdict | Wrapper `action` | Track word | Meaning |
|---|---|---|---|
| ALLOW | `sign` | allow | Every check ran and none found a signal; only a signed ALLOW moves funds through the Gate. |
| UNKNOWN, or an ALLOW the Gate would refuse as it stands | `ask` | warn | A check could not run, or the Gate would refuse the transfer as it stands; nothing is sent until a human decides. Unknown never passes. |
| DENY | `refuse` | block | A signal or a sourced label; nothing is signed and nothing is sent. |

The live check routes report UNKNOWN as `ASK` (`docs/API.md`). They are unsigned advice: `"verdict": null`, `"signing": "never"`.

## Check without a clone

POSIX shell with `curl` and Python. Outputs below were pasted from runs on 15 Nov 2026; block heights change on every run, and 64 character hex values are shortened to 8 characters at each end; the agent key's allowance falls as it pays gas.

```sh
F='import json,sys; d=json.load(sys.stdin); print(d["outcome"], d["action"], d["signing"], " ".join(c["name"]+"="+c["status"] for c in d["checks"]))'
view() { curl -s https://rpc.testnet.near.org -H 'Content-Type: application/json' -d "{\"jsonrpc\":\"2.0\",\"id\":\"1\",\"method\":\"query\",\"params\":{\"request_type\":\"call_function\",\"finality\":\"final\",\"account_id\":\"$1\",\"method_name\":\"$2\",\"args_base64\":\"$3\"}}" | python -c 'import json,sys; print(bytes(json.load(sys.stdin)["result"]["result"]).decode())'; }
```

The site's live check routes are up when `curl -s https://siyab.xyz/live/health` prints `{"ok": true, "network": "mainnet", "rate_per_minute": 60, "checks": true}`.

## The track's example prompt, point by point

1. Allow, warn or block, plus the reasons (`gate/rules.py:judge`, `gate/verdict.py:request`). An account named in the Rhea Finance incident report gets DENY with the label it carries:

```sh
curl -s https://siyab.xyz/live/account-check/rhea000453.multica.near | python -c "$F"
curl -s https://siyab.xyz/live/account-check/rhea000453.multica.near | python -c 'import json,sys; print(json.load(sys.stdin)["reasons"][1][:120])'
```
```
DENY refuse never receiver_exists=pass label_denylist=signal issuer_blacklist=pass account_age=pass dust_funded=pass lookalike=pass contract_control=pass incident_code=pass
rhea000453.multica.near carries the incident label "named in the rekt.news Rhea Finance report as an account within Rhea
```

2. Account age and activity history, and 3. whether the receiver is a contract (`gate/rules.py:_tier2`, `gate/rules.py:_code_check`). A known contract account gets ALLOW, with the age and code checks named. No transaction history is read beyond the creation transaction, and an allowlisted receiver skips these checks (README, Limits):

```sh
curl -s https://siyab.xyz/live/account-check/wrap.near | python -c "$F"
```
```
ALLOW sign never receiver_exists=pass label_denylist=pass issuer_blacklist=pass account_age=pass dust_funded=pass lookalike=pass code_without_metadata=pass contract_control=pass incident_code=pass
```

A token gets its own check: `curl -s https://siyab.xyz/live/token-check/wrap.near | python -c "$F"` prints `ALLOW sign never token_in_catalog=pass ft_metadata_sane=pass symbol_impersonation=pass contract_control=pass incident_code=pass`. A recent code change is not read at verdict time (README, Limits).

4. Prior interactions. The owner's allowlist lives on the Gate (`contracts/gate/src/policy.rs:set_allowlist`); the testnet registry keeps the latest verdict per subject, readable without a key (`contracts/registry/src/lib.rs:latest`). The rules read no payment history between this agent and this receiver, and do not read the registry (README, Limits) (the argument is `{"subject":"kestrelridge-shop.testnet"}` in base64):

```sh
view registry.kestrelridge-main.testnet latest eyJzdWJqZWN0Ijoia2VzdHJlbHJpZGdlLXNob3AudGVzdG5ldCJ9
view registry.kestrelridge-main.testnet total_records e30=
```
```
{"verdict":"Allow","evidence_hash":"4b99f7ed...fc84bf29","observed_block":275907012,"recorded_ms":1793716241660,"verdict_id":"652c1e0ebd165a413dd0489dea6a75ea"}
7
```

5. A shared blocklist with evidence attached (`intel/labels.py:validate`, `contracts/reports/src/lib.rs:submit_report`). Every label names its provenance class and its source; a document source carries its URL and the sha256 of the document fetched. Reports filed on the testnet reports contract carry an evidence URL and hash:

```sh
curl -s https://siyab.xyz/labels/rhea000453.multica.near | python -c 'import json,sys; a=json.load(sys.stdin)["assertions"][0]; print(a["category"], a["provenance"], a["source"]["url"])'
curl -s https://siyab.xyz/live/reports/recent | python -c 'import json,sys; [print(r["id"], r["subject"], r["status"], r["evidence_url"].rsplit("/", 1)[1], r["evidence_sha256"][:8] + "..." + r["evidence_sha256"][-8:]) for r in json.load(sys.stdin)["reports"]]'
```
```
incident third_party https://rekt.news/rhea-finance-rekt
1 kestrelridge-mallory.testnet overturned synthetic-report.md d07ec137...b2ce140a
0 kestrelridge-mallory.testnet overturned synthetic-report.md d07ec137...b2ce140a
```

6. A wrapper that refuses on block and asks a human on warn: `python -m gate pay` (`gate/agent.py:pay`) exits 1 on DENY and 3 on an ask. It sends transactions, so check it offline from a clone with `python -m unittest tests.test_agent.PayTest.test_pay_sends_nothing_on_deny tests.test_agent.PayTest.test_unknown_asks_a_human_and_sends_nothing tests.test_agent.PayTest.test_an_allow_the_gate_refuses_asks_a_human_and_sends_nothing`, which ends `Ran 3 tests` and `OK`.

7. A function call key scoped to one contract (`contracts/gate/src/lib.rs:request_transfer`). The testnet Gate holds one full access key, the operator's, and one agent key that may call only `request_transfer` on the Gate itself:

```sh
curl -s https://rpc.testnet.near.org -H 'Content-Type: application/json' -d '{"jsonrpc":"2.0","id":"1","method":"query","params":{"request_type":"view_access_key_list","finality":"final","account_id":"kestrelridge-main.testnet"}}' | python -c 'import json,sys; [print(k["access_key"]["permission"]) for k in json.load(sys.stdin)["result"]["keys"]]'
```
```
FullAccess
{'FunctionCall': {'allowance': '998171686460891800000000', 'method_names': ['request_transfer'], 'receiver_id': 'kestrelridge-main.testnet'}}
```

8. A prompt injected agent tries to pay a known bad account and gets stopped. The refused testnet submission: [`REFUSED:no_verdict`](https://testnet.nearblocks.io/txns/8M5NqTdAHGpLbGoRWzw9oo4M1foshWB4FkNKNer6yVMS). A scripted agent stands in for the injected model; `python -m gate injected` replays the log offline (below).

The Gate's version, policy and deployed code hash, to compare with `python -m gate codehash res/gate.wasm`; more testnet transactions with explorer links are in the README sections "What the track asks for, and where it is" and "Try it live (network)":

```sh
view kestrelridge-main.testnet version e30=
view kestrelridge-main.testnet get_policy e30=
curl -s https://rpc.testnet.near.org -H 'Content-Type: application/json' -d '{"jsonrpc":"2.0","id":"1","method":"query","params":{"request_type":"view_account","finality":"final","account_id":"kestrelridge-main.testnet"}}' | python -c 'import json,sys; print(json.load(sys.stdin)["result"]["code_hash"])'
```
```
"0.2.2"
{"denylist":[],"max_ttl_ms":300000,"paused":false}
5EmM2UQRYcvaNvJX6jfcgtNS7Mm8ZPjZhwK1J8bMXrcu
```

## Offline, from a clone

```sh
git clone --depth 1 --branch hackathon-submission https://github.com/Ridwannurudeen/siyab
cd siyab
python -m gate injected
python -m gate demo --offline
python -m gate codehash res/gate.wasm
python -m intel counts
python -m gate incident rhea
```

Python 3.11 or later, no network and no keys; each exits 0. `injected` prints `DENIED.`, `Nothing was signed.` and `injected: refused`; `demo --offline` prints one ALLOW verdict, two `DENIED.` lines and `UNKNOWN, not signed: account_age, dust_funded could not run`; `codehash` prints `code_hash: 5EmM2UQRYcvaNvJX6jfcgtNS7Mm8ZPjZhwK1J8bMXrcu`, the testnet Gate's code hash above. `demo/site/` (5,574 tracked files) is the generated static site, written by `python -m intel render` from committed files; it is the bulk of the clone.

## What was built in the window

The first commit is 29 Oct 2026 (tag `pre-hackathon`); everything under `contracts/`, `gate/`, `intel/` and `mcp/` was built between then and the tag. The diff: https://github.com/Ridwannurudeen/siyab/compare/pre-hackathon...hackathon-submission. Changes after the deadline: https://github.com/Ridwannurudeen/siyab/compare/hackathon-submission...main. The reused baseline is listed in the README section "New in this hackathon vs reused".
