"""Replay signer observations, or record the same read-only views with --live."""

import argparse
import json
from pathlib import Path

from gate import rpc

REFERENCE = Path(__file__).with_name("chainsig_reference.json")
RECORDINGS = Path(__file__).with_name("chainsig_rpc")


def check_views(directory, *, live=False):
    data = json.loads(REFERENCE.read_text(encoding="utf-8"))
    counts = {"state": 0, "public_key": 0, "derived_public_key": 0, "version": 0, "config": 0}
    for root in data["root_observations"]:
        client = rpc.Client(root["rpc"], mode="record" if live else "replay", fixtures_dir=directory)
        height = root["block_height"]
        with rpc.request_deadline(30):
            header = client.block_at(height)["header"]
        if header["hash"] != root["block_hash"]:
            raise ValueError("recorded block does not match the reference root")
        views = [("state", {}, root["domains"]), ("version", {}, "3.19.0"), ("config", {}, 15)]
        views.extend(("public_key", {"domain_id": int(domain)}, key)
                     for domain, key in root["public_keys"].items())
        for child in data["contract_derived_observations"]:
            if child["root_id"] == root["id"]:
                views.extend(("derived_public_key", {**child["args"], "domain_id": int(domain)}, key)
                             for domain, key in child["public_keys"].items())
        for method, args, expected in views:
            with rpc.request_deadline(30):
                value = client.call_function(root["contract"], method, args, block_id=height)
            if method == "state":
                value = value["Running"]["domains"]
            elif method == "config":
                value = value["sign_call_gas_attachment_requirement_tera_gas"]
            if value != expected:
                raise ValueError(f"{root['id']} {method} does not match the reference observation")
            counts[method] += 1
    return counts


def main(argv=None):
    parser = argparse.ArgumentParser(description="Replay or record public signer views without signing")
    parser.add_argument("--live", action="store_true", help="record read-only RPC views at the reference blocks")
    parser.add_argument("--out", type=Path, help="recording directory; --live requires a new directory")
    args = parser.parse_args(argv)
    if args.live and args.out is None:
        parser.error("--live requires --out naming a new directory")
    directory = args.out if args.out is not None else RECORDINGS
    if args.live:
        if directory.exists():
            parser.error("--live requires a new recording directory")
        directory.mkdir(parents=True)
    counts = check_views(directory, live=args.live)
    print(json.dumps(counts, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
