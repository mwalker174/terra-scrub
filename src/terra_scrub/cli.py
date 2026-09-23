"""terra-scrub command-line entry point.

Every command lives in its own module and exposes exactly two functions:

    add_arguments(parser: argparse.ArgumentParser) -> None
    run(args: argparse.Namespace) -> int

``cli.main`` wires them into one root parser. Each command module also exposes
``main(argv=None) -> int`` so it can be run and tested standalone.
"""
from __future__ import annotations

import argparse
import sys

from terra_scrub import __version__

# (command name, module path, help). Order = order shown in --help.
COMMANDS = [
    ("snapshot",   "terra_scrub.snapshot",   "enumerate a bucket -> JSONL snapshot (GET only)"),
    ("context",    "terra_scrub.terra",      "capture Terra workspace attrs, referenced URIs, submissions"),
    ("workspaces", "terra_scrub.terra",      "list workspaces visible to you (optionally by namespace)"),
    ("lookup",     "terra_scrub.terra",      "which workspace owns a bucket"),
    ("report",     "terra_scrub.analyze",    "offline space report from a snapshot"),
    ("stale",      "terra_scrub.analyze",    "staleness report from snapshot + context"),
    ("dupes",      "terra_scrub.analyze",    "duplicate analysis from a snapshot"),
    ("candidates", "terra_scrub.candidates", "build reviewable delete + protected lists (offline)"),
    ("plan",       "terra_scrub.plan",       "re-validate a candidate list against live GCS -> PLAN (no delete verb)"),
    ("approve",    "terra_scrub.approve",    "arm a plan wrapper by writing its CONFIRM token (human step)"),
    ("verify",     "terra_scrub.verify",     "prove what a delete wrapper did (before/after re-listing)"),
    ("estate",     "terra_scrub.estate",     "drive scan / plan across every bucket in a set of namespaces"),
]


def build_parser() -> argparse.ArgumentParser:
    import importlib

    ap = argparse.ArgumentParser(
        prog="terra-scrub",
        description=__doc__.split("\n\n")[0],
    )
    ap.add_argument("--version", action="version", version=f"terra-scrub {__version__}")
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name, modpath, help_ in COMMANDS:
        mod = importlib.import_module(modpath)
        p = sub.add_parser(name, help=help_)
        # modules that own several commands dispatch on the command name
        adder = getattr(mod, f"add_arguments_{name}", None) or mod.add_arguments
        runner = getattr(mod, f"run_{name}", None) or mod.run
        adder(p)
        p.set_defaults(_run=runner)
    return ap


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return int(args._run(args) or 0)


if __name__ == "__main__":
    sys.exit(main())
