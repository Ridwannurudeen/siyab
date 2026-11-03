"""Issuer blacklist status rows built from recorded NEAR view calls."""

import argparse
import base64
import json
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

from gate import rpc
from intel import labels, seed

ROOT = Path(__file__).resolve().parent.parent
ISSUERS_PATH = ROOT / "intel" / "issuers.json"
FIXTURE_DIR = ROOT / "fixtures" / "issuers"
DATA_DIR = ROOT / "intel" / "issuers"
STATUS_PATH = DATA_DIR / "status.jsonl"
READS_PATH = DATA_DIR / "reads.json"
RECEIVERS_PATH = ROOT / "fixtures" / "receivers" / "set.json"
RPC_URL = rpc.MAINNET_RPC_URL
STATUS_CATEGORIES = ("incident", "denylist", "watch", "treasury", "infrastructure", "bridge")
RPC_INTERVAL_S = 1
BACKOFF_S = 10
LONG_BACKOFF_S = 120
MAX_CONSECUTIVE_429 = 5


def issuer_entries():
    return json.loads(ISSUERS_PATH.read_text(encoding="utf-8"))["mainnet"]


def subjects(store=None):
    store = store or labels.load()
    result = []
    seen = set()
    for assertion in store.assertions:
        if (assertion.chain == "near" and assertion.superseded_by is None
                and assertion.category in STATUS_CATEGORIES and assertion.subject not in seen):
            seen.add(assertion.subject)
            result.append(assertion.subject)
    for item in json.loads(RECEIVERS_PATH.read_text(encoding="utf-8"))["subjects"]:
        subject = item["subject"]
        if subject not in seen:
            seen.add(subject)
            result.append(subject)
    return result


def _query_params(token, method, args):
    encoded = json.dumps(args, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return {"request_type": "call_function", "finality": "final", "account_id": token,
            "method_name": method, "args_base64": base64.b64encode(encoded).decode("ascii")}


def _fixture_is_fresh(token, method, args, fixture_dir):
    params = _query_params(token, method, args)
    key = rpc.fixture_key(RPC_URL, "query", params)
    path = Path(fixture_dir) / f"{key}.json"
    if not path.exists():
        return False
    fetched = json.loads(path.read_text(encoding="utf-8"))["fetched_at"]
    fetched_at = datetime.strptime(fetched, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
    return (datetime.now(UTC) - fetched_at).total_seconds() < rpc.MAX_AGE_S


def read_status(mode="replay", store=None, fixture_dir=FIXTURE_DIR, sleep=time.sleep, clock=time.monotonic,
                progress=print):
    client = rpc.Client(RPC_URL, mode, fixtures_dir=fixture_dir)
    observed = []
    total_requests = 0
    total_429 = 0
    consecutive_429 = 0
    long_backoff_used = False
    last_request_started = None
    started = clock()
    pairs = [(subject, issuer) for subject in subjects(store) for issuer in issuer_entries()]
    try:
        for index, (subject, issuer) in enumerate(pairs, start=1):
            args = {issuer["args_key"]: subject}
            while True:
                network_request = mode == "off" or (mode == "record" and not _fixture_is_fresh(
                    issuer["token"], issuer["method"], args, fixture_dir))
                if network_request:
                    if last_request_started is not None:
                        delay = RPC_INTERVAL_S - (clock() - last_request_started)
                        if delay > 0:
                            sleep(delay)
                    last_request_started = clock()
                    total_requests += 1
                try:
                    answer = client.call_function(issuer["token"], issuer["method"], args)
                except rpc.HttpError as error:
                    if error.status != 429:
                        raise
                    total_429 += 1
                    consecutive_429 += 1
                    progress(f"429 #{total_429}, consecutive {consecutive_429}, status read {index}, "
                             f"method {issuer['method']}")
                    if consecutive_429 >= MAX_CONSECUTIVE_429:
                        if long_backoff_used:
                            raise
                        sleep(LONG_BACKOFF_S)
                        long_backoff_used = True
                        consecutive_429 = 0
                    else:
                        sleep(BACKOFF_S)
                    continue
                consecutive_429 = 0
                read = {"issuer": issuer["token"], "subject": subject, "method": issuer["method"],
                        "args": args, "answer": answer, "block": client.last_block_height,
                        "observed_at": client.last_source["fetched_at"]}
                observed.append(read)
                if mode == "record":
                    write_reads(observed, READS_PATH)
                break
    finally:
        elapsed = clock() - started
        if mode == "record":
            progress(f"recording summary: requests={total_requests}, 429s={total_429}, "
                     f"wall_seconds={elapsed:.1f}, status_reads={len(observed)}")
    return observed


def build_rows(reads):
    entries = {entry["token"]: entry for entry in issuer_entries()}
    rows = []
    for read in reads:
        issuer = entries[read["issuer"]]
        if read["answer"] != issuer["banned"]:
            continue
        answer = json.dumps(read["answer"], ensure_ascii=False)
        token = read["issuer"]
        rows.append(seed.make_row(
            entity=f"{token} issuer action",
            subject=read["subject"],
            chain="near",
            label=(f'banned by the issuer of {token}: {read["method"]} answers {answer} '
                   f'at block {read["block"]}'),
            category="watch",
            provenance="onchain",
            source={"rpc": RPC_URL, "method": read["method"], "args": read["args"],
                    "block": read["block"]},
            observed_at=read["observed_at"],
            note=f'{token} answered {answer} for this account id at block {read["block"]}',
        ))
    return rows


def reads():
    return json.loads(READS_PATH.read_text(encoding="utf-8"))


def rows():
    return build_rows(read_status())


def rows_jsonl():
    return "".join(labels.row_line(row) + "\n" for row in rows())


def write_rows(rows_to_write=None, path=STATUS_PATH):
    rows_to_write = rows() if rows_to_write is None else rows_to_write
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(labels.row_line(row) + "\n" for row in rows_to_write),
                    encoding="utf-8", newline="\n")
    return len(rows_to_write)


def write_reads(reads_to_write, path=READS_PATH):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(reads_to_write, indent=2, ensure_ascii=False) + "\n",
                    encoding="utf-8", newline="\n")


def record(sleep=time.sleep, clock=time.monotonic, progress=print):
    observed = read_status("record", sleep=sleep, clock=clock, progress=progress)
    write_reads(observed)
    count = write_rows(build_rows(observed))
    return len(observed), count


def append_rows():
    return seed.append(rows())


def main(argv=None):
    parser = argparse.ArgumentParser(prog="python -m intel.issuers")
    parser.add_argument("command", choices=("record", "rows", "append"))
    args = parser.parse_args(argv)
    try:
        if args.command == "record":
            reads_count, row_count = record()
            print(f"{reads_count} issuer status reads, {row_count} watch rows")
        elif args.command == "rows":
            print(f"{write_rows()} issuer status rows")
        else:
            print(f"{append_rows()} issuer status rows appended")
    except (rpc.FixtureMiss, rpc.HttpError, rpc.TransportError, rpc.RpcError, rpc.MalformedResponse) as error:
        print(f"stopped: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
