"""Public address derivation, offline by default and read-only with --live."""

import argparse
import json

from gate import rpc
from gate.chainsig import derive_public_key
from gate.chainsig.addresses import bitcoin_address, ethereum_address, solana_address
from gate.chainsig.views import read_derivation

_NETWORKS = {"mainnet": (rpc.MAINNET_RPC_URL, "v1.signer"),
             "testnet": (rpc.TESTNET_RPC_URL, "v1.signer-prod.testnet")}


def main(argv=None):
    parser = argparse.ArgumentParser(description="Derive public keys and addresses without signing")
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--root", help="supplied NEAR public root key; no network access")
    source.add_argument("--live", action="store_true", help="read the signer root and cross-check its derived key")
    parser.add_argument("--network", choices=tuple(_NETWORKS), help="NEAR signer network (default: testnet)")
    parser.add_argument("--domain-id", type=int, help="explicit Sign domain id for --live")
    parser.add_argument("--predecessor", required=True, help="account that would call the signer")
    parser.add_argument("--path", required=True, help="exact UTF-8 derivation path, including an empty string")
    args = parser.parse_args(argv)
    if args.domain_id is not None and not 0 <= args.domain_id < 2**64:
        parser.error("--domain-id must be a u64 integer")
    if args.live and args.domain_id is None:
        parser.error("--live requires --domain-id")
    if args.root is not None and args.domain_id is not None:
        parser.error("--domain-id requires --live so the domain can be checked")
    if args.root is not None and args.network is not None:
        parser.error("--network requires --live")
    if args.live:
        if args.network is None:
            args.network = "testnet"
        url, contract = _NETWORKS[args.network]
        client = rpc.Client(url, mode="off")
        with rpc.request_deadline(30):
            result = read_derivation(client, contract, args.domain_id, args.predecessor, args.path)
        result["network"] = args.network
    else:
        child = derive_public_key(args.root, args.predecessor, args.path)
        result = {"root": {"public_key": args.root, "source": "supplied"}, "predecessor": args.predecessor,
                  "path": args.path, "public_key": child, "contract_cross_check": False}
    key = result["public_key"]
    if key.startswith("secp256k1:"):
        result["addresses"] = {"ethereum": ethereum_address(key), "bitcoin": {
            network: {kind: bitcoin_address(key, network=network, kind=kind)
                      for kind in ("p2wpkh", "p2pkh", "p2sh_p2wpkh")}
            for network in ("mainnet", "testnet", "regtest")}}
    else:
        result["addresses"] = {"solana": solana_address(key)}
    print(json.dumps(result, indent=2, ensure_ascii=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
