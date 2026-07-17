from __future__ import annotations

import os

from lucen.support.errors import report_fallback


def _apply_affinity(spec, workers: int) -> None:
    cv = spec.clauses.get("affinity")
    if cv is None:
        return
    if not hasattr(os, "sched_setaffinity"):
        report_fallback(
            "affinity= is not supported on this platform "
            "(Linux-only in practice, spec 7); OS default "
            "scheduling kept",
            file=spec.filename,
            line=spec.line,
            error="AffinityUnsupported",
        )
        return
    cpu_total = os.cpu_count() or 1
    if cv.kind == "name":
        if cv.value == "compact":
            cores = set(range(min(workers, cpu_total)))
        else:
            stride = max(1, cpu_total // max(workers, 1))
            cores = set(range(0, cpu_total, stride))
    else:
        cores = {item.value for item in cv.value.kwargs["cores"].value}
    try:
        os.sched_setaffinity(0, cores)
    except OSError as exc:
        report_fallback(
            f"affinity= could not be applied ({exc})",
            file=spec.filename,
            line=spec.line,
            error="AffinityUnsupported",
        )
