"""Run mutation testing over the correctness core and report the survivors.

Usage (from the repo root):

    python tools/mutation_report.py                 # the whole correctness core
    python tools/mutation_report.py --paths lucen/execution/runtime.py
    python tools/mutation_report.py --quick         # unit tests only, faster

Writes mutation-survivors.txt: every mutation no test killed, with its diff.
"""

from __future__ import annotations

import argparse
import atexit
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

# The code the bit-identical guarantee actually rests on. Front-end files are
# included because a mis-analysis silently changes what gets parallelized.
CORE = [
    "lucen/execution/runtime.py",
    "lucen/execution/planning.py",
    "lucen/execution/_accel.py",
    "lucen/execution/dispatch.py",
    "lucen/analysis/purity.py",
    "lucen/analysis/rewriter.py",
    "lucen/analysis/selector.py",
    "lucen/codegen/generator.py",
]


def _capture(cmd: "list[str]", cwd: Path, env: "dict[str, str] | None" = None) -> str:
    """Read a child's output as utf-8; the locale codec cannot decode mutmut."""
    done = subprocess.run(
        cmd, cwd=cwd, env=env, capture_output=True, encoding="utf-8", errors="replace"
    )
    return done.stdout or ""


def fail(message: str) -> None:
    print(f"error: {message}", file=sys.stderr)
    raise SystemExit(1)


def preflight(root: Path) -> None:
    if not (root / "lucen").is_dir():
        fail("run this from the repository root")
    if shutil.which("mutmut") is None:
        fail("mutmut is not installed: pip install 'mutmut==2.5.1'")
    # An interrupted run leaves its own mutant behind, which would otherwise
    # block every later run. Those are recognisable by the .bak mutmut writes
    # next to the file, so clear them and refuse only for real edits.
    leftovers, edited = [], []
    for line in _capture(["git", "status", "--porcelain", "lucen"], cwd=root).splitlines():
        path = line[3:].strip().strip('"')
        if path.endswith(".bak") or (root / (path + ".bak")).exists():
            leftovers.append(path)
        else:
            edited.append(path)
    if edited:
        fail(
            "lucen/ has uncommitted changes, and mutmut edits files in place; "
            "commit or stash first:\n  " + "\n  ".join(edited)
        )
    if leftovers:
        print(f"clearing {len(leftovers)} leftover(s) from an interrupted run")
        restore(root)
    # Tests must import the tree being mutated, not a copy in site-packages.
    imported = _capture(
        [sys.executable, "-c", "import lucen, pathlib; print(pathlib.Path(lucen.__file__).parent)"],
        cwd=root,
    ).strip()
    if Path(imported).resolve() != (root / "lucen").resolve():
        fail(f"tests would import {imported}, not {root / 'lucen'}; pip install -e . first")


def restore(root: Path) -> None:
    """Undo any mutant still on disk.

    mutmut edits in place and leaves both the mutation and a .bak file behind if
    it is interrupted, which turns the next test run into a false failure.
    """
    subprocess.run(["git", "checkout", "--", "lucen"], cwd=root, check=False)
    for backup in (root / "lucen").rglob("*.bak"):
        backup.unlink(missing_ok=True)
    left = _capture(["git", "status", "--porcelain", "lucen"], cwd=root).strip()
    if left:
        print(f"WARNING: lucen/ still modified after restore:\n{left}", file=sys.stderr)


def parse_survivors(text: str) -> "list[str]":
    ids: list[str] = []
    for line in text.splitlines():
        line = line.strip()
        if not line or not re.fullmatch(r"[\d,\s-]+", line):
            continue
        for part in line.split(","):
            part = part.strip()
            if "-" in part:
                lo, hi = part.split("-", 1)
                if lo.isdigit() and hi.isdigit():
                    ids.extend(str(i) for i in range(int(lo), int(hi) + 1))
            elif part.isdigit():
                ids.append(part)
    return ids


def run_suite(quick: bool) -> int:
    """Run the suite once per accel mode, for use as mutmut's runner.

    Pinning one mode makes the other dead code, so mutations in the unused half
    cannot be killed and report as false survivors.
    """
    targets = ["tests/unit"] if quick else ["tests/unit", "tests/property"]
    for disable_native in (False, True):
        env = dict(os.environ)
        env.pop("LUCEN_DISABLE_NATIVE", None)
        if disable_native:
            env["LUCEN_DISABLE_NATIVE"] = "1"
        done = subprocess.run(
            [sys.executable, "-m", "pytest", "-x", "-q", "-p", "no:cacheprovider", *targets],
            env=env,
        )
        if done.returncode != 0:
            return done.returncode
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--paths", nargs="*", default=CORE, help="files to mutate")
    ap.add_argument("--quick", action="store_true", help="unit tests only")
    ap.add_argument("--out", default="mutation-survivors.txt")
    ap.add_argument("--run-suite", action="store_true", help=argparse.SUPPRESS)
    args = ap.parse_args()

    if args.run_suite:
        return run_suite(args.quick)

    root = Path.cwd()
    preflight(root)
    atexit.register(restore, root)

    env = dict(os.environ)
    # mutmut's own output is not cp1252-decodable on Windows.
    env["PYTHONIOENCODING"] = "utf-8"
    quick = " --quick" if args.quick else ""
    runner = f"{sys.executable} tools/mutation_report.py --run-suite{quick}"

    print(f"mutating {len(args.paths)} file(s); runner: {runner}")
    print("this takes a while: each mutant runs the suite once\n")
    started = time.time()
    subprocess.run(
        [
            "mutmut",
            "run",
            "--paths-to-mutate",
            ",".join(args.paths),
            "--runner",
            runner,
            "--tests-dir",
            "tests/",
            "--no-progress",
            "--CI",
        ],
        env=env,
        cwd=root,
        check=False,
    )
    restore(root)

    listing = _capture(["mutmut", "results"], cwd=root, env=env)
    ids = parse_survivors(listing)

    out = root / args.out
    with out.open("w", encoding="utf-8") as fh:
        fh.write(f"survivors: {len(ids)}\nmutated: {', '.join(args.paths)}\nrunner: {runner}\n")
        fh.write(f"elapsed: {time.time() - started:.0f}s\n\n{listing}\n")
        for n, mid in enumerate(ids, 1):
            diff = _capture(["mutmut", "show", mid], cwd=root, env=env)
            fh.write(f"\n===== survivor {mid} ({n}/{len(ids)}) =====\n{diff}")
            print(f"  collected diff {n}/{len(ids)}", end="\r")

    restore(root)
    print(f"\n{len(ids)} survivors written to {out}")
    print("send me that file; many survivors are equivalent mutants, not gaps")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
