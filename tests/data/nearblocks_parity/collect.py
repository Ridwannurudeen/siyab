"""Read public NearBlocks responses for parity without touching replay recordings.

Run from the repository root with python tests/data/nearblocks_parity/collect.py.
Each request uses gate.rpc.Client in off mode, with an additional 11 second interval
across both versions. Completed captures are kept when a run is resumed.
"""

import hashlib
import json
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import UTC, datetime
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))

from gate import rpc

DATA = Path(__file__).resolve().parent
MANIFEST = DATA / "manifest.json"
KEY_SAMPLE = (
    "connectors.outlayer.near",
    "31ac7a2705a0686ff427b1a52d3ffd1fcfaa4b1f3cb3e83a0f767494e724a540",
    "rhea000453.multica.near",
    "intents.near",
    "grid.deltatrade.near",
    "user.intear.near",
    "mmt-arb.near",
    "aggregatedex.near",
    "via-trading.near",
    "codame.sputnik-dao.near",
)


def inventory():
    recordings = []
    paths = subprocess.check_output(["git", "ls-files", "fixtures"], cwd=ROOT, text=True).splitlines()
    for name in paths:
        path = ROOT / name
        if path.suffix != ".json":
            continue
        data = path.read_bytes()
        try:
            value = json.loads(data)
        except (ValueError, UnicodeError):
            continue
        if not isinstance(value, dict):
            continue
        url = value.get("url", "")
        if "/v1/txns/" in url:
            kind = "txn"
        elif "/v1/account/" in url and url.endswith("/keys"):
            kind = "keys"
        elif "nearblocks.io/v3/accounts/" in url and url.endswith("/txns"):
            kind = "account_txns_v3"
        else:
            continue
        recordings.append({"fixture": name, "fixture_sha256": hashlib.sha256(data).hexdigest(),
                           "kind": kind, "url": url})
    return recordings


class CapturedReply:
    def __init__(self, reply, capture):
        self.reply = reply
        self.capture = capture
        capture["status"] = reply.status
        capture["headers"] = {name: reply.headers[name] for name in ("Content-Type", "Retry-After")
                              if name in reply.headers}

    def __enter__(self):
        self.reply.__enter__()
        return self

    def __exit__(self, *args):
        return self.reply.__exit__(*args)

    def read(self):
        data = self.reply.read()
        self.capture["raw"] = data
        return data


def main():
    recordings = inventory()
    transactions = [row for row in recordings if row["kind"] == "txn"]
    sampled = []
    for account in KEY_SAMPLE:
        url = f"{rpc.NEARBLOCKS_URL}/v1/account/{account}/keys"
        sampled.append(next(row for row in recordings if row["url"] == url))
    requests = []
    for index, row in enumerate(sampled):
        for version in ("v3", "v1"):
            path = urllib.parse.urlsplit(row["url"]).path
            if version == "v3":
                path = path.replace("/v1/account/", "/v3/accounts/", 1)
            requests.append({**row, "id": f"keys-{index:02d}-{version}", "version": version,
                             "request_url": rpc.NEARBLOCKS_URL + path})
    for index, row in enumerate(transactions):
        requests.append({**row, "id": f"txn-{index:02d}-v3", "version": "v3",
                         "request_url": row["url"].replace("/v1/txns/", "/v3/txns/", 1)})
    manifest = (json.loads(MANIFEST.read_bytes()) if MANIFEST.exists() else
                {"revision": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT,
                                                       text=True).strip(),
                 "interval_seconds": 11, "recordings": recordings, "captures": []})
    if manifest["recordings"] != recordings:
        raise SystemExit("Committed NearBlocks inventory changed; refusing to replace the recorded baseline.")
    completed = {row["id"] for row in manifest["captures"]}
    print(f"{len(transactions)} transaction recordings; {len(sampled)} keys samples; "
          f"{len(requests)} requests; {len(completed)} already captured", flush=True)
    real_open = urllib.request.urlopen
    previous_start = None
    run = time.monotonic_ns()
    if manifest["captures"]:
        last = datetime.fromisoformat(manifest["captures"][-1]["requested_at"])
        remaining = 11.05 - (datetime.now(UTC) - last).total_seconds()
        if remaining > 0:
            time.sleep(remaining)
    for index, row in enumerate(requests):
        if row["id"] in completed:
            continue
        capture = dict(row)

        def capture_open(request, **kwargs):
            nonlocal previous_start
            if previous_start is not None:
                delay = previous_start + 11.05 - time.monotonic()
                if delay > 0:
                    time.sleep(delay)
            previous_start = time.monotonic()
            capture["requested_at"] = datetime.now(UTC).isoformat()
            capture["requested_monotonic"] = previous_start
            capture["capture_run"] = run
            try:
                return CapturedReply(real_open(request, **kwargs), capture)
            except urllib.error.HTTPError as error:
                capture["raw"] = error.read()
                capture["status"] = error.code
                capture["headers"] = {name: error.headers[name] for name in ("Content-Type", "Retry-After")
                                      if name in error.headers}
                raise

        client = rpc.Client(rpc.NEARBLOCKS_URL, "off", kind="http")
        failure = None
        try:
            with mock.patch.object(rpc.urllib.request, "urlopen", side_effect=capture_open):
                client.get(urllib.parse.urlsplit(row["request_url"]).path)
        except (rpc.HttpError, rpc.TransportError, rpc.RateLimited, OSError, ValueError) as error:
            failure = error
            capture["error"] = str(error)
        capture["completed_at"] = datetime.now(UTC).isoformat()
        capture["source"] = client.last_source
        raw = capture.pop("raw", None)
        if raw is not None:
            name = f"{row['id']}.json"
            (DATA / name).write_bytes(raw)
            capture["response_file"] = name
            capture["response_sha256"] = hashlib.sha256(raw).hexdigest()
        manifest["captures"].append(capture)
        MANIFEST.write_text(json.dumps(manifest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        print(f"{index + 1}/{len(requests)} {row['id']} HTTP {capture.get('status')} "
              f"{row['request_url']}" + (f" {failure}" if failure is not None else ""), flush=True)
        if capture.get("status") in (401, 403, 429) or raw is None:
            raise SystemExit("Collection stopped after provider refusal or missing response bytes.")


if __name__ == "__main__":
    main()
