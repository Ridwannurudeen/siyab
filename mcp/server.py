"""A stdio MCP server with three tools: python -m mcp.server, run from the repository root.

JSON-RPC 2.0 messages arrive on stdin one per line; when the first bytes are a Content-Length
header, every message is read and answered in that framed form instead. The methods are
initialize, notifications/initialized, ping, tools/list and tools/call; an unknown method with an
id answers the error -32601, and a notification never gets an answer. Only JSON-RPC frames reach
stdout: anything else the code prints goes to stderr.

The tools replay recorded fixtures unless GATE_FIXTURES_MODE says record or off:

    check_near_payment   gate.verdict.request for one transfer, signed with the TEST key on ALLOW
    check_intents_quote  gate.intents.judge_quote, the unsigned preflight on a 1Click quote
    scan_near_account    gate.scan.scan, key hygiene, verifier keys and labels of an account

check_near_payment binds the verdict to GATE_ACCOUNT_ID (default gate.near) and the TEST agent key,
judges against GATE_POLICY (JSON in the get_agent shape; default no allowlist, so every receiver
gets the off allowlist checks too) and writes its evidence into a temporary directory.
"""

import json
import os
import sys
import tempfile

from gate import canon, ed25519, intents, rpc, scan, signer, verdict
from gate.__main__ import _clients
from intel import labels

PROTOCOL_VERSIONS = ("2024-11-05", "2025-03-26", "2025-06-18")
FALLBACK_VERSION = "2024-11-05"
SERVER_INFO = {"name": "shieldbot-near", "version": "0.1.0"}
DEFAULT_GATE = "gate.near"
DEFAULT_POLICY = '{"allowlist": [], "caps": []}'
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


def _check_near_payment(arguments, evidence_dir):
    gate = os.environ.get("GATE_ACCOUNT_ID") or DEFAULT_GATE
    try:
        policy = json.loads(os.environ.get("GATE_POLICY") or DEFAULT_POLICY)
    except ValueError as error:
        raise ToolError(f"GATE_POLICY is not JSON: {error}") from error
    client, nearblocks, scanner = _clients(gate)
    try:
        result = verdict.request(arguments.get("kind"), arguments.get("token"), arguments.get("receiver"),
                                 arguments.get("amount"), arguments.get("memo"), gate=gate,
                                 agent_pk_hex=ed25519.public_from_seed(ed25519.TEST_AGENT_SEED).hex(),
                                 policy=policy, store=labels.load(), rpc=client, nearblocks=nearblocks,
                                 signer=signer.TestSigner(), injection_scanner=scanner, evidence_dir=evidence_dir)
    except canon.CanonError as error:
        raise ToolError(f"invalid request: {error}") from error
    return {**result, "gate": gate, "signer": TEST_KEY_NOTE}


def _check_intents_quote(arguments, evidence_dir):
    replay = (os.environ.get("GATE_FIXTURES_MODE") or "replay") == "replay"
    try:
        result = intents.judge_quote(arguments.get("origin_asset"), arguments.get("destination_asset"),
                                     arguments.get("amount"), arguments.get("recipient"),
                                     arguments.get("recipient_type") or "DESTINATION_CHAIN",
                                     firewall_url=os.environ.get("SHIELDBOT_API_URL"),
                                     deadline=intents.RECORDED_DEADLINE if replay else intents.live_deadline())
    except ValueError as error:
        raise ToolError(f"invalid request: {error}") from error
    return {key: result[key] for key in ("verdict", "checks", "reasons", "quote", "evidence_hash")}


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
        if name not in HANDLERS:
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
        yield True, stream.read(length)


def serve(stdin, stdout):
    evidence_dir = tempfile.mkdtemp(prefix="shieldbot-near-mcp-")
    print(f"shieldbot-near MCP server on stdio; evidence documents go to {evidence_dir}", file=sys.stderr)
    for framed, body in _messages(stdin):
        try:
            message = json.loads(body)
        except ValueError as error:
            reply = {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": f"parse error: {error}"}}
        else:
            reply = handle(message, evidence_dir)
        if reply is None:
            continue
        data = json.dumps(reply, ensure_ascii=False).encode("utf-8")
        stdout.write(b"Content-Length: %d\r\n\r\n" % len(data) + data if framed else data + b"\n")
        stdout.flush()
    if not os.listdir(evidence_dir):
        os.rmdir(evidence_dir)
    return 0


def main():
    stdout = sys.stdout.buffer
    sys.stdout = sys.stderr
    return serve(sys.stdin.buffer, stdout)


if __name__ == "__main__":
    sys.exit(main())
