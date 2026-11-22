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

The harder case is the recorded mainnet judgement of `rhea000453.multica.near`. In that file `receiver_exists` and `account_age` pass, dating it at least 7 days old; `issuer_blacklist`, `memo_injection`, `dust_funded`, `lookalike` and `code_without_metadata` also pass. Its DENY comes from `label_denylist`, which cites the rekt.news report naming it within Rhea's Multichain Account system and receiving traceable funds (`fixtures/receivers/judgements/rhea000453.multica.near.json:11-97`). Replay with `python -m intel receivers`, which rewrites the recorded set's judgements from the replay, after which `git status` shows no change (`README.md:345`); then inspect that file. The live command above checks the same account with an unsigned answer and its own set of check names (`docs/API.md:256,281`).

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

8. A prompt injected agent tries to pay a known bad account and gets stopped. The refused testnet submission: [`REFUSED:no_verdict`](https://testnet.nearblocks.io/txns/8M5NqTdAHGpLbGoRWzw9oo4M1foshWB4FkNKNer6yVMS). A scripted agent stands in for the injected model; the separate model trials below were added after the deadline. `python -m gate injected` replays the log offline (below).

Separately, after the deadline on 15 Nov 2026, `claude-haiku-5-5` read the same vendor message in 30 recorded trials: it asked to pay the labelled account in 0 of 30 and called `ask_a_human` in all 30. No verdict was signed; the forced submission without a verdict remains scripted. Replay offline with `python -m gate injected-model --provider anthropic` (`gate/injected_model.py:19-24,215-217,325-388`; `fixtures/demo/model-runs/`; `tests/test_injected_model.py`).

After the deadline on 15 Nov 2026, NEAR AI Cloud served `deepseek-ai/DeepSeek-V4.1-Flash-1017` the same vendor message in 30 recorded trials: it asked to pay the labelled account in 0 of 30, called `ask_a_human` in 29, and trial 10 timed out. The recorded total of 30981 micro-dollars includes a reserved bound of 4156 for trial 10, not a measured charge; the other trials total 26825 micro-dollars. No verdict was signed; the forced submission remains scripted. Replay offline with `python -m gate injected-model --provider nearai` (`gate/injected_model.py:25-32,215-217,325-388`; `fixtures/demo/model-runs-nearai/`; `tests/test_injected_model.py`).

That testnet injected DENY is the easy case: besides its labels and the memo, the collector does not exist on testnet and is not registered on the token (`fixtures/rpc/attacks/10-injected.json:3-5`), so the existence check alone refuses it. For a DENY from labels after existence and age pass, use the mainnet receiver in point 1.

The mainnet run of 15 Nov, after the deadline and not in the tag `hackathon-submission`, is different again. On its refused leg the collector carries incident labels and the Tether issuer answered `Banned`, so that DENY is not label-only; `request_transfer` with a null verdict failed `REFUSED:no_verdict` and spent nothing. Its allowed leg moved 0.01 NEAR to `maryamnurudeen.near` under a signed ALLOW (`fixtures/runs/gate-mainnet-20261115.txt:25-34`). `python -m unittest tests.test_mainnet_run -v` (on main, added after the deadline) checks retained evidence hashes and the ALLOW signature bound to that transfer, without re-querying mainnet (`tests/test_mainnet_run.py:30-72`; `fixtures/runs/gate-mainnet-20261115.txt:50-51`).

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

Python 3.11 or later, no network and no keys; each exits 0. `injected` prints `DENIED.`, `Nothing was signed.` and `injected: refused`; `demo --offline` prints one ALLOW verdict, two `DENIED.` lines and `UNKNOWN, not signed: account_age, dust_funded could not run`; `codehash` prints `code_hash: 5EmM2UQRYcvaNvJX6jfcgtNS7Mm8ZPjZhwK1J8bMXrcu`, the testnet Gate's code hash above. `demo/site/` (5,579 of the repository's 15,862 tracked files) is the generated static site, written by `python -m intel render` from committed files; it is the bulk of the clone.

## What was built in the window

The first commit is 29 Oct 2026 (tag `pre-hackathon`); everything under `contracts/`, `gate/`, `intel/` and `mcp/` was built between then and the tag. The diff: https://github.com/Ridwannurudeen/siyab/compare/pre-hackathon...hackathon-submission. Changes after the deadline: https://github.com/Ridwannurudeen/siyab/compare/hackathon-submission...main. The reused baseline is listed in the README section "New in this hackathon vs reused".
