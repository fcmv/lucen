from __future__ import annotations

import json
import os
import sys
import threading
import time
from typing import Any, Dict, List, Optional


def run(
    script: str,
    argv: List[str],
    per_block: bool = False,
    export: Optional[str] = None,
    live: bool = False,
) -> int:
    import lucen
    from lucen import import_hook
    from lucen.execution import dispatch
    from lucen.support.errors import get_fallback_report

    lucen.activate()
    stop_live = threading.Event()
    if live:
        threading.Thread(target=_live_loop, args=(stop_live,), daemon=True).start()

    old_argv = sys.argv
    sys.argv = [script] + list(argv)
    wall0 = time.perf_counter()
    cpu0 = time.process_time()
    exit_code = 0
    try:
        import_hook.run_path(script, run_name="__main__")
    except SystemExit as exc:
        exit_code = int(exc.code or 0)
    finally:
        sys.argv = old_argv
        stop_live.set()
    wall = time.perf_counter() - wall0
    cpu = time.process_time() - cpu0

    report = _build_report(script, wall, cpu, dispatch.get_block_stats(), get_fallback_report())
    if export:
        with open(export, "w", encoding="utf-8") as f:
            json.dump(report, f, indent=2)
        print(f"lucen profile: report written to {export}")
    else:
        print(_render(report, per_block))
    return exit_code


def _build_report(script: str, wall: float, cpu: float, stats, fallbacks) -> Dict[str, Any]:
    return {
        "script": script,
        "wall_seconds": round(wall, 4),
        "parent_cpu_seconds": round(cpu, 4),
        "parent_cpu_ratio": round(cpu / wall, 3) if wall > 0 else None,
        "cpu_note": "parent process only; PROCESS-backend worker CPU runs in "
        "children and is not attributable cross-platform in v1",
        "cpu_count": os.cpu_count(),
        "blocks": {f"{file}:{line}": dict(entry) for (file, line), entry in sorted(stats.items())},
        "fallbacks": [
            {"error": r.error, "file": r.file, "line": r.line, "message": r.message}
            for r in fallbacks
        ],
    }


def _render(report: Dict[str, Any], per_block: bool) -> str:
    blocks = report["blocks"]
    parallel_runs = sum(b["parallel_runs"] for b in blocks.values())
    sequential_runs = sum(b["sequential_runs"] for b in blocks.values())
    peak_workers = max((b["workers"] for b in blocks.values()), default=0)
    lines = [
        f"lucen profile: {report['script']}",
        f"  Wall time: {report['wall_seconds']} s   "
        f"parent CPU ratio: {report['parent_cpu_ratio']} "
        f"({report['cpu_note']})",
        f"  Blocks dispatched: {len(blocks)}   parallel runs: {parallel_runs}"
        f"   sequential runs: {sequential_runs}   peak workers: {peak_workers}",
        f"  Fallbacks: {len(report['fallbacks'])}",
    ]
    for fb in report["fallbacks"]:
        lines.append(f"    {fb['file']}:{fb['line']}: {fb['error']}: {fb['message']}")
    if per_block:
        for key, entry in blocks.items():
            probe = f"{entry['probe_ns']:.0f} ns/iter" if entry.get("probe_ns") else "not probed"
            lines.append(
                f"  Block {key}: backend={entry['backend'] or 'n/a'} "
                f"runs={entry['runs']} parallel={entry['parallel_runs']} "
                f"sequential={entry['sequential_runs']} "
                f"chunks={entry['chunks']} workers={entry['workers']} "
                f"probe={probe}"
            )
    return "\n".join(lines)


def _live_loop(stop: threading.Event) -> None:
    from lucen.execution import dispatch

    while not stop.wait(0.5):
        stats = dispatch.get_block_stats()
        if stats:
            done = sum(s["chunks"] for s in stats.values())
            print(f"lucen: {len(stats)} block(s), {done} chunk(s) completed", file=sys.stderr)
