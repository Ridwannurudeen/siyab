"""Command line: python -m gate <command>."""

import argparse
import sys

from gate import canon


def _canon_vectors(args):
    for path in canon.write_vectors(args.write, args.gate_root):
        print(f"wrote {path.as_posix()}")
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(prog="python -m gate")
    commands = parser.add_subparsers(dest="command", required=True)

    vectors = commands.add_parser("canon-vectors", help="write the signed canonical message vectors")
    vectors.add_argument("--write", required=True, metavar="DIR",
                         help="vector directory; evidence and the TEST key go to its siblings")
    vectors.add_argument("--gate-root", default=canon.DEFAULT_GATE_ROOT, metavar="ACCOUNT",
                         help=f"sandbox root account id (default {canon.DEFAULT_GATE_ROOT})")
    vectors.set_defaults(handler=_canon_vectors)

    args = parser.parse_args(argv)
    return args.handler(args)


if __name__ == "__main__":
    sys.exit(main())
