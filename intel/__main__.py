"""Command line: python -m intel <command>."""

import argparse
import sys

from intel import api, labels, seed


def _counts(args):
    print(labels.counts_line(labels.load().counts()))
    return 0


def _seed(args):
    added = seed.run(args.source)
    print(f"{added} new assertions from {args.source}")
    print(labels.counts_line(labels.load().counts()))
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(prog="python -m intel")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("counts", help="print the label store counts").set_defaults(handler=_counts)
    seeding = commands.add_parser("seed", help="seed the label store from one source")
    seeding.add_argument("source", choices=seed.SOURCES)
    seeding.set_defaults(handler=_seed)
    server = commands.add_parser("serve", help="serve the read only labels, incidents, evidence and flow API")
    server.add_argument("--host", default="127.0.0.1")
    server.add_argument("--port", type=int, default=8787)
    server.add_argument("--site", help="a directory of static files to serve too, such as demo/site")
    server.set_defaults(handler=lambda args: api.serve(args.host, args.port, args.site))
    args = parser.parse_args(argv)
    return args.handler(args)


if __name__ == "__main__":
    sys.exit(main())
