from __future__ import annotations

import argparse
from typing import List, Optional

from lucen import __version__

from . import explain


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="lucen",
        description="Lucen diagnostics CLI (spec §5.15).",
    )
    parser.add_argument("--version", action="version", version=f"lucen {__version__}")
    sub = parser.add_subparsers(dest="command")

    p_explain = sub.add_parser(
        "explain", help="static per-block parallelization report (spec §5.15.1)"
    )
    p_explain.add_argument("file")
    p_explain.add_argument("--block", type=int, help="report a single block by index")
    p_explain.add_argument(
        "--format",
        choices=("text", "json"),
        default="text",
        dest="fmt",
        help="output format (json is the baseline format)",
    )
    mode_group = p_explain.add_mutually_exclusive_group()
    mode_group.add_argument(
        "--assume-free-threaded",
        action="store_const",
        const="free_threaded",
        dest="assume",
        help="report backend choices as if free-threaded",
    )
    mode_group.add_argument(
        "--assume-gil",
        action="store_const",
        const="gil",
        dest="assume",
        help="report backend choices as if GIL-enabled",
    )
    p_explain.add_argument(
        "--strict",
        action="store_true",
        help="exit nonzero if classification differs from --baseline",
    )
    p_explain.add_argument("--baseline", help="baseline JSON produced by a prior --format=json run")

    p_profile = sub.add_parser(
        "profile", help="run a script and report observed behavior (spec 5.15.2)"
    )
    p_profile.add_argument("script")
    p_profile.add_argument("args", nargs="*")
    p_profile.add_argument(
        "--live", action="store_true", help="stream chunk-completion stats during the run"
    )
    p_profile.add_argument(
        "--per-block",
        action="store_true",
        dest="per_block",
        help="break the report down per marked block",
    )
    p_profile.add_argument(
        "--export", metavar="FILE", help="write the report as JSON instead of text"
    )

    p_run = sub.add_parser(
        "run", help="run a script with Lucen active, rewriting the script itself"
    )
    p_run.add_argument("script")
    p_run.add_argument("args", nargs="*")

    args = parser.parse_args(argv)
    if args.command == "explain":
        return explain.run(
            args.file,
            block=args.block,
            fmt=args.fmt,
            assume=args.assume,
            strict=args.strict,
            baseline_path=args.baseline,
        )
    if args.command == "profile":
        from . import profile

        return profile.run(
            args.script, args.args, per_block=args.per_block, export=args.export, live=args.live
        )
    if args.command == "run":
        from . import run as run_cmd

        return run_cmd.run(args.script, args.args)
    parser.print_help()
    return 0
