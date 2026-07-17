from __future__ import annotations

import sys
import textwrap

import pytest

from lucen import import_hook
from lucen.execution import dispatch
from lucen.support import config
from lucen.support.errors import (
    ClauseValueError,
    UnresolvedDependencyShapeError,
    clear_fallback_report,
    get_fallback_report,
)

_MOD_COUNTER = [0]


@pytest.fixture()
def project(tmp_path):
    clear_fallback_report()
    dispatch.reset_runtime_state()
    config.set_active(config.Config())
    import_hook.uninstall()
    import_hook.install(str(tmp_path))
    sys.path.insert(0, str(tmp_path))
    created: list = []

    def write(name, body):
        (tmp_path / f"{name}.py").write_text(textwrap.dedent(body), encoding="utf-8")
        created.append(name)

    yield tmp_path, write

    sys.path.remove(str(tmp_path))
    import_hook.uninstall()
    for name in created:
        sys.modules.pop(name, None)
    config.set_active(config.Config())
    clear_fallback_report()
    dispatch.reset_runtime_state()


def _uniq(prefix):
    _MOD_COUNTER[0] += 1
    return f"{prefix}_{_MOD_COUNTER[0]}"


def test_naive_map_reduction_dag(project):
    root, write = project
    helpers = _uniq("helpers")
    write(
        helpers,
        """
        def transform(x):
            return x * x - 3 * x + 7
    """,
    )
    mod = _uniq("naivework")
    write(
        mod,
        f"""
        from {helpers} import transform

        def run_map(xs):
            ys = [0] * len(xs)
            # LUCEN START calibrate=false
            for i in range(len(xs)):
                ys[i] = transform(xs[i])
            # LUCEN END
            return ys, i

        def run_reduction(vals):
            total = 0.0
            # LUCEN START calibrate=false
            for i in range(len(vals)):
                total += vals[i] * 1.5
            # LUCEN END
            return total

        def run_dag(n):
            out = [1.0] + [0.0] * (n - 1)
            w = [float(k) for k in range(n)]
            # LUCEN START calibrate=false
            for i in range(1, n):
                out[i] = out[i // 2] + w[i]
            # LUCEN END
            return out
    """,
    )
    work = __import__(mod)
    tr = __import__(helpers).transform

    xs = list(range(3000))
    ys, last = work.run_map(xs)
    assert ys == [tr(v) for v in xs] and last == 2999

    vals = [0.1 * k + 0.03 for k in range(2000)]
    exp = 0.0
    for v in vals:
        exp += v * 1.5
    assert work.run_reduction(vals) == exp

    out = work.run_dag(1500)
    g = [1.0] + [0.0] * 1499
    w = [float(k) for k in range(1500)]
    for i in range(1, 1500):
        g[i] = g[i // 2] + w[i]
    assert out == g


def test_expert_custom_reduce_with_dotted_fn(project):
    root, write = project
    mod = _uniq("expertwork")
    write(
        mod,
        """
        def run(xs):
            total = 0
            # LUCEN START calibrate=false, reduce=custom(fn=operator.add, identity=0)
            for i in range(len(xs)):
                total += xs[i]
            # LUCEN END
            return total
    """,
    )
    work = __import__(mod)
    nums = list(range(1, 4001))
    assert work.run(nums) == sum(nums)


def test_trusted_shared_mutation_is_correct(project):
    root, write = project
    helpers = _uniq("th")
    write(
        helpers,
        """
        # LUCEN TRUST args=unchecked
        def poke(state, i, v):
            state[i] = v
            return v
    """,
    )
    mod = _uniq("trustwork")
    write(
        mod,
        f"""
        from {helpers} import poke

        def run(xs, out):
            # LUCEN START calibrate=false
            for i in range(len(xs)):
                poke(out, i, xs[i] * 2)
            # LUCEN END
            return out
    """,
    )
    work = __import__(mod)
    n = 800
    out = work.run(list(range(n)), [0] * n)
    assert out == [v * 2 for v in range(n)]


def test_malformed_clause_fails_at_import(project):
    root, write = project
    mod = _uniq("badwork")
    write(
        mod,
        """
        def run(xs, ys):
            # LUCEN START backend=threed
            for i in range(len(xs)):
                ys[i] = xs[i]
            # LUCEN END
            return ys
    """,
    )
    with pytest.raises(ClauseValueError):
        __import__(mod)


def test_strict_block_fails_at_import(project):
    root, write = project
    mod = _uniq("strictwork")
    write(
        mod,
        """
        def run(xs, out, idx):
            # LUCEN START calibrate=false, strict=true
            for i in range(len(xs)):
                out[idx[i]] = xs[i]
            # LUCEN END
            return out
    """,
    )
    with pytest.raises(UnresolvedDependencyShapeError):
        __import__(mod)


def test_write_conflict_reruns_transparently(project):
    root, write = project
    mod = _uniq("conflictwork")
    write(
        mod,
        """
        def run(xs, out, dst):
            # LUCEN START calibrate=false, depend=none
            for i in range(len(xs)):
                out[dst[i]] = xs[i]
            # LUCEN END
            return out
    """,
    )
    work = __import__(mod)
    n = 200
    dst = [0] * n
    out = work.run(list(range(1, n + 1)), [-1] * n, dst)
    g = [-1] * n
    for i in range(n):
        g[dst[i]] = i + 1
    assert out == g
    assert any(r.error == "ParallelWriteConflictError" for r in get_fallback_report())


def test_comment_invariant_without_activate(project):
    root, write = project
    mod = _uniq("plainwork")
    write(
        mod,
        """
        def compute(xs):
            ys = [0] * len(xs)
            # LUCEN START
            for i in range(len(xs)):
                ys[i] = xs[i] * xs[i]
            # LUCEN END
            return ys
    """,
    )
    import_hook.uninstall()
    work = __import__(mod)
    assert work.compute([1, 2, 3, 4]) == [1, 4, 9, 16]
