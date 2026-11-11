"""A stdio MCP server with three tools: python -m mcp.server, run from the repository root.

JSON-RPC 2.0 messages arrive on stdin one per line; when the first bytes are a Content-Length
header, every message is read and answered in that framed form instead. The methods are
initialize, notifications/initialized, ping, tools/list and tools/call; an unknown method with an
id answers the error -32601, and a notification never gets an answer. Only JSON-RPC frames reach
stdout: anything else the code prints goes to stderr.

The tools replay recorded fixtures unless GATE_FIXTURES_MODE says record or off:

    check_near_payment   gate.verdict.request for one transfer, signed only when its action is sign
                         with the TEST key or the live key
    check_intents_quote  gate.intents.judge_quote, the unsigned preflight on a 1Click quote
    scan_near_account    gate.scan.scan, key hygiene, verifier keys and labels of an account

check_near_payment binds the verdict to GATE_ACCOUNT_ID (default gate.near) and writes its evidence
into a temporary directory, removed when the server exits unless GATE_KEEP_EVIDENCE=1. By default it
binds the verdict to the TEST agent key, judges against GATE_POLICY (JSON in the get_agent shape;
default no allowlist and no unknown payee caps, so every ALLOW for a payee off the allowlist answers
action: ask with REFUSED:not_allowlisted) and signs only an ALLOW whose action is sign with the TEST key. It signs
with the live key in VERIFIER_KEY_FILE (PyNaCl) only when
GATE_LIVE_SIGNING=1, GATE_FIXTURES_MODE=off, GATE_ACCOUNT_ID ends .testnet, GATE_POLICY is unset and
AGENT_PUB holds the agent public key: the verdict is then bound to AGENT_PUB and judged against the
Gate's get_agent and get_policy for that key, read at every call. With the flag set, any other condition missing is
a tool error and nothing is signed; there is no fall back to the TEST key. The result names the key
that signed.
"""

import json
import os
import shutil
import sys
import tempfile

from gate import agent, base58, canon, ed25519, intents, rpc, rules, scan, signer, verdict
from gate.__main__ import _clients
from intel import labels

PROTOCOL_VERSIONS = ("2024-11-05", "2025-03-26", "2025-06-18")
FALLBACK_VERSION = "2024-11-05"
SERVER_INFO = {"name": "siyab", "version": "0.1.0"}
DEFAULT_GATE = "gate.near"
DEFAULT_POLICY = '{"allowlist": [], "caps": []}'
LIVE_FLAG = "GATE_LIVE_SIGNING"
KEEP_EVIDENCE = "GATE_KEEP_EVIDENCE"
NOTHING_SIGNED = "; nothing was signed"
MAX_FRAME_LENGTH = 1048576
TEST_KEY_NOTE = ("an ALLOW here is signed with the TEST verifier key, which is derived from a published seed "
                 "and is never registered on testnet, so no Gate pays on it")

TOOLS = [
    {
        "name": "check_near_payment",
        "description": "Judge one NEAR agent payment (receiver, token, amount, memo) and return ALLOW, DENY or "
                       "UNKNOWN with every check, the reasons and the evidence hash. Unknown is never a pass.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "kind": {"type": "string", "enum": list(canon.KINDS), "description": "near or ft"},
                "token": {"type": "string", "description": "the NEP-141 token account, for kind ft"},
                "receiver": {"type": "string", "description": "the receiving NEAR account id"},
                "amount": {"type": "string", "description": "the amount in base units, as a decimal string"},
                "memo": {"type": "string", "description": "the transfer memo, if any"},
            },
            "required": ["kind", "receiver", "amount"],
        },
    },
    {
        "name": "check_intents_quote",
        "description": "Judge where a NEAR Intents 1Click quote sends money on an EVM chain: a dry quote, the "
                       "recipient's theft labels and its code. An unsigned preflight; nothing is deposited.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "origin_asset": {"type": "string", "description": "the 1Click origin asset id"},
                "destination_asset": {"type": "string", "description": "the 1Click destination asset id"},
                "amount": {"type": "string", "description": "the amount in the origin asset's base units"},
                "recipient": {"type": "string", "description": "the 0x recipient on the destination chain"},
                "recipient_type": {"type": "string", "description": "default DESTINATION_CHAIN"},
                "deposit_address": {"type": "string", "description": "optional address from a real quote; checked by a read only status lookup"},
            },
            "required": ["origin_asset", "destination_asset", "amount", "recipient"],
        },
    },
    {
        "name": "scan_near_account",
        "description": "Access key hygiene of a NEAR account: full access keys, keys with an empty method_names, "
                       "dead and unlimited allowances, a Gate's agent keys and verifier keys, and its labels.",
        "inputSchema": {
            "type": "object",
            "properties": {"account_id": {"type": "string", "description": "the NEAR account id"}},
            "required": ["account_id"],
        },
    },
]


class ToolError(Exception):
    """A tool call that ran and failed; answered as isError content."""


def _live_signing():
    """(the live signer, AGENT_PUB, its hex) when GATE_LIVE_SIGNING=1 and every live condition holds;
    None without the flag. With the flag, a condition that does not hold is a ToolError."""
    if os.environ.get(LIVE_FLAG) != "1":
        return None
    mode = os.environ.get("GATE_FIXTURES_MODE") or "replay"
    if mode != "off":
        raise ToolError(f"live signing needs GATE_FIXTURES_MODE=off, not {mode}: a live key never signs over "
                        f"fixtures{NOTHING_SIGNED}")
    gate = os.environ.get("GATE_ACCOUNT_ID") or DEFAULT_GATE
    if not gate.endswith(".testnet"):
        raise ToolError(f"live signing is for a .testnet Gate only, not {gate}{NOTHING_SIGNED}")
    if os.environ.get("GATE_POLICY"):
        raise ToolError(f"live signing reads the policy from get_agent; unset GATE_POLICY{NOTHING_SIGNED}")
    key_file = os.environ.get("VERIFIER_KEY_FILE")
    if not key_file:
        raise ToolError(f"live signing needs VERIFIER_KEY_FILE{NOTHING_SIGNED}")
    agent_pub = os.environ.get("AGENT_PUB")
    if not agent_pub:
        raise ToolError(f"live signing needs AGENT_PUB, the agent public key as ed25519:<base58>{NOTHING_SIGNED}")
    try:
        agent_raw = base58.decode_key(agent_pub)
        if len(agent_raw) != 32:
            raise ValueError(f"{len(agent_raw)} bytes")
    except ValueError as error:
        raise ToolError(f"AGENT_PUB is not a 32 byte ed25519 key: {agent_pub}{NOTHING_SIGNED}") from error
    try:
        live_signer = signer.NaClSigner(key_file)
    except signer.SignerError as error:
        raise ToolError(f"no live signer: {error}{NOTHING_SIGNED}") from error
    return live_signer, agent_pub, agent_raw.hex()


def _live_policy(client, gate, public_key):
    """The agent policy and Gate transfer settings, read live at every call."""
    try:
        policy = agent.read_policy_views(client, gate, public_key)
    except rules.UNAVAILABLE as error:
        raise ToolError(f"cannot read Gate policy on {gate}: {error}{NOTHING_SIGNED}") from error
    if policy is None:
        raise ToolError(f"the agent key {public_key} is not registered on {gate}{NOTHING_SIGNED}")
    return policy


def _check_near_payment(arguments, evidence_dir):
    live = _live_signing()
    gate = os.environ.get("GATE_ACCOUNT_ID") or DEFAULT_GATE
    if live is None:
        try:
            policy = json.loads(os.environ.get("GATE_POLICY") or DEFAULT_POLICY)
        except ValueError as error:
            raise ToolError(f"GATE_POLICY is not JSON: {error}") from error
        verdict_signer, policy_source = signer.TestSigner(), "GATE_POLICY"
        agent_pk_hex = ed25519.public_from_seed(ed25519.TEST_AGENT_SEED).hex()
    client, nearblocks, scanner, archival = _clients(gate)
    if live is not None:
        verdict_signer, agent_pub, agent_pk_hex = live
        policy, policy_source = _live_policy(client, gate, agent_pub), "get_agent"
    reports_account = os.environ.get("REPORTS_ACCOUNT_ID")
    try:
        result = verdict.request(arguments.get("kind"), arguments.get("token"), arguments.get("receiver"),
                                 arguments.get("amount"), arguments.get("memo"), gate=gate,
                                 agent_pk_hex=agent_pk_hex, policy=policy, store=labels.load(), rpc=client,
                                 nearblocks=nearblocks, signer=verdict_signer, injection_scanner=scanner,
                                 archival=archival, evidence_dir=evidence_dir,
                                 issuers=None if gate.endswith(".testnet") else rules.issuers_for("mainnet"),
                                 reports_account=reports_account)
    except canon.CanonError as error:
        raise ToolError(f"invalid request: {error}") from error
    public_key = base58.encode_key(bytes.fromhex(verdict_signer.public_key_hex))
    signed_by = {"kind": "TEST" if live is None else "live", "key_id": verdict_signer.key_id,
                 "public_key": public_key, "public_key_hex": verdict_signer.public_key_hex}
    if live is None:
        signed_by["note"] = TEST_KEY_NOTE
    elif result["action"] == "sign":
        print(f"live ALLOW on {gate} for the agent key {agent_pub}, signed with the verifier key {public_key}",
              file=sys.stderr)
    return {**result, "gate": gate, "signer": signed_by, "agent_pk": agent_pk_hex, "policy_source": policy_source}


def _check_intents_quote(arguments, evidence_dir):
    mode = os.environ.get("GATE_FIXTURES_MODE") or "replay"
    try:
        recipient_type = arguments.get("recipient_type") or "DESTINATION_CHAIN"
        result = intents.judge_quote(arguments.get("origin_asset"), arguments.get("destination_asset"),
                                     arguments.get("amount"), arguments.get("recipient"), recipient_type,
                                     firewall_url=os.environ.get("SHIELDBOT_API_URL"),
                                     mode=mode,
                                     deadline=intents.RECORDED_DEADLINE if mode == "replay" else intents.live_deadline())
    except (ValueError, RecursionError) as error:
        raise ToolError(f"invalid request: {error}") from error
    quote_request = {"originAsset": arguments.get("origin_asset"),
                     "destinationAsset": arguments.get("destination_asset"), "amount": arguments.get("amount"),
                     "recipient": arguments.get("recipient"), "recipientType": recipient_type}
    deposit_address = arguments.get("deposit_address")
    try:
        intents.verify_binding(result, quote_request)
        if deposit_address is not None:
            intents.bind_deposit_address(result, quote_request, deposit_address, mode=mode)
            intents.verify_binding(result, quote_request)
    except ValueError as error:
        raise ToolError(f"refused: {error}") from error
    answer = {key: result[key] for key in ("verdict", "checks", "reasons", "quote", "document", "evidence_hash")}
    answer["deposit_address_check"] = ("checked by a read only 1Click status lookup" if deposit_address is not None
                                       else "not checked: a dry quote has no deposit address")
    return answer


def _scan_near_account(arguments, evidence_dir):
    account_id = arguments.get("account_id")
    try:
        canon.account_id(account_id, "account_id")
    except canon.CanonError as error:
        raise ToolError(f"invalid request: {error}") from error
    client = rpc.Client(_clients(account_id)[0].url, fixtures_dir=scan.FIXTURES_DIR)
    return scan.scan(account_id, client=client, store=labels.load())


HANDLERS = {"check_near_payment": _check_near_payment, "check_intents_quote": _check_intents_quote,
            "scan_near_account": _scan_near_account}


def _content(value, is_error):
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    return {"content": [{"type": "text", "text": text}], "isError": is_error}


def handle(message, evidence_dir):
    """The response object for one parsed message, or None for a notification."""
    if not isinstance(message, dict) or message.get("jsonrpc") != "2.0" or not isinstance(message.get("method"), str):
        return {"jsonrpc": "2.0", "id": message.get("id") if isinstance(message, dict) else None,
                "error": {"code": -32600, "message": "not a JSON-RPC 2.0 request"}}
    if "id" not in message:
        return None
    params = message.get("params") if isinstance(message.get("params"), dict) else {}
    method, reply = message["method"], {"jsonrpc": "2.0", "id": message["id"]}
    if method == "initialize":
        requested = params.get("protocolVersion")
        reply["result"] = {"protocolVersion": requested if requested in PROTOCOL_VERSIONS else FALLBACK_VERSION,
                           "capabilities": {"tools": {}}, "serverInfo": SERVER_INFO}
    elif method == "ping":
        reply["result"] = {}
    elif method == "tools/list":
        reply["result"] = {"tools": TOOLS}
    elif method == "tools/call":
        name, arguments = params.get("name"), params.get("arguments") or {}
        if not isinstance(name, str):
            reply["error"] = {"code": -32602, "message": "tool name must be a string"}
        elif name not in HANDLERS:
            reply["error"] = {"code": -32602, "message": f"unknown tool: {name}"}
        elif not isinstance(arguments, dict):
            reply["result"] = _content("arguments must be an object", True)
        else:
            try:
                reply["result"] = _content(HANDLERS[name](arguments, evidence_dir), False)
            except ToolError as error:
                reply["result"] = _content(str(error), True)
            except Exception as error:  # noqa: BLE001 (a tool failure is answered as isError, never a crash)
                print(f"tool {name} failed: {type(error).__name__}: {error}", file=sys.stderr)
                reply["result"] = _content(f"the tool failed: {type(error).__name__}: {error}", True)
    else:
        reply["error"] = {"code": -32601, "message": f"method not found: {method}"}
    return reply


def _messages(stream):
    """(framed, body bytes) for each message on the stream, newline delimited or Content-Length framed."""
    first = stream.peek(1)[:15] if hasattr(stream, "peek") else b""
    framed = first.lower().startswith(b"content-length")
    while True:
        if not framed:
            line = stream.readline()
            if not line:
                return
            if line.strip():
                yield False, line
            continue
        length = None
        while True:
            header = stream.readline()
            if not header:
                return
            if not header.strip():
                break
            try:
                name, _, value = header.decode("ascii").partition(":")
                if name.strip().lower() == "content-length":
                    length = int(value.strip())
            except (ValueError, UnicodeDecodeError) as error:
                print(f"skipped a header that does not parse: {error}", file=sys.stderr)
        if length is None:
            return
        if length < 0 or length > MAX_FRAME_LENGTH:
            print(f"skipped a Content-Length outside 0..{MAX_FRAME_LENGTH}", file=sys.stderr)
            return
        yield True, stream.read(length)


def serve(stdin, stdout):
    evidence_dir = tempfile.mkdtemp(prefix="siyab-mcp-")
    print(f"Siyab MCP server on stdio; evidence documents go to {evidence_dir}, removed at exit unless "
          f"{KEEP_EVIDENCE}=1", file=sys.stderr)
    try:
        for framed, body in _messages(stdin):
            try:
                message = json.loads(body)
            except (UnicodeError, ValueError, RecursionError) as error:
                reply = {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": f"parse error: {error}"}}
            else:
                reply = handle(message, evidence_dir)
            if reply is None:
                continue
            data = json.dumps(reply, ensure_ascii=False).encode("utf-8")
            stdout.write(b"Content-Length: %d\r\n\r\n" % len(data) + data if framed else data + b"\n")
            stdout.flush()
    finally:
        if os.environ.get(KEEP_EVIDENCE) != "1" or not os.listdir(evidence_dir):
            shutil.rmtree(evidence_dir)
    return 0


def main():
    stdout = sys.stdout.buffer
    sys.stdout = sys.stderr
    return serve(sys.stdin.buffer, stdout)


if __name__ == "__main__":
    sys.exit(main())
