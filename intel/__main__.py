"""Command line: python -m intel <command>.

receivers [--record] judges the recorded mainnet receiver set with gate.rules.judge in replay (an
empty allowlist, the committed label store and the archival RPC) and writes one
fixtures/receivers/judgements/<subject>.json per subject of fixtures/receivers/set.json. Each subject
has a cohort. The set is the last COUNTERPARTIES distinct counterparties of each account in
RECEIVER_SOURCES (the FastNEAR Transfers API, newest first; implicit accounts are left out as the
likeliest personal wallets, unless the label store holds them as a catalog token), less the ids the
exclusion file names (cohort counterparty); then the first ORGANISATION_SUBJECTS Sputnik DAO accounts
and the first ORGANISATION_SUBJECTS validator pool accounts among the organisation rows of the label
store, in store order (cohort organisation); then the NEAR accounts of intel/incidents/rhea.json
(cohort incident); then the first CATALOG_SUBJECTS catalog_token NEAR accounts of the label store
that are not in the set yet (cohort catalog). A token subject is judged as the token of a transfer to
TOKEN_RECEIVER, as intel/incidents/rhea.json judges its token contracts, and every other subject as
the receiver of a NEAR transfer.

--record chooses the set and fetches what is missing for the subjects new to it. The counterparties
come from the transfers pages recorded under fixtures/fastnear/, read in replay, and the ids of
--exclude-file (a file kept outside the repository, one account id per line) are left out after
they are chosen, so no other account takes their place. A subject of the committed set is judged
on its earlier recording; a new subject gets the view calls and archival reads the rules make and,
while fewer than AGE_SUBJECTS receivers have their age recorded, the NearBlocks keys and creation
transaction (at most two NearBlocks calls each), so every later receiver without an earlier
recording is dated by its oldest access key nonce or an archival view_account alone and reads
unknown on age where neither dates it. The view calls, archival reads and NearBlocks answers live
under fixtures/receivers/rpc/. Without --record nothing touches the network.

top-receivers --hours N [--to-height H] [--record] [--data-dir DIR] scans the pinned window ending
at neardata's final block, or at H, and bisects for the first height at or after the start time.
It stores only reduced heights in resumable batches outside the repository; a null height is marked
skipped. Successful native Transfer actions with a positive deposit count unless their predecessor
is system, and successful EVENT_JSON nep141 ft_transfer events count from old_owner_id to
new_owner_id with the token contract as the asset; failed receipts and self transfers are dropped.
nep245 mt_transfer events from intents.near stay in a separate cohort. Receivers are ordered by
distinct payer count descending, transfer count descending, then account id ascending. Implicit
account ids are left out unless the label store has a current catalog_token row. The named-account
exclusions stated by set.json are applied after selecting the first 1,000, without replacement.
fixtures/receivers/top.json records the pinned heights and timestamps, fetched and skipped heights,
the sha256 of the concatenated reductions, and the exclusions. The data directory defaults to
~/.cache/shieldbot-near/top-receivers and never contains raw blocks.
"""

import argparse
import hashlib
import json
import os
import re
import sys
import time
from pathlib import Path

from gate import rpc, rules
from intel import alerts, api, cluster, feed, issuers, labels, records, render, seed, top_receivers, trace

ROOT = Path(__file__).resolve().parent.parent
RECEIVERS_DIR = ROOT / "fixtures" / "receivers"
FASTNEAR_DIR = ROOT / "fixtures" / "fastnear"
# The view calls and NearBlocks answers the receiver judgements read, kept apart from fixtures/rpc/.
RECEIVER_RPC_DIR = RECEIVERS_DIR / "rpc"
TOP_RECEIVERS_PATH = top_receivers.RANKING_PATH
TOP_RECEIVERS_DIR = RECEIVERS_DIR / "top"
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
AGE_SUBJECTS = 80
ORGANISATION_SUBJECTS = 25
# The registry reads whose organisation rows the organisation cohort is drawn from, each with its why.
ORGANISATION_SOURCES = (
    ("get_dao_list", (f"one of the first {ORGANISATION_SUBJECTS} Sputnik DAO accounts among the organisation rows of "
                      "the label store (sputnik-dao.near get_dao_list), in store order; most DAO accounts have a "
                      "NearBlocks key creation time and read their age like any account; "
                      "ref-finance.sputnik-dao.near reads as the pools do: NearBlocks lists no access key for the "
                      "account and view_account shows contract code, so its age comes from an archival "
                      "view_account at a block at least 7 days back")),
    ("validators", (f"one of the first {ORGANISATION_SUBJECTS} validator pool accounts among the organisation rows of "
                    "the label store (the validators RPC), in store order; NearBlocks lists no access key for the "
                    "account and view_account shows contract code, so its age comes from an archival "
                    "view_account at a block at least 7 days back")),
)
# Each cohort with the words the cohort summary uses for it, in its order.
COHORTS = (("counterparty", "ordinary counterparties"), ("organisation", "organisations from registries"),
           ("incident", "incident accounts"), ("catalog", "catalog tokens"))
EXCLUSION = ("Named accounts whose id reads as a person's name or handle were left out by hand before publication "
             "({} accounts); the set lists no account id for them.")
TOKEN_RECEIVER = "intents.near"
IMPLICIT = re.compile(r"[0-9a-f]{64}|0x[0-9a-f]{40}")
_TIME = "%Y-%m-%dT%H:%M:%SZ"


def _counts(args):
    print(labels.counts_line(labels.load().counts()))
    return 0


def _record_check(args):
    results = []
    superseders = {}
    for path in args.paths:
        try:
            record = records.read(path)
        except records.RecordError as error:
            results.append((path, None, error))
        else:
            results.append((path, record, None))
            if record["supersedes"] is not None:
                superseders.setdefault(record["supersedes"], set()).add(record["record_id"])

    conflicts = {record_id for record_id, ids in superseders.items() if len(ids) > 1}
    failed = False
    for path, record, error in results:
        if error is not None:
            print(f"refused {path}: {error}")
            failed = True
        elif record["supersedes"] in conflicts:
            print(f"refused {path}: multiple records supersede {record['supersedes']}")
            failed = True
        else:
            print(f"valid {path}")
    return 1 if failed else 0


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


def organisation_accounts(store):
    """[(subject, why)]: the first ORGANISATION_SUBJECTS current NEAR organisation subjects of each read of
    ORGANISATION_SOURCES, in store order."""
    picked = {method: [] for method, _ in ORGANISATION_SOURCES}
    for assertion in store.assertions:
        if assertion.category != "organisation" or assertion.chain != "near" or assertion.superseded_by is not None:
            continue
        found = picked.get(assertion.source.get("method"))
        if found is not None and len(found) < ORGANISATION_SUBJECTS and assertion.subject not in found:
            found.append(assertion.subject)
    return [(subject, why) for method, why in ORGANISATION_SOURCES for subject in picked[method]]


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


def choose(store, transfers_client, exclude=frozenset()):
    """(subjects [{subject, why, as, cohort, request}], sources [{account, role, requests}], how many chosen
    counterparties exclude left out)."""
    catalog = catalog_accounts(store)
    rhea = json.loads(RHEA_INCIDENT.read_text(encoding="utf-8"))
    rhea_tokens = {entry["subject"] for entry in rhea["rules_today"] if entry["as"] == "token"}
    subjects, sources, seen, excluded = [], [], set(), set()

    def add(subject, why, as_token, cohort):
        if subject not in seen:
            seen.add(subject)
            subjects.append({"subject": subject, "why": why, "as": "token" if as_token else "receiver",
                             "cohort": cohort, "request": _request(subject, as_token)})

    for account, role in RECEIVER_SOURCES:
        found, keys = counterparties(transfers_client, account, catalog)
        sources.append({"account": account, "role": role, "requests": keys})
        for subject in found:
            if subject in exclude:
                excluded.add(subject)
                continue
            add(subject, f"one of the last {COUNTERPARTIES} distinct counterparties of {account} in the FastNEAR "
                         "Transfers API", subject in catalog, "counterparty")
    for subject, why in organisation_accounts(store):
        add(subject, why, False, "organisation")
    for account in rhea["accounts"]:
        if account["chain"] == "near":
            add(account["subject"], f"named in the incident page rhea as {account['role_as_reported']}",
                account["subject"] in rhea_tokens, "incident")
    added = 0
    for account in catalog:
        if added < CATALOG_SUBJECTS and account not in seen:
            add(account, "a catalog_token NEAR account of the label store, in store order", True, "catalog")
            added += 1
    return subjects, sources, len(excluded)


def _judge(entry, store, client, nearblocks, now_ms, archival, reports_account=None):
    judgement = rules.judge(entry["request"], {"allowlist": [], "caps": []}, store, client, nearblocks,
                            now_ms=now_ms, injection_scanner=None, archival=archival,
                            issuers=rules.issuers_for("mainnet"), reports_account=reports_account)
    return {"subject": entry["subject"], "as": entry["as"], "request": entry["request"],
            "verdict": judgement.verdict, "checks": judgement.checks, "reasons": judgement.reasons,
            "labels": judgement.labels, "observed": judgement.observed}


def _write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes((json.dumps(value, indent=2, ensure_ascii=False) + "\n").encode("utf-8"))


def receivers_rule():
    """The selection and recording rule set.json states: the two receivers paragraphs of this docstring."""
    return " ".join(" ".join(__doc__.split("\n\n")[1:3]).split())


def top_receivers_rule():
    """The top receiver rule from the third receiver paragraph of this docstring."""
    return " ".join(__doc__.split("\n\n")[3].split())


def record_receivers(store, previous, exclude, clock=time.time, reports_account=None):
    """Choose the set, leaving the ids of exclude out of the counterparties, and fetch what the rules read for
    the subjects previous (the subjects of the committed set) does not hold; returns the set document."""
    transfers_client = rpc.Client(TRANSFERS_URL, "replay", kind="http", fixtures_dir=FASTNEAR_DIR)
    subjects, sources, excluded = choose(store, transfers_client, exclude)
    now_ms = int(clock() * 1000)
    client = rpc.Client(rpc.MAINNET_RPC_URL, "record", fixtures_dir=RECEIVER_RPC_DIR)
    archival = rpc.Client(rpc.ARCHIVAL_RPC_URL, "record", fixtures_dir=RECEIVER_RPC_DIR)
    earlier = {entry["subject"]: entry["age_recorded"] for entry in previous}
    aged = []
    for entry in subjects:
        if earlier.get(entry["subject"]) and entry["request"]["receiver_id"] not in aged:
            aged.append(entry["request"]["receiver_id"])
    fresh = []
    for entry in subjects:
        if entry["subject"] in earlier:
            continue
        receiver = entry["request"]["receiver_id"]
        if receiver not in aged and len(aged) < AGE_SUBJECTS:
            aged.append(receiver)
            fresh.append(receiver)
        nearblocks = rpc.Client(rpc.NEARBLOCKS_URL, "record" if receiver in fresh else "replay", kind="http",
                                fixtures_dir=RECEIVER_RPC_DIR)
        _judge(entry, store, client, nearblocks, now_ms, archival, reports_account)
    for entry in subjects:
        entry["age_recorded"] = earlier.get(entry["subject"], entry["request"]["receiver_id"] in aged)
    return {"chosen_at": time.strftime(_TIME, time.gmtime(now_ms // 1000)), "judged_at_ms": now_ms,
            "rule": receivers_rule(), "exclusion": EXCLUSION.format(excluded), "sources": sources,
            "subjects": subjects}


def judge_receivers(document, store, directory=RECEIVERS_DIR, reports_account=None):
    """Judge every subject of the set in replay and write its judgement file; returns the judgements."""
    client = rpc.Client(rpc.MAINNET_RPC_URL, "replay", fixtures_dir=RECEIVER_RPC_DIR)
    nearblocks = rpc.Client(rpc.NEARBLOCKS_URL, "replay", kind="http", fixtures_dir=RECEIVER_RPC_DIR)
    archival = rpc.Client(rpc.ARCHIVAL_RPC_URL, "replay", fixtures_dir=RECEIVER_RPC_DIR)
    results = []
    for entry in document["subjects"]:
        result = _judge(entry, store, client, nearblocks, document["judged_at_ms"], archival, reports_account)
        _write_json(Path(directory) / "judgements" / f"{entry['subject']}.json", result)
        results.append(result)
    return results


def cohort_line(document, results):
    """The verdicts of the judgements results per cohort of the set document, as one sentence."""
    verdicts = {result["subject"]: result["verdict"] for result in results}
    parts = []
    for cohort, words in COHORTS:
        found = [verdicts[entry["subject"]] for entry in document["subjects"] if entry["cohort"] == cohort]
        parts.append(f"{words} {len(found)} ({found.count('ALLOW')} ALLOW, {found.count('DENY')} DENY, "
                     f"{found.count('UNKNOWN')} Unknown)")
    return "By cohort: " + "; ".join(parts) + "."


def _receivers(args):
    store = labels.load()
    reports_account = os.environ.get("REPORTS_ACCOUNT_ID")
    set_path = RECEIVERS_DIR / "set.json"
    if args.record:
        previous = json.loads(set_path.read_text(encoding="utf-8"))["subjects"] if set_path.is_file() else []
        exclude = set()
        if args.exclude_file:
            exclude = {line.strip() for line in Path(args.exclude_file).read_text(encoding="utf-8").splitlines()
                       if line.strip()}
        try:
            document = record_receivers(store, previous, exclude, reports_account=reports_account)
        except rpc.HttpError as error:
            print(f"stopped: {error}; no key is ever added", file=sys.stderr)
            return 1
        _write_json(set_path, document)
    document = json.loads(set_path.read_text(encoding="utf-8"))
    results = judge_receivers(document, store, reports_account=reports_account)
    verdicts = [result["verdict"] for result in results]
    print(f"On {len(verdicts)} recorded mainnet receivers the rules returned {verdicts.count('ALLOW')} ALLOW, "
          f"{verdicts.count('DENY')} DENY, {verdicts.count('UNKNOWN')} Unknown.")
    print(cohort_line(document, results))
    return 0


def top_receiver_exclusions(store):
    """The manually excluded counterparties recorded by the existing receiver set."""
    set_document = json.loads((RECEIVERS_DIR / "set.json").read_text(encoding="utf-8"))
    recorded = {entry["subject"] for entry in set_document["subjects"] if entry["cohort"] == "counterparty"}
    transfers_client = rpc.Client(TRANSFERS_URL, "replay", kind="http", fixtures_dir=FASTNEAR_DIR)
    chosen, _, _ = choose(store, transfers_client)
    candidates = {entry["subject"] for entry in chosen if entry["cohort"] == "counterparty"}
    excluded = candidates - recorded
    stated_count = re.search(r"\((\d+) accounts\)", set_document["exclusion"])
    if stated_count is None or len(excluded) != int(stated_count.group(1)):
        raise ValueError("the receiver-set exclusion count does not match its recorded candidates")
    return excluded


def record_top_receiver_judgements(document, store, reports_account=None):
    """Record missing RPC reads for the top set, using the existing age lookup limit."""
    client = rpc.Client(rpc.MAINNET_RPC_URL, "record", fixtures_dir=RECEIVER_RPC_DIR)
    archival = rpc.Client(rpc.ARCHIVAL_RPC_URL, "record", fixtures_dir=RECEIVER_RPC_DIR)
    for entry in document["subjects"]:
        nearblocks = rpc.Client(rpc.NEARBLOCKS_URL, "record" if entry["age_recorded"] else "replay",
                                kind="http", fixtures_dir=RECEIVER_RPC_DIR)
        _judge(entry, store, client, nearblocks, document["judged_at_ms"], archival, reports_account)


def top_receivers_coverage_line(document, results):
    """Count outcomes and each unknown check across the separate top receiver judgement set."""
    expected = len(document["subjects"])
    verdicts = [result["verdict"] for result in results]
    causes = {}
    for result in results:
        if result["verdict"] != "UNKNOWN":
            continue
        found = [check["id"] for check in result["checks"] if check["status"] == "unknown"]
        for cause in found or ["unspecified"]:
            causes[cause] = causes.get(cause, 0) + 1
    cause_text = ", ".join(f"{cause} {count}" for cause, count in sorted(causes.items())) or "none"
    return (f"Top receiver coverage: judged {len(verdicts)}/{expected}; "
            f"ALLOW {verdicts.count('ALLOW')}/{expected}; DENY {verdicts.count('DENY')}/{expected}; "
            f"UNKNOWN {verdicts.count('UNKNOWN')}/{expected}; UNKNOWN causes: {cause_text}.")


def _top_receivers(args):
    store = labels.load()
    try:
        state = top_receivers.scan_window(args.hours, args.to_height, args.record, args.data_dir)
        exclusions = top_receiver_exclusions(store)
        document = top_receivers.build_ranking(
            args.data_dir, state, top_receivers_rule(), catalog_accounts(store), exclusions,
            EXCLUSION.format(len(exclusions)), AGE_SUBJECTS)
    except (feed.FeedError, rpc.FixtureMiss, rpc.HttpError, rpc.RateLimited, rpc.TransportError,
            top_receivers.TopReceiversError, OSError) as error:
        print(f"stopped: {error}", file=sys.stderr)
        return 1
    _write_json(TOP_RECEIVERS_PATH, document)
    print(f"Wrote {len(document['subjects'])} ranked receivers to fixtures/receivers/top.json.")
    return 0


def _judge_top_receivers(args):
    store = labels.load()
    try:
        document = json.loads(TOP_RECEIVERS_PATH.read_text(encoding="utf-8"))
        if args.record:
            record_top_receiver_judgements(document, store, os.environ.get("REPORTS_ACCOUNT_ID"))
        results = judge_receivers(document, store, TOP_RECEIVERS_DIR,
                                  reports_account=os.environ.get("REPORTS_ACCOUNT_ID"))
    except (feed.FeedError, rpc.FixtureMiss, rpc.HttpError, rpc.RateLimited, rpc.TransportError,
            OSError, ValueError) as error:
        print(f"stopped: {error}", file=sys.stderr)
        return 1
    print(top_receivers_coverage_line(document, results))
    return 0


def _feed(args):
    try:
        if args.follow:
            if args.blocks is not None or args.record:
                print("stopped: --follow takes neither --blocks nor --record", file=sys.stderr)
                return 2
            interrupted = False

            def stop(signum, frame):
                nonlocal interrupted
                interrupted = True

            import signal

            previous = signal.signal(signal.SIGINT, stop)
            try:
                state, walked = feed.follow(args.start, max_blocks=args.max_blocks,
                                            stopped=lambda: interrupted)
            finally:
                signal.signal(signal.SIGINT, previous)
            print(f"followed {walked} heights to height {state['last_height']}, lag {state['lag_blocks']} blocks")
            return 0
        if args.blocks is None:
            print("stopped: --blocks is required unless --follow is set", file=sys.stderr)
            return 2
        state, events = feed.run(args.start, args.blocks, args.record)
    except (feed.FeedError, rpc.HttpError, rpc.TransportError) as error:
        print(f"stopped: {error}", file=sys.stderr)
        return 1
    print(f"{state['blocks']} blocks to height {state['last_height']}, {len(events)} new events")
    return 0


def write_feed_snapshot(source, destination):
    source = Path(source)
    state = json.loads((source / "state.json").read_text(encoding="utf-8"))
    state.pop("fresh_accounts", None)
    events = [json.loads(path.read_text(encoding="utf-8")) for path in sorted((source / "events").glob("*.json"))]
    blocks = {path.name: json.loads(path.read_text(encoding="utf-8"))
              for path in sorted((source / "event-blocks").glob("*.json"))}
    state["event_documents"] = events
    state["event_blocks"] = blocks
    feed._write_json(Path(destination), state, indent=2)
    return len(events), len(blocks)


def _feed_snapshot(args):
    events, blocks = write_feed_snapshot(args.source, feed.SNAPSHOT_PATH)
    print(f"snapshot written with {events} events and {blocks} event blocks")
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


def _alert_test(args):
    secret_path = os.environ.get("SHIELDBOT_ALERT_SECRET_FILE")
    if not secret_path:
        print("stopped: SHIELDBOT_ALERT_SECRET_FILE is required", file=sys.stderr)
        return 2
    try:
        sink = alerts.WebhookSink(args.url, secret_path)
    except ValueError as error:
        print(f"stopped: {error}", file=sys.stderr)
        return 2
    created_ms = int(time.time() * 1000)
    detail = "A test alert was sent from the intel CLI."
    alert = alerts.Alert(
        source="shieldbot-cli",
        severity="info",
        subject="alert-test",
        ref=f"alert-test-{created_ms}",
        title="ShieldBot alert test",
        detail=detail,
        evidence_sha256=hashlib.sha256(detail.encode("utf-8")).hexdigest(),
        created_ms=created_ms,
    )
    result = alerts.send(alert, [sink])[0]
    if result["ok"]:
        print("test alert sent")
        return 0
    print(f"stopped: alert delivery failed ({result.get('error', 'delivery_failed')})", file=sys.stderr)
    return 1


def main(argv=None):
    parser = argparse.ArgumentParser(prog="python -m intel")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("counts", help="print the label store counts").set_defaults(handler=_counts)
    record_check = commands.add_parser("record-check", help="validate incident records")
    record_check.add_argument("paths", nargs="+")
    record_check.set_defaults(handler=_record_check)
    seeding = commands.add_parser("seed", help="seed the label store from one source")
    seeding.add_argument("source", choices=seed.SOURCES)
    seeding.set_defaults(handler=_seed)
    server = commands.add_parser("serve", help="serve the read only labels, incidents, evidence, flow, cluster and "
                                 "issuers API")
    server.add_argument("--host", default="127.0.0.1")
    server.add_argument("--port", type=int, default=8787)
    server.add_argument("--site", help="a directory of static files to serve too, such as demo/site")
    server.set_defaults(handler=lambda args: api.serve(args.host, args.port, args.site))
    receivers = commands.add_parser("receivers", help="judge the recorded mainnet receiver set in replay")
    receivers.add_argument("--record", action="store_true", help="choose the set and record what is missing")
    receivers.add_argument("--exclude-file", help="with --record: a file of account ids kept outside the repository, "
                           "one per line, left out of the counterparties")
    receivers.set_defaults(handler=_receivers)
    top = commands.add_parser("top-receivers", help="scan and rank mainnet payment receivers")
    top.add_argument("--hours", type=int, required=True, help="length of the pinned scan window in hours")
    top.add_argument("--to-height", type=int, help="pin the window end to this height instead of the final block")
    top.add_argument("--record", action="store_true", help="fetch blocks and resume the reduced scan")
    top.add_argument("--data-dir", type=Path, default=top_receivers.DEFAULT_DATA_DIR,
                     help=f"outside-repository reductions and checkpoint (default: "
                          f"{top_receivers.DEFAULT_DATA_DIR})")
    top.set_defaults(handler=_top_receivers)
    top_judge = commands.add_parser("judge-top-receivers", help="judge the separate top receiver set")
    top_judge.add_argument("--record", action="store_true", help="record missing RPC reads before replay")
    top_judge.set_defaults(handler=_judge_top_receivers)
    feeding = commands.add_parser("feed", help="five signals: code_hash_matches_incident, fresh_dust_funded_pays_labelled, watch_account_key_added, labelled_account_large_transfer, deploy_on_labelled_contract")
    feeding.add_argument("--from", dest="start", type=int, help="the first height (default: after the state)")
    feeding.add_argument("--blocks", type=int, help="how many recorded heights to walk")
    feeding.add_argument("--follow", action="store_true", help="follow finalized heights continuously")
    feeding.add_argument("--max-blocks", type=int, help="stop after this many heights, for bounded runs")
    feeding.add_argument("--record", action="store_true", help="fetch and reduce the blocks not recorded yet")
    feeding.set_defaults(handler=_feed)
    snapshot = commands.add_parser("feed-snapshot", help="copy a follower state into the committed snapshot")
    snapshot.add_argument("--from", dest="source", required=True, help="the follower state directory")
    snapshot.set_defaults(handler=_feed_snapshot)
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
    issuing = commands.add_parser("issuers", help="issuer status reads and the issuer action history, as python -m "
                                  "intel.issuers", add_help=False, prefix_chars="+")
    issuing.add_argument("args", nargs=argparse.REMAINDER)
    issuing.set_defaults(handler=lambda args: issuers.main(args.args))
    rendering = commands.add_parser("render", help="write the static hosted pages from the committed files")
    rendering.add_argument("--out", required=True, help="the directory to write, such as demo/site")
    rendering.add_argument("--indexable", action="store_true", help="leave the robots noindex meta out")
    rendering.set_defaults(handler=_render)
    commands.add_parser("feed-stats", help="print the feed state").set_defaults(handler=_feed_stats)
    alert_test = commands.add_parser("alert-test", help="send one test alert to an HTTPS webhook")
    alert_test.add_argument("--url", required=True, help="the HTTPS webhook URL")
    alert_test.set_defaults(handler=_alert_test)
    args = parser.parse_args(argv)
    return args.handler(args)


if __name__ == "__main__":
    sys.exit(main())
