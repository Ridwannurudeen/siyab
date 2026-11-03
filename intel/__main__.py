"""Command line: python -m intel <command>.

receivers [--record] judges the recorded mainnet receiver set with gate.rules.judge in replay (an
empty allowlist and the committed label store) and writes one
fixtures/receivers/judgements/<subject>.json per subject of fixtures/receivers/set.json. The set
is the last COUNTERPARTIES distinct counterparties of each account in RECEIVER_SOURCES (the FastNEAR
Transfers API, newest first; implicit accounts are left out as the likeliest personal wallets,
unless the label store holds them as a catalog token), then the NEAR accounts of
intel/incidents/rhea.json, then the first CATALOG_SUBJECTS catalog_token NEAR accounts of the label
store that are not in the set yet. A token subject is judged as the token of a transfer to
TOKEN_RECEIVER, as intel/incidents/rhea.json judges its token contracts, and every other subject as
the receiver of a NEAR transfer.

--record chooses the set and fetches what is missing: the transfers pages, the view calls the
rules make, and the NearBlocks keys and creation transaction for the first AGE_SUBJECTS receivers
only (at most two NearBlocks calls each), so every later receiver without an earlier recording
reads unknown on age. The view calls and NearBlocks answers live under fixtures/receivers/rpc/.
Without --record nothing touches the network.
"""

import argparse
import json
import re
import sys
import time
from pathlib import Path

from gate import rpc, rules
from intel import api, cluster, feed, labels, render, seed, trace

ROOT = Path(__file__).resolve().parent.parent
RECEIVERS_DIR = ROOT / "fixtures" / "receivers"
FASTNEAR_DIR = ROOT / "fixtures" / "fastnear"
# The view calls and NearBlocks answers the receiver judgements read, kept apart from fixtures/rpc/.
RECEIVER_RPC_DIR = RECEIVERS_DIR / "rpc"
RHEA_INCIDENT = ROOT / "intel" / "incidents" / "rhea.json"
TRANSFERS_URL = "https://transfers.main.fastnear.com"
# The public accounts whose counterparties join the set, each with its role.
RECEIVER_SOURCES = (
    ("v2.ref-finance.near", "the Ref Finance v2 exchange contract (role from its documentation, UNVERIFIED)"),
    ("wrap.near", "the wrapped NEAR token contract, a catalog_token in the label store"),
)
COUNTERPARTIES = 15
TRANSFER_PAGES = 5
TRANSFER_LIMIT = 100
CATALOG_SUBJECTS = 10
AGE_SUBJECTS = 30
TOKEN_RECEIVER = "intents.near"
IMPLICIT = re.compile(r"[0-9a-f]{64}|0x[0-9a-f]{40}")
_TIME = "%Y-%m-%dT%H:%M:%SZ"


def _counts(args):
    print(labels.counts_line(labels.load().counts()))
    return 0


def _seed(args):
    added = seed.run(args.source)
    print(f"{added} new assertions from {args.source}")
    print(labels.counts_line(labels.load().counts()))
    return 0


def catalog_accounts(store):
    """The current catalog_token NEAR accounts of the store, in store order."""
    accounts = []
    for assertion in store.assertions:
        if (assertion.category == "catalog_token" and assertion.chain == "near" and assertion.superseded_by is None
                and assertion.subject not in accounts):
            accounts.append(assertion.subject)
    return accounts


def counterparties(client, account, catalog):
    """(the last COUNTERPARTIES distinct counterparties of account, newest first, the request keys)."""
    found, keys, resume_token = [], [], None
    for _ in range(TRANSFER_PAGES):
        body = {"account_id": account, "limit": TRANSFER_LIMIT, "desc": True}
        if resume_token is not None:
            body["resume_token"] = resume_token
        page = client.post("/v0/transfers", body)
        if not isinstance(page, dict) or not isinstance(page.get("transfers"), list):
            raise rpc.MalformedResponse("the transfers page has no transfers list")
        keys.append(client.last_source["request_sha256"])
        for transfer in page["transfers"]:
            other = transfer.get("other_account_id")
            if (not isinstance(other, str) or other in found or other in (account, "system")
                    or (IMPLICIT.fullmatch(other) and other not in catalog)):
                continue
            found.append(other)
            if len(found) == COUNTERPARTIES:
                return found, keys
        resume_token = page.get("resume_token")
        if not resume_token:
            break
    return found, keys


def _request(subject, as_token):
    if as_token:
        return {"kind": "ft", "token_id": subject, "receiver_id": TOKEN_RECEIVER, "amount": "1000000", "memo": None}
    return {"kind": "near", "token_id": None, "receiver_id": subject, "amount": str(rules.YOCTO_PER_NEAR),
            "memo": None}


def choose(store, transfers_client):
    """(subjects [{subject, why, as, request}], sources [{account, role, requests}])."""
    catalog = catalog_accounts(store)
    rhea = json.loads(RHEA_INCIDENT.read_text(encoding="utf-8"))
    rhea_tokens = {entry["subject"] for entry in rhea["rules_today"] if entry["as"] == "token"}
    subjects, sources, seen = [], [], set()

    def add(subject, why, as_token):
        if subject not in seen:
            seen.add(subject)
            subjects.append({"subject": subject, "why": why, "as": "token" if as_token else "receiver",
                             "request": _request(subject, as_token)})

    for account, role in RECEIVER_SOURCES:
        found, keys = counterparties(transfers_client, account, catalog)
        sources.append({"account": account, "role": role, "requests": keys})
        for subject in found:
            add(subject, f"one of the last {COUNTERPARTIES} distinct counterparties of {account} in the FastNEAR "
                         "Transfers API", subject in catalog)
    for account in rhea["accounts"]:
        if account["chain"] == "near":
            add(account["subject"], f"named in the incident page rhea as {account['role_as_reported']}",
                account["subject"] in rhea_tokens)
    added = 0
    for account in catalog:
        if added < CATALOG_SUBJECTS and account not in seen:
            add(account, "a catalog_token NEAR account of the label store, in store order", True)
            added += 1
    return subjects, sources


def _judge(entry, store, client, nearblocks, now_ms):
    judgement = rules.judge(entry["request"], {"allowlist": [], "caps": []}, store, client, nearblocks,
                            now_ms=now_ms, injection_scanner=None, issuers=rules.issuers_for("mainnet"))
    return {"subject": entry["subject"], "as": entry["as"], "request": entry["request"],
            "verdict": judgement.verdict, "checks": judgement.checks, "reasons": judgement.reasons,
            "labels": judgement.labels, "observed": judgement.observed}


def _write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes((json.dumps(value, indent=2, ensure_ascii=False) + "\n").encode("utf-8"))


def receivers_rule():
    """The selection and recording rule set.json states: the two receivers paragraphs of this docstring."""
    return " ".join(" ".join(__doc__.split("\n\n")[1:3]).split())


def record_receivers(store, clock=time.time):
    """Choose the set and fetch what the rules read for it; returns the set document."""
    transfers_client = rpc.Client(TRANSFERS_URL, "record", kind="http", fixtures_dir=FASTNEAR_DIR)
    subjects, sources = choose(store, transfers_client)
    now_ms = int(clock() * 1000)
    client = rpc.Client(rpc.MAINNET_RPC_URL, "record", fixtures_dir=RECEIVER_RPC_DIR)
    aged = []
    for entry in subjects:
        receiver = entry["request"]["receiver_id"]
        if receiver not in aged and len(aged) < AGE_SUBJECTS:
            aged.append(receiver)
        nearblocks = rpc.Client(rpc.NEARBLOCKS_URL, "record" if receiver in aged else "replay", kind="http",
                                fixtures_dir=RECEIVER_RPC_DIR)
        _judge(entry, store, client, nearblocks, now_ms)
    for entry in subjects:
        entry["age_recorded"] = entry["request"]["receiver_id"] in aged
    return {"chosen_at": time.strftime(_TIME, time.gmtime(now_ms // 1000)), "judged_at_ms": now_ms,
            "rule": receivers_rule(), "sources": sources, "subjects": subjects}


def judge_receivers(document, store, directory=RECEIVERS_DIR):
    """Judge every subject of the set in replay and write its judgement file; returns the judgements."""
    client = rpc.Client(rpc.MAINNET_RPC_URL, "replay", fixtures_dir=RECEIVER_RPC_DIR)
    nearblocks = rpc.Client(rpc.NEARBLOCKS_URL, "replay", kind="http", fixtures_dir=RECEIVER_RPC_DIR)
    results = []
    for entry in document["subjects"]:
        result = _judge(entry, store, client, nearblocks, document["judged_at_ms"])
        _write_json(Path(directory) / "judgements" / f"{entry['subject']}.json", result)
        results.append(result)
    return results


def _receivers(args):
    store = labels.load()
    set_path = RECEIVERS_DIR / "set.json"
    if args.record:
        try:
            document = record_receivers(store)
        except rpc.HttpError as error:
            print(f"stopped: {error}; no key is ever added", file=sys.stderr)
            return 1
        _write_json(set_path, document)
    document = json.loads(set_path.read_text(encoding="utf-8"))
    verdicts = [result["verdict"] for result in judge_receivers(document, store)]
    print(f"On {len(verdicts)} recorded mainnet receivers the rules returned {verdicts.count('ALLOW')} ALLOW, "
          f"{verdicts.count('DENY')} DENY, {verdicts.count('UNKNOWN')} Unknown.")
    return 0


def _feed(args):
    try:
        state, events = feed.run(args.start, args.blocks, args.record)
    except (feed.FeedError, rpc.HttpError, rpc.TransportError) as error:
        print(f"stopped: {error}", file=sys.stderr)
        return 1
    print(f"{state['blocks']} blocks to height {state['last_height']}, {len(events)} new events")
    return 0


def _feed_stats(args):
    for line in feed.stats_lines(feed.load_state()):
        print(line)
    return 0


def _flow(args):
    try:
        return trace.main(args.subject, args.depth, args.max, args.name, args.record)
    except (rpc.FixtureMiss, rpc.HttpError, rpc.TransportError) as error:
        print(f"stopped: {error}", file=sys.stderr)
        return 1


def _render(args):
    pages = render.render(args.out, indexable=args.indexable)
    print(f"{len(pages)} pages written under {args.out}")
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(prog="python -m intel")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("counts", help="print the label store counts").set_defaults(handler=_counts)
    seeding = commands.add_parser("seed", help="seed the label store from one source")
    seeding.add_argument("source", choices=seed.SOURCES)
    seeding.set_defaults(handler=_seed)
    server = commands.add_parser("serve", help="serve the read only labels, incidents, evidence, flow and cluster API")
    server.add_argument("--host", default="127.0.0.1")
    server.add_argument("--port", type=int, default=8787)
    server.add_argument("--site", help="a directory of static files to serve too, such as demo/site")
    server.set_defaults(handler=lambda args: api.serve(args.host, args.port, args.site))
    receivers = commands.add_parser("receivers", help="judge the recorded mainnet receiver set in replay")
    receivers.add_argument("--record", action="store_true", help="choose the set and record what is missing")
    receivers.set_defaults(handler=_receivers)
    feeding = commands.add_parser("feed", help="run the three signals over finalized mainnet blocks")
    feeding.add_argument("--from", dest="start", type=int, help="the first height (default: after the state)")
    feeding.add_argument("--blocks", type=int, required=True, help="how many heights to walk")
    feeding.add_argument("--record", action="store_true", help="fetch and reduce the blocks not recorded yet")
    feeding.set_defaults(handler=_feed)
    flow = commands.add_parser("flow", help="the bounded exit trace of an account with an incident label")
    flow.add_argument("subject")
    flow.add_argument("--depth", type=int, default=trace.DEFAULT_DEPTH)
    flow.add_argument("--max", type=int, default=trace.DEFAULT_MAX, help="the transfer cap")
    flow.add_argument("--name", help="the trace file name under intel/flows/ (default: the existing one, else the subject)")
    flow.add_argument("--record", action="store_true", help="fetch the transfer pages not recorded yet")
    flow.set_defaults(handler=_flow)
    # Every argument after the command goes to the module's own parser, options included: this parser
    # reads no option of its own (its prefix is "+", which no argument of that module starts with).
    clustering = commands.add_parser("cluster", help="account relationship edges over incident labelled seeds, "
                                     "as python -m intel.cluster", add_help=False, prefix_chars="+")
    clustering.add_argument("args", nargs=argparse.REMAINDER)
    clustering.set_defaults(handler=lambda args: cluster.main(args.args))
    rendering = commands.add_parser("render", help="write the static hosted pages from the committed files")
    rendering.add_argument("--out", required=True, help="the directory to write, such as demo/site")
    rendering.add_argument("--indexable", action="store_true", help="leave the robots noindex meta out")
    rendering.set_defaults(handler=_render)
    commands.add_parser("feed-stats", help="print the feed state").set_defaults(handler=_feed_stats)
    args = parser.parse_args(argv)
    return args.handler(args)


if __name__ == "__main__":
    sys.exit(main())
