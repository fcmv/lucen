from __future__ import annotations

import importlib
import sys
import textwrap

import pytest

from lucen import import_hook
from lucen.execution import dispatch
from lucen.support import config
from lucen.support.errors import (
    ClauseValueError,
    ErrorsMode,
    clear_fallback_report,
    get_fallback_report,
    set_errors_mode,
)

MODULE_OK = """\
def prepare(n):
    return list(range(n))

def run_map(xs):
    ys = [0] * len(xs)
    # LUCEN START calibrate=false
    for i in range(len(xs)):
        ys[i] = xs[i] * 3 + 1
    # LUCEN END
    return ys, i

def run_reduction(vals):
    total = 0.25
    # LUCEN START calibrate=false
    for i in range(len(vals)):
        total += vals[i] * 1.0001
    # LUCEN END
    return total

def run_monotonic(xs):
    out = [0] * len(xs)
    out[0] = 1
    # LUCEN START
    for i in range(1, len(xs)):
        out[i] = out[i - 1] + xs[i]
    # LUCEN END
    return out
"""

MODULE_PLAIN = """\
VALUE = sum(range(10))
"""

MODULE_BAD_CLAUSE = """\
def f(xs):
    ys = [0] * len(xs)
    # LUCEN START backend=threed
    for i in range(len(xs)):
        ys[i] = xs[i]
    # LUCEN END
    return ys
"""

MODULE_TRUST = """\
# LUCEN TRUST
def poke(state, i, v):
    state[i] = v
    return v

def run(xs, out):
    # LUCEN START calibrate=false
    for i in range(len(xs)):
        out[i] = poke(out, i, xs[i])
    # LUCEN END
    return out
"""


@pytest.fixture()
def project(tmp_path):
    set_errors_mode(ErrorsMode.REPORT)
    clear_fallback_report()
    dispatch.reset_runtime_state()
    config.set_active(config.Config())
    import_hook.uninstall()
    import_hook.install(str(tmp_path))
    sys.path.insert(0, str(tmp_path))
    yield tmp_path
    sys.path.remove(str(tmp_path))
    import_hook.uninstall()
    for name in list(sys.modules):
        if name.startswith("plxmod_"):
            del sys.modules[name]
    config.set_active(config.Config())
    set_errors_mode(ErrorsMode.REPORT)
    clear_fallback_report()


def _write(project, name, content):
    path = project / f"{name}.py"
    path.write_text(textwrap.dedent(content), encoding="utf-8")
    return name


def test_end_to_end_parallel_module(project):
    name = _write(project, "plxmod_ok", MODULE_OK)
    mod = importlib.import_module(name)
    xs = mod.prepare(4000)
    ys, final_i = mod.run_map(xs)
    assert ys == [v * 3 + 1 for v in xs]
    assert final_i == 3999
    vals = [0.1 * k + 0.007 for k in range(2000)]
    expected = 0.25
    for v in vals:
        expected += v * 1.0001
    assert mod.run_reduction(vals) == expected
    xs2 = list(range(50))
    out = mod.run_monotonic(xs2)
    golden = [0] * 50
    golden[0] = 1
    for i in range(1, 50):
        golden[i] = golden[i - 1] + xs2[i]
    assert out == golden
    stats = dispatch.get_block_stats()
    assert any(s["parallel_runs"] >= 1 for s in stats.values())


def test_comment_invariant_passthrough(project):
    name = _write(project, "plxmod_plain", MODULE_PLAIN)
    mod = importlib.import_module(name)
    assert mod.VALUE == 45
    assert not (project / ".lucen_cache").exists()


def test_clause_error_raises_at_import(project):
    name = _write(project, "plxmod_bad", MODULE_BAD_CLAUSE)
    with pytest.raises(ClauseValueError):
        importlib.import_module(name)


def test_cache_round_trip(project, monkeypatch):
    name = _write(project, "plxmod_cached", MODULE_OK)
    importlib.import_module(name)
    assert (project / ".lucen_cache").exists()
    del sys.modules[name]

    def boom(*_a, **_k):
        raise AssertionError("pipeline must not re-run on a cache hit")

    monkeypatch.setattr(import_hook, "rewrite_module", boom)
    mod = importlib.import_module(name)
    ys, _ = mod.run_map(mod.prepare(600))
    assert ys == [v * 3 + 1 for v in range(600)]


def test_trusted_call_with_shared_arg_stays_sequential(project):
    name = _write(project, "plxmod_trust", MODULE_TRUST)
    mod = importlib.import_module(name)
    out = mod.run(list(range(200)), [0] * 200)
    assert out == list(range(200))
    assert any(r.error == "TrustedArgumentError" for r in get_fallback_report())
    assert dispatch.get_block_stats() == {}


def test_scope_exclude_leaves_module_alone(project):
    config.set_active(config.Config(scope_exclude=("skipme*",)))
    name = _write(project, "skipme_plxmod", MODULE_OK.replace("plxmod", "skip"))
    mod = importlib.import_module(name)
    ys, _ = mod.run_map(mod.prepare(100))
    assert ys == [v * 3 + 1 for v in range(100)]
    assert dispatch.get_block_stats() == {}
