"""Command line: python -m intel <command>."""

import argparse
import sys

from intel import labels, seed


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
    args = parser.parse_args(argv)
    return args.handler(args)


if __name__ == "__main__":
    sys.exit(main())
