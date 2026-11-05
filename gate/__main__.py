"""Command line: python -m gate <command>."""

import argparse
import json
import os
import sys
from pathlib import Path

from gate import (
    agent,
    canon,
    demo,
    ed25519,
    incident,
    injected,
    intents,
    registry,
    rpc,
    rules,
    scan,
    signer,
    verdict,
    verify,
    wasm,
)
from intel import labels


def _canon_vectors(args):
    written, removed = canon.write_vectors(args.write, args.gate_root)
    for path in written:
        print(f"wrote {path.as_posix()}")
    for path in removed:
        print(f"removed {path.as_posix()}")
    return 0


def _clients(gate):
    """The NEAR RPC, NearBlocks, optional ShieldBot API and archival RPC clients for the Gate's network."""
    if gate.endswith(".testnet"):
        rpc_url = os.environ.get("NEAR_RPC_URL") or rpc.TESTNET_RPC_URL
        nearblocks_url = os.environ.get("NEARBLOCKS_API_URL") or rpc.NEARBLOCKS_TESTNET_URL
        archival_url = os.environ.get("NEAR_ARCHIVAL_RPC_URL") or rpc.ARCHIVAL_TESTNET_RPC_URL
    else:
        rpc_url = os.environ.get("NEAR_MAINNET_RPC_URL") or rpc.MAINNET_RPC_URL
        nearblocks_url = os.environ.get("NEARBLOCKS_API_URL") or rpc.NEARBLOCKS_URL
        archival_url = os.environ.get("NEAR_ARCHIVAL_RPC_URL") or rpc.ARCHIVAL_RPC_URL
    api_url = os.environ.get("SHIELDBOT_API_URL")
    scanner = rpc.Client(api_url, kind="http") if api_url else None
    return rpc.Client(rpc_url), rpc.Client(nearblocks_url, kind="http"), scanner, rpc.Client(archival_url)


def _verdict(args):
    try:
        policy = json.loads(args.policy)
    except ValueError as error:
        print(f"--policy is not JSON: {error}", file=sys.stderr)
        return 2
    try:
        if args.live:
            if not os.environ.get("VERIFIER_KEY_FILE"):
                raise signer.SignerError("--live signs with the key file VERIFIER_KEY_FILE names, and it is unset")
            verdict_signer = signer.NaClSigner(os.environ["VERIFIER_KEY_FILE"])
        else:
            verdict_signer = signer.TestSigner()
    except signer.SignerError as error:
        print(f"no signer: {error}", file=sys.stderr)
        return 2
    client, nearblocks, scanner, archival = _clients(args.gate)
    issuers = None if args.gate.endswith(".testnet") else rules.issuers_for("mainnet")
    try:
        result = verdict.request(args.kind, args.token, args.receiver, args.amount, args.memo, gate=args.gate,
                                 agent_pk_hex=args.agent_pk, policy=policy, store=labels.load(), rpc=client,
                                 nearblocks=nearblocks, signer=verdict_signer, injection_scanner=scanner,
                                 archival=archival, evidence_dir=args.evidence_dir, issuers=issuers)
    except canon.CanonError as error:
        print(f"invalid request: {error}", file=sys.stderr)
        return 2
    for check in result["checks"]:
        print(f"{check['status']:<7} {check['id']}: {check['detail']}")
    print(f"verdict: {result['outcome']}")
    print(f"action: {result['action']}")
    for reason in result["reasons"]:
        print(f"  {reason}")
    print(f"evidence: {result['evidence_hash']}")
    print(f"latency: {result['latency_ms']} ms")
    if result["verdict"]:
        name, body = result["verdict"]["verdict_id"], result["verdict"]
    else:
        name, body = result["evidence_hash"], {"literal": result["outcome"], "evidence_hash": result["evidence_hash"]}
    path = Path(args.out) / f"{name}.verdict.json"
    path.write_bytes((json.dumps(body, indent=2) + "\n").encode("utf-8"))
    print(json.dumps(body))
    print(f"wrote {path.as_posix()}")
    return 0


def _intents(args):
    mode = "off" if args.live else "replay"
    deadline = intents.live_deadline() if args.live else intents.RECORDED_DEADLINE
    try:
        result = intents.judge_quote(args.origin, args.destination, args.amount, args.recipient, args.recipient_type,
                                     mode=mode, firewall_url=os.environ.get("SHIELDBOT_API_URL"), deadline=deadline)
    except ValueError as error:
        print(f"invalid request: {error}", file=sys.stderr)
        return 2
    quote = result["quote"] or {}
    print(f"dry quote: {args.amount} of {args.origin} to {args.destination}, recipient {args.recipient}, "
          f"amountOut {quote.get('amountOut', 'not known')}")
    for check in result["checks"]:
        print(f"{check['status']:<7} {check['id']}: {check['detail']}")
    print(f"verdict: {result['verdict']} (unsigned preflight)")
    for reason in result["reasons"]:
        print(f"  {reason}")
    print(f"evidence: {result['evidence_hash']}")
    print("Nothing was signed, nothing was deposited.")
    return 0


def _verify(args):
    try:
        ok, lines = verify.verify(args.record, args.evidence, args.verdict, args.key)
    except (OSError, ValueError, KeyError) as error:
        print(f"cannot verify: {error}", file=sys.stderr)
        return 1
    for line in lines:
        print(line)
    return 0 if ok else 1


def _agent(args):
    """pay and attack: the agent key on GATE_ACCOUNT_ID, testnet only."""
    gate, key_file = os.environ.get("GATE_ACCOUNT_ID"), os.environ.get("AGENT_KEY_FILE")
    try:
        if args.command == "attack" and args.name == "injected":
            if not args.message or any((args.kind, args.receiver, args.amount)):
                raise agent.AgentError("attack injected reads the transfer from --message")
        elif not all((args.kind, args.receiver, args.amount)):
            raise agent.AgentError("set --kind, --receiver and --amount")
        if not gate or not key_file:
            raise agent.AgentError("set GATE_ACCOUNT_ID and AGENT_KEY_FILE")
        if not gate.endswith(".testnet"):
            raise agent.AgentError(f"the agent sends on testnet only, and {gate} is not a .testnet account")
        public_hex, public_key, secret = agent.agent_key(key_file)
        if args.command == "attack" and args.name == "injected":
            request = agent.transfer_request(**injected.parse_message(Path(args.message).read_text(encoding="utf-8")),
                                             gate=gate, agent_pk_hex=public_hex)
        else:
            request = agent.transfer_request(args.kind, args.token, args.receiver, args.amount, args.memo, gate=gate,
                                             agent_pk_hex=public_hex)
        if args.via == "near-cli":
            transport = agent.NearCliTransport(gate, key_file)
        else:
            transport = agent.PyNearTransport(gate, secret, os.environ.get("NEAR_RPC_URL") or rpc.TESTNET_RPC_URL)
        context = agent.Agent(gate=gate, public_hex=public_hex, public_key=public_key, transport=transport,
                              clients=_clients(gate), verifier=agent.live_signer, out_dir=Path(args.out))
        if args.command == "pay":
            return agent.pay(context, request, args.verdict_file)
        return agent.attack(args.name, context, request)
    except rpc.FixtureMiss:
        print("not sent: pay reads the Gate live: set GATE_FIXTURES_MODE=record or off", file=sys.stderr)
        return 2
    except (agent.AgentError, signer.SignerError, canon.CanonError) as error:
        print(f"not sent: {error}", file=sys.stderr)
        return 2


def _registry_args(args):
    try:
        batch, notes = registry.record_batch(args.verdicts, args.evidence_dir, args.observed_block)
    except registry.RegistryError as error:
        print(f"refused: {error}", file=sys.stderr)
        return 2
    for note in notes:
        print(note, file=sys.stderr)
    print(json.dumps(batch, indent=2))
    return 0


def _scan(args):
    if args.account.endswith(".testnet"):
        url = os.environ.get("NEAR_RPC_URL") or rpc.TESTNET_RPC_URL
    else:
        url = os.environ.get("NEAR_MAINNET_RPC_URL") or rpc.MAINNET_RPC_URL
    result = scan.scan(args.account, client=rpc.Client(url, fixtures_dir=scan.FIXTURES_DIR), store=labels.load())
    for line in result["lines"]:
        print(line)
    return 0


def _wasm_file(handler):
    """A wasm command: read the file, run handler on its bytes, exit 1 on a file that does not read or parse."""
    def run(args):
        try:
            lines = handler(Path(args.wasm).read_bytes())
        except (OSError, ValueError) as error:
            print(f"cannot read {args.wasm}: {error}", file=sys.stderr)
            return 1
        for line in lines:
            print(line)
        return 0
    return run


def _codehash(data):
    hex_digest, code_hash = wasm.codehash(data)
    return [f"sha256: {hex_digest}", f"code_hash: {code_hash}"]


def main(argv=None):
    parser = argparse.ArgumentParser(prog="python -m gate")
    commands = parser.add_subparsers(dest="command", required=True)

    vectors = commands.add_parser("canon-vectors", help="write the signed canonical message vectors")
    vectors.add_argument("--write", required=True, metavar="DIR",
                         help="vector directory; evidence and the TEST key go to its siblings")
    vectors.add_argument("--gate-root", default=canon.DEFAULT_GATE_ROOT, metavar="ACCOUNT",
                         help=f"sandbox root account id (default {canon.DEFAULT_GATE_ROOT})")
    vectors.set_defaults(handler=_canon_vectors)

    judge = commands.add_parser("verdict", help="judge one transfer and sign it only on ALLOW")
    judge.add_argument("--kind", required=True, choices=canon.KINDS)
    judge.add_argument("--token", help="the NEP-141 token account (kind ft)")
    judge.add_argument("--receiver", required=True)
    judge.add_argument("--amount", required=True, help="decimal amount in base units")
    judge.add_argument("--memo")
    judge.add_argument("--gate", required=True, help="the Gate account id; .testnet selects testnet")
    judge.add_argument("--agent-pk", default=ed25519.public_from_seed(ed25519.TEST_AGENT_SEED).hex(),
                       help="the agent public key as 64 lowercase hex (default TEST agent 1)")
    judge.add_argument("--policy", default='{"allowlist": [], "caps": []}',
                       help="the agent policy as JSON in the get_agent shape")
    judge.add_argument("--live", action="store_true",
                       help="sign with the PyNaCl key in VERIFIER_KEY_FILE instead of the TEST key")
    judge.add_argument("--evidence-dir", default=verdict.EVIDENCE_DIR,
                       help="where the evidence document <hash>.json goes (default fixtures/evidence)")
    judge.add_argument("--out", default=".",
                       help="where the verdict file goes: <verdict_id>.verdict.json on ALLOW, "
                            "<evidence hash>.verdict.json otherwise (default the current directory)")
    judge.set_defaults(handler=_verdict)

    injected_demo = commands.add_parser(
        "injected", help="a prompt injected agent, replayed offline: the message, the DENY, the refused submission")
    injected_demo.set_defaults(handler=lambda args: injected.main())

    show = commands.add_parser("demo", help="four verdicts over recorded testnet data, or the same four live")
    mode = show.add_mutually_exclusive_group(required=True)
    mode.add_argument("--offline", action="store_true", help="replay the fixtures under fixtures/demo")
    mode.add_argument("--live", action="store_true",
                      help="read testnet and a dry 1Click quote on mainnet; sends nothing, writes no fixture")
    show.set_defaults(handler=lambda args: demo.main(args.offline))

    leg = commands.add_parser("intents", help="judge where a 1Click quote sends NEAR money (dry quote, unsigned)")
    leg.add_argument("--origin", required=True, help="the 1Click origin asset id")
    leg.add_argument("--destination", required=True, help="the 1Click destination asset id")
    leg.add_argument("--amount", required=True, help="the amount in the origin asset's base units")
    leg.add_argument("--recipient", required=True, help="the 0x recipient on the destination chain")
    leg.add_argument("--recipient-type", default="DESTINATION_CHAIN")
    leg.add_argument("--live", action="store_true",
                     help="fetch a live dry quote and labels instead of replaying fixtures; writes nothing")
    leg.set_defaults(handler=_intents)

    check = commands.add_parser("verify", help="check a registry record against its evidence file, offline")
    check.add_argument("record", help="a registry record: the latest view or the verdict_recorded event data")
    check.add_argument("evidence", help="the evidence document the record's evidence_hash names")
    check.add_argument("--verdict", help="the Verdict object file, to check its signature")
    check.add_argument("--key", help="the verifier public key as ed25519:<base58>")
    check.set_defaults(handler=_verify)

    page = commands.add_parser("incident", help="print an incident page from intel/incidents")
    page.add_argument("id", help="the incident id, such as rhea")
    page.add_argument("--json", action="store_true", help="print the incident file itself")
    page.set_defaults(handler=lambda args: incident.main(args.id, args.json))

    for name, text in (("pay", ("pay on testnet through the Gate: judge the transfer, then send request_transfer "
                                 "only on a signed ALLOW")),
                       ("attack", ("the simulation switch: send one transaction the Gate or the protocol must "
                                   "refuse, and keep its answer"))):
        sender = commands.add_parser(name, help=text)
        if name == "attack":
            sender.add_argument("name", choices=agent.ATTACKS)
            sender.add_argument("--message",
                                help="for attack injected: the vendor message the agent follows; the transfer fields "
                                     "come from it")
        sender.add_argument("--kind", required=(name == "pay"), choices=canon.KINDS)
        sender.add_argument("--token", help="the NEP-141 token account (kind ft)")
        sender.add_argument("--receiver", required=(name == "pay"))
        sender.add_argument("--amount", required=(name == "pay"), help="decimal amount in base units")
        sender.add_argument("--memo")
        if name == "pay":
            sender.add_argument("--verdict-file", help="a verdict file the verdict service wrote with the live "
                                                       "key, instead of judging here")
        sender.add_argument("--via", choices=("py-near", "near-cli"), default="py-near",
                            help="sign and send with py-near (default) or with near-cli-rs")
        sender.add_argument("--out", required=True, metavar="DIR", help="where the outcome JSON goes")
        sender.set_defaults(handler=_agent)

    keys = commands.add_parser("scan", help="access key hygiene and verifier keys of an account, with its labels")
    keys.add_argument("account", help="the account id; .testnet selects testnet")
    keys.set_defaults(handler=_scan)

    digest = commands.add_parser("codehash", help="the sha256 of a wasm file in hex and as the base58 code_hash")
    digest.add_argument("wasm", help="the wasm file, such as res/gate.wasm")
    digest.set_defaults(handler=_wasm_file(_codehash))

    names = commands.add_parser("wasm-exports", help="the exported function names of a wasm file, sorted")
    names.add_argument("wasm", help="the wasm file, such as res/gate.wasm")
    names.set_defaults(handler=_wasm_file(wasm.exports))

    batch = commands.add_parser("registry-args",
                                help="print the registry record_batch arguments for verdict files")
    batch.add_argument("verdicts", nargs="+", metavar="verdict.json", help="files python -m gate verdict wrote")
    batch.add_argument("--observed-block", type=int, metavar="N",
                       help="the block height to record instead of the evidence observed.block_height")
    batch.add_argument("--evidence-dir", default=verdict.EVIDENCE_DIR,
                       help="where the evidence documents are (default fixtures/evidence)")
    batch.set_defaults(handler=_registry_args)

    args = parser.parse_args(argv)
    return args.handler(args)


if __name__ == "__main__":
    sys.exit(main())
