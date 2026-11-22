"""Judge an unlabelled account file with the receiver benchmark's version 3 rules."""

import contextlib
import hashlib
import json
import os
import sys
import time

from gate import canon, rpc
from intel import __main__ as receivers
from intel import budget, labels, top_receivers


def _accounts(path):
    accounts, seen = [], {}
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        canon.account_id(line, f"{path}:{number}")
        if line == "testnet" or line.endswith(".testnet"):
            raise ValueError(f"{path}:{number}: a mainnet account is required")
        if line in seen:
            raise ValueError(f"{path}:{number}: duplicate account (first on line {seen[line]})")
        accounts.append(line)
        seen[line] = number
    if not accounts:
        raise ValueError(f"{path}: no accounts")
    if accounts != sorted(accounts, key=lambda account: hashlib.sha256(account.encode("utf-8")).digest()):
        raise ValueError(f"{path}: accounts must be sorted by sha256 of the account id")
    return accounts


class _FixtureError(Exception):
    """An invalid read fixture envelope, reported before the rules can consume it."""


class _ReadClient(rpc.Client):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.missing = {}

    def _fixture_bytes(self, path, data=None):
        content = super()._fixture_bytes(path, data)
        if data is None:
            try:
                fixture = json.loads(content)
            except (ValueError, UnicodeError, RecursionError) as error:
                raise _FixtureError(f"{path.name}: malformed read fixture JSON") from error
            if (not isinstance(fixture, dict) or not {"response", "fetched_at"} <= fixture.keys()
                    or not isinstance(fixture["fetched_at"], str)):
                raise _FixtureError(f"{path.name}: malformed read fixture envelope")
        return content

    def _exchange(self, url, method, params, body, *, request_url=None, record_http_errors=()):
        # The NearBlocks adapter can satisfy a v3 probe from its recorded v1 response.
        logical_url = (url.replace("/v1/account/", "/v3/accounts/").replace("/v1/txns/", "/v3/txns/")
                       if self.url == rpc.NEARBLOCKS_URL and method == "GET" else url)
        key = rpc.fixture_key(logical_url, method, params)
        try:
            response = super()._exchange(url, method, params, body, request_url=request_url,
                                         record_http_errors=record_http_errors)
        except rpc.FixtureMiss:
            if method == "GET":
                checks = ("account_age", "dust_funded")
            elif method == "block" or params["request_type"] == "view_access_key_list":
                checks = ("account_age",)
            elif params["request_type"] == "view_account":
                checks = ("receiver_exists", "account_age")
            else:
                checks = {
                    "contract_source_metadata": ("code_without_metadata",),
                    "subject_summary": ("community_report",),
                    "get_blacklist_status": ("issuer_blacklist",),
                    "is_blocklisted": ("issuer_blacklist",),
                }[params["method_name"]]
            self.missing[key] = checks
            raise
        except rpc.HttpError:
            self.missing.pop(key, None)
            raise
        self.missing.pop(key, None)
        return response


def run(args):
    rules_version = 3
    try:
        accounts = _accounts(args.accounts)
        reports_account = os.environ.get("REPORTS_ACCOUNT_ID")
        destination = args.out.expanduser().resolve()
        holdout = receivers.ROOT / "fixtures" / "holdout"
        if ((destination == receivers.ROOT or receivers.ROOT in destination.parents)
                and holdout not in destination.parents):
            raise ValueError("use an output directory outside the source repository or under fixtures/holdout/")
        for published in holdout.glob("*/receivers.jsonl"):
            published = published.parent.resolve()
            if destination == published or published in destination.parents:
                raise ValueError("use an output directory outside an existing published holdout")
        store = labels.load(args.labels)
        checkpoint = destination / "set.json"
        identity = {"accounts": accounts, "rules_version": rules_version, "label_store_sha256": store.sha256,
                    "reports_account": reports_account}
        if checkpoint.is_file():
            document = json.loads(checkpoint.read_text(encoding="utf-8"))
            if (not isinstance(document, dict) or document != {**identity,
                    "judged_at_ms": document.get("judged_at_ms")}
                    or type(document.get("judged_at_ms")) is not int or document["judged_at_ms"] < 0):
                raise ValueError("the saved run differs from these accounts, rules or label store; use another --out")
        else:
            if not args.record:
                raise ValueError("replay requires a recorded run with set.json; use --record first")
            if (destination / "judged-block.json").exists() or any((destination / "judgements").glob("*.json")):
                raise ValueError("judgements without a saved run; use another --out")
            document = {**identity, "judged_at_ms": int(time.time() * 1000)}
        database = receivers._record_database("judge-accounts") if args.record else None
        with contextlib.ExitStack() as stack:
            headers, refusals = {}, []
            if args.record:
                stack.enter_context(budget.use_database(database))
                refusals = stack.enter_context(rpc.caller_class("recording"))
                headers = top_receivers.fastnear_headers()
                receivers._write_json(checkpoint, document)
            fixtures = destination / "rpc"
            initial_mode = "record" if args.record else "replay"
            client = _ReadClient(rpc.MAINNET_RPC_URL, initial_mode, fixtures_dir=fixtures,
                                headers=headers, reuse_existing=True)
            archival = _ReadClient(rpc.ARCHIVAL_RPC_URL, initial_mode, fixtures_dir=fixtures,
                                  headers=headers, reuse_existing=True)
            block_path = destination / "judged-block.json"
            now_ms = document["judged_at_ms"]
            block = receivers.top_judged_block(archival, now_ms, path=block_path)
            verdicts = []
            for index, account in enumerate(accounts):
                entry = {"subject": account, "as": "receiver", "request": receivers._request(account, False)}
                for read_mode in (("record", "replay") if args.record else ("replay",)):
                    client.mode = archival.mode = read_mode
                    client.missing.clear()
                    archival.missing.clear()
                    nearblocks = _ReadClient(rpc.NEARBLOCKS_URL,
                        "record" if read_mode == "record" and index < receivers.AGE_SUBJECTS else "replay",
                        kind="http", fixtures_dir=fixtures, reuse_existing=True)
                    result = receivers._judge(entry, store, client, nearblocks, now_ms, archival, reports_account,
                                              rules_version=rules_version)
                    if refusals:
                        raise refusals[0]
                    missing = client.missing | archival.missing | nearblocks.missing
                    if receivers._age_unknown(result):
                        if block is None and read_mode == "record":
                            block = receivers.top_judged_block(archival, now_ms, discover=True, path=block_path)
                        if block is not None:
                            archival.missing.clear()
                            nearblocks.missing.clear()
                            pinned = receivers._pinned_judgement(entry, store, archival, nearblocks, block, now_ms,
                                reports_account, rules_version=rules_version)
                            if refusals:
                                raise refusals[0]
                            if pinned is not None:
                                result = pinned
                                missing = archival.missing | nearblocks.missing
                answer = {**result, "rules_version": rules_version}
                unknown = {check["id"] for check in result["checks"] if check["status"] == "unknown"}
                if result["verdict"] == "UNKNOWN" and any(unknown.intersection(checks) for checks in missing.values()):
                    answer["scoreable"] = False
                receivers._write_json(destination / "judgements" / f"{account}.json", answer)
                verdicts.append(result["verdict"])
    except (_FixtureError, rpc.FixtureMiss, rpc.HttpError, rpc.RecordingKeyRejected, rpc.RateLimited, rpc.RpcError,
            rpc.TransportError, OSError, ValueError) as error:
        print(f"stopped: {error}", file=sys.stderr)
        return 1
    print(json.dumps({"rules_version": rules_version, "judged": len(verdicts),
                      "verdicts": {verdict: verdicts.count(verdict) for verdict in ("ALLOW", "DENY", "UNKNOWN")}},
                     sort_keys=True))
    return 0
