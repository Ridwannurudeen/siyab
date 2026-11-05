"""The agent: python -m gate pay, and python -m gate attack <name> as the simulation switch.

pay judges one transfer through gate.verdict.request, signing with the live verifier key in
VERIFIER_KEY_FILE (or reads a verdict file the service wrote), and submits request_transfer to the
Gate only when the verdict is a signed ALLOW. It has no way to skip or weaken the verdict: a DENY or
an UNKNOWN is printed and nothing is sent. The agent key lives on the Gate, so every transaction is
signed as GATE_ACCOUNT_ID with the function call key in AGENT_KEY_FILE.

attack <name> sends one transaction that the Gate or the protocol must refuse, and keeps the
answer: the failed receipt, or the RPC error JSON when the protocol rejects the transaction and no
receipt exists. The attacks are the rows of the adversarial table; each verdict an attack forges
or replays is built only on this path.

Transports: py-near (Account.function_call; the default) or near-cli-rs through subprocess
(--via near-cli), with the command line recorded in docs/SPIKES.md. Verified on testnet on 29 Oct:
py-near signing with a function call key, and its rejection errors, whose JSON is sometimes missing.
UNVERIFIED: the near-cli-rs output the transaction hash and logs are read from.
"""

import asyncio
import dataclasses
import hashlib
import json
import os
import re
import secrets
import subprocess
import time
from pathlib import Path

from gate import base58, canon, rpc, signer, verdict
from intel import labels

GAS = 30 * 10**12
EXPLORER_URL = "https://testnet.nearblocks.io/txns/"
ATTACKS = ("no-verdict", "forged-signature", "replay", "expired", "over-cap", "callback", "token-direct",
           "service-down", "one-yocto", "unknown-payee", "injected")
DAY_MS = 24 * 60 * 60 * 1000
VERDICT_KEYS = {"literal", "issued_ms", "expires_ms", "verdict_id", "evidence_hash", "key_id", "signature"}
_SIGNATURE = re.compile(r"[0-9a-f]{128}")
_HEX64 = re.compile(r"[0-9a-f]{64}")
_VERDICT_ID = re.compile(r"[0-9a-f]{32}")
# Bounds one near-cli-rs send, which signs, broadcasts and waits for the final outcome.
NEAR_CLI_TIMEOUT_S = 120
# near-cli-rs prints the hash as "Transaction ID: <base58>"; UNVERIFIED until the first testnet run.
_CLI_HASH = re.compile(r"Transaction ID: ([1-9A-HJ-NP-Za-km-z]{43,44})")


class AgentError(Exception):
    """A payment or an attack that cannot be sent: a missing setting, key or verdict."""


@dataclasses.dataclass
class Agent:
    gate: str
    public_hex: str
    public_key: str
    transport: object
    clients: tuple
    verifier: object
    out_dir: Path
    clock: object = time.time


def agent_key(path):
    """(public key hex, public key ed25519:<base58>, secret ed25519:<base58>) of a near-cli key file."""
    seed, public = signer._read_key_file(path)
    return public.hex(), base58.encode_key(public), base58.encode_key(seed + public)


def live_signer():
    """The verifier signer the service uses on testnet: PyNaCl over the key in VERIFIER_KEY_FILE."""
    if not os.environ.get("VERIFIER_KEY_FILE"):
        raise signer.SignerError("the live verdict is signed with the key file VERIFIER_KEY_FILE names, and it "
                                 "is unset")
    return signer.NaClSigner(os.environ["VERIFIER_KEY_FILE"])


def transfer_request(kind, token_id, receiver_id, amount, memo, *, gate, agent_pk_hex):
    """The TransferRequest argument, validated as the canonical message would be."""
    amount = canon.validate_transfer(gate=gate, agent_pk_hex=agent_pk_hex, kind=kind, token_id=token_id,
                                     receiver_id=receiver_id, amount=amount, memo=memo)
    return {"kind": kind, "token_id": token_id, "receiver_id": receiver_id, "amount": amount, "memo": memo}


def action(receiver_id, method_name, args, deposit=0):
    """One function call the agent key signs."""
    return {"receiver_id": receiver_id, "method_name": method_name, "args": args, "gas": GAS, "deposit": deposit}


class PyNearTransport:
    """Sends an action with py-near, signed as the Gate with the agent's secret key."""

    def __init__(self, gate, secret, rpc_url):
        try:
            from py_near.account import Account
            from py_near.exceptions.provider import JsonProviderError
        except ImportError as error:
            raise AgentError("py-near is not installed; run from the venv ~/.venvs/near or pass --via "
                             "near-cli") from error
        self._account_class, self._gate, self._secret, self._rpc_url = Account, gate, secret, rpc_url
        self._rejected = JsonProviderError

    def _new_account(self):
        """A fresh py-near account: its session is bound to the event loop of the one send that uses it."""
        account = self._account_class(account_id=self._gate, private_key=self._secret, rpc_addr=self._rpc_url)
        # py-near waits up to 600 s per request by default; the provider reads .timeout when it opens its
        # session on the first request.
        account._provider.timeout = rpc.TIMEOUT_S
        return account

    async def _send(self, sent):
        account = self._new_account()
        try:
            await account.startup()
            result = await account.function_call(sent["receiver_id"], sent["method_name"], sent["args"],
                                                 gas=sent["gas"], amount=sent["deposit"])
        except self._rejected as error:
            # py-near leaves error_json None when it cannot parse the RPC error (DepositWithFunctionCall).
            rpc_error = error.error_json or {"error": type(error).__name__, "message": str(error)}
            return {"transaction_hash": getattr(error, "trx_hash", None), "status": None, "logs": [],
                    "rpc_error": rpc_error}
        finally:
            await account.shutdown()
        return {"transaction_hash": result.transaction.hash, "status": result.status, "logs": result.logs,
                "rpc_error": None}

    def __call__(self, sent):
        return asyncio.run(self._send(sent))


class NearCliTransport:
    """Sends an action with near-cli-rs, signed as the Gate with the agent's key file."""

    def __init__(self, gate, key_file, run=subprocess.run):
        self.gate = gate
        self.key_file = str(key_file)
        self._run = run

    def argv(self, sent):
        deposit = "0 NEAR" if sent["deposit"] == 0 else f"{sent['deposit']} yoctoNEAR"
        return ["near", "contract", "call-function", "as-transaction", sent["receiver_id"], sent["method_name"],
                "json-args", json.dumps(sent["args"], separators=(",", ":")), "prepaid-gas",
                f"{sent['gas'] // 10**12} Tgas", "attached-deposit", deposit, "sign-as", self.gate, "network-config",
                "testnet", "sign-with-access-key-file", self.key_file, "send"]

    def __call__(self, sent):
        try:
            done = self._run(self.argv(sent), capture_output=True, text=True, timeout=NEAR_CLI_TIMEOUT_S,
                             check=False)
        except FileNotFoundError as error:
            raise AgentError("near-cli-rs (near) is not on PATH") from error
        text = done.stdout + done.stderr
        found = _CLI_HASH.search(text)
        return {"transaction_hash": found.group(1) if found else None, "status": {"returncode": done.returncode},
                "logs": [line.strip() for line in text.splitlines() if "EVENT_JSON:" in line],
                "rpc_error": None, "stdout": done.stdout, "stderr": done.stderr}


def _succeeded(outcome):
    status = outcome["status"]
    return isinstance(status, dict) and ("SuccessValue" in status or status.get("returncode") == 0)


def _submit(agent, sent, out):
    """Send one action and print what went out and what came back."""
    out(f"sent: {sent['method_name']} to {sent['receiver_id']} as {agent.gate}, deposit {sent['deposit']} "
        f"yoctoNEAR, gas {sent['gas'] // 10**12} Tgas")
    out(f"args: {json.dumps(sent['args'])}")
    outcome = agent.transport(sent)
    if outcome["transaction_hash"] and outcome["rpc_error"] is None:
        out(f"transaction: {outcome['transaction_hash']}")
        out(f"explorer: {EXPLORER_URL}{outcome['transaction_hash']}")
    for log in outcome["logs"]:
        out(f"log: {log}")
    if outcome["rpc_error"] is not None:
        out(f"rejected, no receipt: {json.dumps(outcome['rpc_error'])}")
    else:
        out(f"status: {json.dumps(outcome['status'])}")
    return {"sent": sent, "outcome": outcome}


def _write(agent, name, document, out):
    agent.out_dir.mkdir(parents=True, exist_ok=True)
    path = agent.out_dir / f"{name}.json"
    path.write_bytes((json.dumps(document, indent=2) + "\n").encode("utf-8"))
    out(f"wrote {path.as_posix()}")
    return path


def read_policy(agent):
    """The agent's policy from the Gate's get_agent view, in the shape the rules read."""
    policy = agent.clients[0].call_function(agent.gate, "get_agent", {"public_key": agent.public_key})
    if policy is None:
        raise AgentError(f"the agent key {agent.public_key} is not registered on {agent.gate}")
    return policy


def _judge(agent, request, verdict_signer, out, policy=None):
    """The verdict service's answer for the request: gate.verdict.request with the agent's policy."""
    client, nearblocks, scanner, archival = agent.clients
    result = verdict.request(request["kind"], request["token_id"], request["receiver_id"], request["amount"],
                             request["memo"], gate=agent.gate, agent_pk_hex=agent.public_hex,
                             policy=read_policy(agent) if policy is None else policy, store=labels.load(), rpc=client,
                             nearblocks=nearblocks,
                             signer=verdict_signer, injection_scanner=scanner, archival=archival)
    for check in result["checks"]:
        out(f"{check['status']:<7} {check['id']}: {check['detail']}")
    out(f"verdict: {result['outcome']}")
    for reason in result["reasons"]:
        out(f"  {reason}")
    out(f"evidence: {result['evidence_hash']}")
    out(f"latency: {result['latency_ms']} ms")
    return result


def _allow(agent, request, out):
    """A signed ALLOW for the request from the service, or AgentError naming the outcome."""
    result = _judge(agent, request, agent.verifier(), out)
    if result["outcome"] != "ALLOW":
        raise AgentError(f"the verdict is {result['outcome']}, not ALLOW; nothing was sent")
    return result["verdict"]


def _signed_allow(verdict_object):
    """True for a Verdict object in the interface shape: exactly its seven keys, ALLOW, a 32 hex verdict id
    and a 128 hex signature."""
    if not isinstance(verdict_object, dict) or set(verdict_object) != VERDICT_KEYS:
        return False
    verdict_id, signature = verdict_object["verdict_id"], verdict_object["signature"]
    return (verdict_object["literal"] == canon.LITERAL and isinstance(verdict_id, str)
            and bool(_VERDICT_ID.fullmatch(verdict_id)) and isinstance(signature, str)
            and bool(_SIGNATURE.fullmatch(signature)))


def pay(agent, request, verdict_file=None, out=print):
    """Submit request_transfer with the service's signed ALLOW; 0 when the transaction succeeded."""
    policy = None
    result = None
    if verdict_file:
        verdict_object = json.loads(Path(verdict_file).read_text(encoding="utf-8"))
        outcome = verdict_object.get("literal") if isinstance(verdict_object, dict) else None
        evidence_hash = verdict_object.get("evidence_hash") if isinstance(verdict_object, dict) else None
        out(f"verdict file {verdict_file}: {outcome}, evidence {evidence_hash}")
    else:
        policy = read_policy(agent)
        result = _judge(agent, request, agent.verifier(), out, policy=policy)
        verdict_object, outcome, evidence_hash = result["verdict"], result["outcome"], result["evidence_hash"]
    if outcome != canon.LITERAL or not _signed_allow(verdict_object):
        if outcome == canon.LITERAL:
            out("not sent: the ALLOW is not a signed Verdict object (literal, issued_ms, expires_ms, verdict_id, "
                "evidence_hash, key_id and a 128 hex signature)")
        else:
            out(f"not sent: the verdict is {outcome}, and the Gate pays only on a signed ALLOW")
        if outcome == "UNKNOWN":
            if result:
                unknown_checks = [check["id"] for check in result["checks"] if check["status"] == "unknown"]
                count = len(unknown_checks)
                unit = "check" if count == 1 else "checks"
                out(f"ask a human: {count} {unit} could not run: {', '.join(unknown_checks)}")
                if request["receiver_id"] not in policy["allowlist"]:
                    allowlist = list(dict.fromkeys([*policy["allowlist"], request["receiver_id"]]))
                    out(f"the owner's step, when the payee is known to them: bash scripts/set_allowlist.sh "
                        f"{agent.public_key} {','.join(allowlist)}")
                    out("scripts/set_allowlist.sh needs OWNER_ACCOUNT_ID, GATE_ACCOUNT_ID, and NEAR_KEYS_DIR")
            else:
                out("ask a human: the verdict file carries no check details")
            out("nothing is sent until then")
        hashed = isinstance(evidence_hash, str) and bool(_HEX64.fullmatch(evidence_hash))
        name = f"pay-{evidence_hash}" if hashed else "pay-not-sent"
        _write(agent, name, {"command": "pay", "gate": agent.gate, "request": request, "verdict": verdict_object,
                             "submitted": False}, out)
        if outcome == "UNKNOWN":
            return 3
        return 1
    record = _submit(agent, action(agent.gate, "request_transfer", {"request": request, "verdict": verdict_object}),
                     out)
    _write(agent, f"pay-{verdict_object['verdict_id']}", {"command": "pay", "gate": agent.gate, "submitted": True,
                                                          **record}, out)
    failed = [log for log in record["outcome"]["logs"] if "transfer_failed" in log]
    if failed:
        event = json.loads(failed[0].partition("EVENT_JSON:")[2])
        if event["data"][0]["restored"]:
            out("the transfer failed after it was scheduled: the Gate restored the budget, the verdict stays used, "
                "and the payment needs a fresh ALLOW")
        else:
            out("the transfer failed after it was scheduled: the agent or its cap changed before the callback, so "
                "nothing was restored, the verdict stays used, and the payment needs a fresh ALLOW")
        return 1
    return 0 if _succeeded(record["outcome"]) else 1


def _attack_evidence(name, request):
    """The hash an attack's own verdict names: sha256 of the attack and the request, no evidence document."""
    text = json.dumps({"attack": name, "request": request}, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _attack_signer(agent, name, request, verdict_signer, issued_ms, expires_ms):
    """A verdict built outside the rules for one attack: right shape, chosen times and key."""
    verdict_id = secrets.token_hex(16)
    evidence_hash = _attack_evidence(name, request)
    message = canon.canonical_message(
        gate=agent.gate, agent_pk_hex=agent.public_hex, kind=request["kind"], token_id=request["token_id"],
        receiver_id=request["receiver_id"], amount=request["amount"], memo=request["memo"], issued_ms=issued_ms,
        expires_ms=expires_ms, verdict_id=verdict_id, evidence_sha256=evidence_hash, key_id=verdict_signer.key_id)
    return {"literal": canon.LITERAL, "issued_ms": issued_ms, "expires_ms": expires_ms, "verdict_id": verdict_id,
            "evidence_hash": evidence_hash, "key_id": verdict_signer.key_id,
            "signature": verdict_signer.sign(canon.digest(message)).hex()}


def _attack_transfer(agent, request, verdict_object):
    return action(agent.gate, "request_transfer", {"request": request, "verdict": verdict_object})


def _attack_actions(name, agent, request, out):
    """(the actions to send, notes for the record) for one attack."""
    now_ms = int(agent.clock() * 1000)
    if name == "injected":
        result = _judge(agent, request, agent.verifier(), out)
        if result["outcome"] != "DENY":
            outcome = result["outcome"]
            raise AgentError(f"the verdict service answered {outcome} for the injected request, not DENY; "
                             "nothing was sent")
        return [_attack_transfer(agent, request, None)], [
            "the verdict service answered DENY: " + " ".join(result["reasons"]),
            "the agent submits without a verdict"]
    if name == "no-verdict":
        return [_attack_transfer(agent, request, None)], []
    if name == "forged-signature":
        forged = _attack_signer(agent, name, request, signer.TestSigner(), now_ms, now_ms + verdict.DEFAULT_TTL_MS)
        return [_attack_transfer(agent, request, forged)], ["signed with the TEST key, which the Gate does not trust"]
    if name == "replay":
        allow = _allow(agent, request, out)
        return [_attack_transfer(agent, request, allow), _attack_transfer(agent, request, allow)], [
            "one ALLOW sent twice; the first send is a real payment, the second must be refused"]
    if name == "expired":
        expires_ms = now_ms - DAY_MS
        old = _attack_signer(agent, name, request, agent.verifier(), expires_ms - verdict.DEFAULT_TTL_MS, expires_ms)
        return [_attack_transfer(agent, request, old)], ["signed with the live key, expired a day ago"]
    if name in ("over-cap", "unknown-payee", "one-yocto"):
        sent = _attack_transfer(agent, request, _allow(agent, request, out))
        if name == "one-yocto":
            sent["deposit"] = 1
        return [sent], []
    if name == "callback":
        return [action(agent.gate, "on_transfer", {"request_id": 0})], []
    if name == "token-direct":
        if request["token_id"] is None:
            raise AgentError("token-direct needs --kind ft and --token")
        args = {"receiver_id": request["receiver_id"], "amount": request["amount"], "memo": request["memo"]}
        return [action(request["token_id"], "ft_transfer", args)], []
    if name == "service-down":
        unreachable = agent.out_dir / "service-down" / "no-verifier-key.json"
        try:
            signer.NaClSigner(unreachable)
        except signer.SignerError as error:
            note = f"the verdict service could not start its signer ({error}), so the agent submits without a verdict"
            out(note)
            return [_attack_transfer(agent, request, None)], [note]
        raise AgentError(f"a verifier key exists at {unreachable}, so this is not a service down run")
    raise AgentError(f"no attack {name!r}; the attacks are {', '.join(ATTACKS)}")


def attack(name, agent, request, out=print):
    """Send one attack, print each answer and write <out>/<name>.json; 0 when every send was refused."""
    out(f"attack {name}: a simulation the Gate or the protocol must refuse")
    sends, notes = _attack_actions(name, agent, request, out)
    records = [_submit(agent, sent, out) for sent in sends]
    checked = records[1:] if name == "replay" else records
    refused = not any(_succeeded(record["outcome"]) for record in checked)
    out(f"{name}: {'refused' if refused else 'NOT REFUSED'}")
    _write(agent, name, {"attack": name, "gate": agent.gate, "notes": notes, "refused": refused,
                         "submissions": records}, out)
    return 0 if refused else 1
