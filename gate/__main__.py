"""Command line: python -m gate <command>."""

import argparse
import io
import json
import os
import sys
from pathlib import Path

from gate import (
    agent,
    base58,
    canon,
    demo,
    ed25519,
    incident,
    incident_replay,
    injected,
    injected_model,
    intents,
    oneclick,
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
    if args.live and args.policy is not None:
        print("--live cannot be combined with --policy; live signing reads the Gate policy; nothing was signed",
              file=sys.stderr)
        return 2
    try:
        policy = json.loads(args.policy if args.policy is not None else '{"allowlist": [], "caps": []}')
    except ValueError as error:
        print(f"--policy is not JSON: {error}", file=sys.stderr)
        return 2
    if args.live:
        try:
            canon._hex_field(args.agent_pk, 64, "agent_pk_hex")
        except canon.CanonError as error:
            print(f"invalid --agent-pk: {error}; nothing was signed", file=sys.stderr)
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
    if args.live:
        try:
            policy = agent.read_policy_views(client, args.gate, base58.encode_key(bytes.fromhex(args.agent_pk)))
        except (*rules.UNAVAILABLE, KeyError, TypeError) as error:
            print(f"cannot read Gate policy: {error}; nothing was signed", file=sys.stderr)
            return 2
        if policy is None:
            print(f"the agent key is not registered on {args.gate}; nothing was signed", file=sys.stderr)
            return 2
    issuers = None if args.gate.endswith(".testnet") else rules.issuers_for("mainnet")
    reports_account = os.environ.get("REPORTS_ACCOUNT_ID")
    try:
        result = verdict.request(args.kind, args.token, args.receiver, args.amount, args.memo, gate=args.gate,
                                 agent_pk_hex=args.agent_pk, policy=policy, store=labels.load(), rpc=client,
                                 nearblocks=nearblocks, signer=verdict_signer, injection_scanner=scanner,
                                 archival=archival, evidence_dir=args.evidence_dir, issuers=issuers,
                                 reports_account=reports_account)
    except signer.SignerError as error:
        print(f"refused: {error}", file=sys.stderr)
        return 2
    except ValueError as error:
        print(f"invalid request: {error}", file=sys.stderr)
        return 2
    for check in result["checks"]:
        print(f"{check['status']:<7} {check['id']}: {check['detail']}")
    print(f"verdict: {result['outcome']}")
    for reason in result["reasons"]:
        print(f"  {reason}")
    print(f"action: {result['action']}")
    if result["outcome"] == "ALLOW" and result["action"] == "ask":
        for reason in result["ask"]:
            print(f"  ask: {reason}")
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


def _http(args):
    # gate.http imports _clients from this module, so it is imported here and not with the others.
    from gate import http
    return http.serve(args.host, args.port)


def _intents(args):
    mode = "off" if args.live else os.environ.get("GATE_FIXTURES_MODE") or "replay"
    deadline = intents.RECORDED_DEADLINE if mode == "replay" else intents.live_deadline()
    if args.deposit_address is not None and args.refund_to is None:
        print("refused: --refund-to is required with --deposit-address; the address is bound to the refund "
              "account the caller names", file=sys.stderr)
        return 1
    refund_to = intents.REFUND_TO if args.refund_to is None else args.refund_to
    try:
        app_fees = None if args.app_fees is None else json.loads(args.app_fees)
        result = intents.judge_quote(args.origin, args.destination, args.amount, args.recipient, args.recipient_type,
                                     mode=mode, firewall_url=os.environ.get("SHIELDBOT_API_URL"), deadline=deadline,
                                     refund_to=refund_to, refund_type=args.refund_type, app_fees=app_fees)
    except ValueError as error:
        print(f"invalid request: {error}", file=sys.stderr)
        return 2
    except RecursionError:
        print("invalid request: the request is nested too deeply to read", file=sys.stderr)
        return 2
    quote_request = intents.quote_body(args.origin, args.destination, args.amount, args.recipient,
                                       args.recipient_type, deadline, refund_to=refund_to,
                                       refund_type=args.refund_type, app_fees=app_fees)
    try:
        intents.verify_binding(result, quote_request)
        if args.deposit_address is not None:
            intents.bind_deposit_address(result, quote_request, args.deposit_address, mode=mode)
            intents.verify_binding(result, quote_request)
    except ValueError as error:
        print(f"refused: {error}", file=sys.stderr)
        return 1
    quote = result["quote"] or {}
    print(f"dry quote: {args.amount} of {args.origin} to {args.destination}, recipient {args.recipient}, "
          f"amountOut {quote.get('amountOut', 'not known')}")
    if args.deposit_address is None:
        print("deposit address not checked: a dry quote has no deposit address")
    for check in result["checks"]:
        print(f"{check['status']:<7} {check['id']}: {check['detail']}")
    print(f"verdict: {result['verdict']} (unsigned preflight)")
    for reason in result["reasons"]:
        print(f"  {reason}")
    print(f"evidence: {result['evidence_hash']}")
    print("Nothing was signed, nothing was deposited.")
    return 0


def _oneclick(args):
    try:
        if args.command == "intents-resume":
            result = oneclick.resume(args.report, polls=args.polls)
        elif args.command == "intents-release":
            result = oneclick.release(args.sender, args.report)
        else:
            result = oneclick.settle(args.sender, args.destination, args.amount, args.recipient,
                                     min_amount_out=args.min_amount_out, live=args.deposit, key_file=args.key_file,
                                     out=args.out, polls=args.polls, override_reports=args.override_report,
                                     firewall_url=os.environ.get("SHIELDBOT_API_URL"))
    except (*intents.UNAVAILABLE, TypeError, KeyError, AttributeError, RecursionError) as error:
        print(f"refused: {error}", file=sys.stderr)
        return 2
    print(f"verdict: {result['verdict']}; phase: {result['phase']}")
    print(result["reason"])
    if result["deposit_hash"]:
        print(f"original deposit: {result['deposit_hash']}")
    if "storage_deposit" in result:
        print(f"storage_deposit: {result['storage_deposit']} yoctoNEAR")
    report = args.out if args.command == "intents-settle" else args.report
    if report:
        print(f"report: {Path(report).as_posix()}")
    preview = (args.command == "intents-settle" and not args.deposit
               and result["verdict"] == "ASK" and result["phase"] == "ready")
    released = args.command == "intents-release" and result.get("registry_released") is True
    return 0 if result["verdict"] == "ALLOW" or preview or released else 1


def _verify(args):
    try:
        ok, lines = verify.verify(args.record, args.evidence, args.verdict, args.key)
    except (OSError, ValueError, KeyError) as error:
        print(f"cannot verify: {error}", file=sys.stderr)
        return 1
    for line in lines:
        print(line)
    return 0 if ok else 1


def _verify_receipt(args):
    try:
        lines = verify.verify_receipt(args.file)
    except (OSError, ValueError, TypeError, RecursionError) as error:
        print(f"not verified: {error}")
        return 1
    for line in lines:
        print(line)
    return 0


def _agent(args):
    """pay and attack: the agent key on GATE_ACCOUNT_ID, testnet only."""
    gate, key_file = os.environ.get("GATE_ACCOUNT_ID"), os.environ.get("AGENT_KEY_FILE")
    reports_account = os.environ.get("REPORTS_ACCOUNT_ID")
    try:
        if args.command == "attack" and args.name == "injected":
            if not args.message:
                raise agent.AgentError("attack injected reads the transfer from --message")
            if any((args.kind, args.token, args.receiver, args.amount, args.memo)):
                raise agent.AgentError(
                    "attack injected reads the transfer from --message; do not pass transfer options"
                )
        elif not all((args.kind, args.receiver, args.amount)):
            raise agent.AgentError("set --kind, --receiver and --amount")
        if not gate or not key_file:
            raise agent.AgentError("set GATE_ACCOUNT_ID and AGENT_KEY_FILE")
        if not gate.endswith(".testnet"):
            raise agent.AgentError(f"the agent sends on testnet only, and {gate} is not a .testnet account")
        public_hex, public_key, secret = agent.agent_key(key_file)
        if args.command == "attack" and args.name == "injected":
            try:
                message = Path(args.message).read_text(encoding="utf-8")
            except OSError as error:
                raise agent.AgentError(f"cannot read --message: {error}") from error
            try:
                fields = injected.parse_message(message)
            except ValueError as error:
                raise agent.AgentError(str(error)) from error
            request = agent.transfer_request(**fields, gate=gate, agent_pk_hex=public_hex)
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
            return agent.pay(context, request, args.verdict_file, reports_account=reports_account)
        return agent.attack(args.name, context, request, reports_account=reports_account)
    except rpc.FixtureMiss:
        print("not sent: pay reads the Gate live: set GATE_FIXTURES_MODE=off", file=sys.stderr)
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
    # A piped or redirected stdout writes the locale encoding (cp1252 on Windows), which has no byte for
    # many characters a third party string can carry; those print as backslash escapes, never a crash.
    if isinstance(sys.stdout, io.TextIOWrapper):
        sys.stdout.reconfigure(errors="backslashreplace")
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
    judge.add_argument("--policy", help="the agent policy as JSON in the get_agent shape; cannot be used with --live")
    judge.add_argument("--live", action="store_true",
                       help="sign with the PyNaCl key in VERIFIER_KEY_FILE instead of the TEST key")
    judge.add_argument("--evidence-dir", default=verdict.EVIDENCE_DIR,
                       help="where the evidence document <hash>.json goes (default fixtures/evidence)")
    judge.add_argument("--out", default=".",
                       help="where the verdict file goes: <verdict_id>.verdict.json on ALLOW, "
                            "<evidence hash>.verdict.json otherwise (default the current directory)")
    judge.set_defaults(handler=_verdict)

    endpoint = commands.add_parser("http", help="serve unsigned verdicts on loopback behind API keys "
                                                 "(SHIELDBOT_API_KEYS_FILE); signs nothing")
    endpoint.add_argument("--host", default="127.0.0.1")
    endpoint.add_argument("--port", type=int, default=8788)
    endpoint.set_defaults(handler=_http)

    injected_demo = commands.add_parser(
        "injected",
        help=("offline replay of a scripted agent following a vendor message instruction, standing in for a "
              "prompt injected model; no model runs"),
    )
    injected_demo.set_defaults(handler=lambda args: injected.main())

    model_demo = commands.add_parser(
        "injected-model",
        help=("offline replay of recorded trials in which a hosted language model read the same vendor message; "
              "each pay call is judged again on recorded testnet reads"),
    )
    model_demo.add_argument("--record", type=int, metavar="N",
                            help="send N verified NEAR AI trials with NEARAI_API_KEY_FILE and SIYAB_NEARAI_LIVE=1; "
                                 "capped at 30 trials and $0.50")
    model_demo.add_argument("--provider", choices=("anthropic", "nearai"), default="anthropic")
    model_demo.set_defaults(handler=lambda args: injected_model.main(args.record, provider=args.provider))

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
    leg.add_argument("--deposit-address", help="the deposit address from the real quote to check")
    leg.add_argument("--refund-to", help="the refund account of the real quote; required with --deposit-address")
    leg.add_argument("--refund-type", default=intents.REFUND_TYPE)
    leg.add_argument("--app-fees", help='the real quote\'s app fees as JSON, [{"recipient": ..., "fee": ...}]')
    leg.add_argument("--live", action="store_true",
                     help="fetch a live dry quote and labels instead of replaying fixtures; writes nothing")
    leg.set_defaults(handler=_intents)

    settlement = commands.add_parser("intents-settle", allow_abbrev=False,
                                     help="check a wNEAR swap and stop before funding; --deposit funds once")
    settlement.add_argument("--sender", required=True, help="the mainnet NEAR account that holds wNEAR and receives refunds")
    settlement.add_argument("--destination", required=True, help="the destination EVM asset id from the 1Click catalog")
    settlement.add_argument("--amount", required=True, help="wNEAR base units, at most 250000000000000000000000")
    settlement.add_argument("--recipient", required=True, help="the destination EVM recipient")
    settlement.add_argument("--min-amount-out", required=True, help="the owner's minimum receipt in destination base units")
    settlement.add_argument("--deposit", action="store_true", help="sign and send one mainnet deposit, including capped receiver registration if needed")
    settlement.add_argument("--key-file", help="external near-cli Ed25519 FullAccess key file; read only with --deposit")
    settlement.add_argument("--out", help="new external report file; required with --deposit, never overwrites")
    settlement.add_argument("--override-report", action="append", metavar="REPORT",
                            help="explicitly bypass one unresolved report in the account registry or --out directory; repeat to name each")
    settlement.add_argument("--polls", type=int, default=60, help="status reads, 1 to 120; resume the same report after timeout")
    settlement.set_defaults(handler=_oneclick)

    settlement_resume = commands.add_parser("intents-resume", help="read and poll an original deposit report; sends nothing")
    settlement_resume.add_argument("report", help="the external report written by intents-settle")
    settlement_resume.add_argument("--polls", type=int, default=60, help="status reads, 1 to 120")
    settlement_resume.set_defaults(handler=_oneclick)

    settlement_release = commands.add_parser("intents-release", allow_abbrev=False,
                                             help="read proof then release one account registry row; never signs or sends")
    settlement_release.add_argument("--sender", required=True, help="the mainnet NEAR account that reserved the report")
    settlement_release.add_argument("--report", required=True, help="the external report to verify before releasing its row")
    settlement_release.set_defaults(handler=_oneclick)

    check = commands.add_parser("verify", help="check a registry record against its evidence file, offline")
    check.add_argument("record", help="a registry record: the latest view or the verdict_recorded event data")
    check.add_argument("evidence", help="the evidence document the record's evidence_hash names")
    check.add_argument("--verdict", help="the Verdict object file, to check its signature")
    check.add_argument("--key", help="the verifier public key as ed25519:<base58>")
    check.set_defaults(handler=_verify)

    receipt = commands.add_parser("verify-receipt", help="check a payment check receipt against its evidence, offline")
    receipt.add_argument("file", help="the downloaded payment check answer JSON")
    receipt.set_defaults(handler=_verify_receipt)

    page = commands.add_parser("incident", help="print an incident page from intel/incidents")
    page.add_argument("id", help="the incident id, such as rhea")
    page.add_argument("--json", action="store_true", help="print the incident file itself")
    page.set_defaults(handler=lambda args: incident.main(args.id, args.json))

    replay = commands.add_parser("incident-replay", help="replay the guard at an incident block, with no labels")
    replay.add_argument("id", help="the incident id, such as rhea")
    replay.add_argument("--at", help="UTC ISO time; defaults to midnight on the incident date")
    replay.add_argument("--record", action="store_true", help="record archival reads and the run; uses the network")
    replay.set_defaults(handler=lambda args: incident_replay.main(args.id, at=args.at, record=args.record))

    for name, text in (("pay", ("pay on testnet through the Gate: judge the transfer, then send request_transfer "
                                 "only on a signed ALLOW")),
                       ("attack", ("the simulation switch: send one transaction the Gate or the protocol must "
                                   "refuse, and keep its answer"))):
        sender = commands.add_parser(name, help=text)
        if name == "attack":
            sender.add_argument("name", choices=agent.ATTACKS)
            sender.add_argument("--message",
                                help=("for attack injected: the scripted agent follows an instruction planted in this "
                                      "vendor message, standing in for a prompt injected model"))
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
                       help="the block height to record when evidence observed.block_height is absent; "
                            "must equal it when present")
    batch.add_argument("--evidence-dir", default=verdict.EVIDENCE_DIR,
                       help="where the evidence documents are (default fixtures/evidence)")
    batch.set_defaults(handler=_registry_args)

    args = parser.parse_args(argv)
    return args.handler(args)


if __name__ == "__main__":
    sys.exit(main())
