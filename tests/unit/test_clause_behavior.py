from __future__ import annotations

import ast
import copy

import pytest

from lucen.analysis.rewriter import analyze_source
from lucen.analysis.scanner import scan_source
from lucen.analysis.selector import select
from lucen.codegen import generate
from lucen.execution import dispatch
from lucen.execution.dispatch import execute, make_spec
from lucen.support import config
from lucen.support.config import Config, clamp
from lucen.support.errors import (
    ErrorsMode,
    UnprofitableParallelismError,
    clear_fallback_report,
    get_fallback_report,
    set_errors_mode,
)


@pytest.fixture(autouse=True)
def _clean_state():
    set_errors_mode(ErrorsMode.REPORT)
    clear_fallback_report()
    dispatch.reset_runtime_state()
    config.set_active(Config())
    yield
    set_errors_mode(ErrorsMode.REPORT)
    clear_fallback_report()
    dispatch.reset_runtime_state()
    config.set_active(Config())


def build(src):
    scan = scan_source(src, "t.py")
    analysis = analyze_source(src, scan, "t.py")[0]
    decision = select(analysis, workers=8)
    artifact = generate(analysis, decision, "t.py")
    return analysis, decision, artifact


def run(src, env, backend="thread"):
    analysis, decision, artifact = build(src)
    assert artifact is not None
    spec = make_spec(analysis, decision, artifact)
    env = copy.deepcopy(env)
    it = analysis.for_node.iter
    if isinstance(it, ast.Call) and isinstance(it.func, ast.Name) and it.func.id == "enumerate":
        iterable = eval(ast.unparse(it.args[0]), dict(env))
    else:
        iterable = eval(ast.unparse(it), dict(env))
    result = execute(spec, iterable, env, force_backend=backend)
    return env, result, spec


def golden(src, env):
    g = copy.deepcopy(env)
    exec(src, g)
    return g


def block(body, header="for i in range(len(xs)):", clauses="calibrate=false"):
    body_src = "\n".join("    " + b for b in body)
    return f"# LUCEN START {clauses}\n{header}\n{body_src}\n# LUCEN END\n"


def test_backend_pool_size_parity():
    body = ["ys[i] = xs[i] * 2"]
    env = {"xs": list(range(2000)), "ys": [0] * 2000}
    naive, _, _ = run(block(body), env)
    expert, _, _ = run(
        block(body, clauses="calibrate=false, backend=thread(pool_size=4, chunks=16)"), env
    )
    assert naive["ys"] == expert["ys"] == golden(block(body), env)["ys"]


def test_reduction_order_parity_bit_identical():
    body = ["total += xs[i] * 1.0001"]
    env = {"xs": [0.1 * k + 0.03 for k in range(3000)], "total": 0.0}
    naive, _, _ = run(block(body), env)
    explicit, _, _ = run(
        block(body, clauses="calibrate=false, reduction_order=sequential_equivalent"), env
    )
    assert naive["total"] == explicit["total"] == golden(block(body), env)["total"]


def test_grainsize_parity_on_dag():
    body = ["out[i] = out[i // 2] + w[i]"]
    header = "for i in range(1, n):"
    env = {"n": 1000, "out": [1] + [0] * 999, "w": list(range(1000))}
    src_a = block(body, header, "calibrate=false, grainsize=2")
    src_b = block(body, header, "calibrate=false, grainsize=128")
    got_a, _, _ = run(src_a, env)
    got_b, _, _ = run(src_b, env)
    assert got_a["out"] == got_b["out"] == golden(src_a, env)["out"]


def test_calibrate_static_uses_prescreen_only():
    src = block(["ys[i] = xs[i] + 1"], "for i in range(200):", clauses="calibrate=static")
    env = {"xs": list(range(200)), "ys": [0] * 200}
    _, _, spec = run(src, env)
    stats = dispatch.get_block_stats()[spec.key]
    assert stats["probe_ns"] is None
    assert stats["sequential_runs"] == 1


def test_calibrate_always_reprobes_each_call():
    src = block(["ys[i] = big(xs[i])"], "for i in range(len(xs)):", clauses="calibrate=always")
    analysis, decision, artifact = build(src)
    spec = make_spec(analysis, decision, artifact)
    env = {"xs": list(range(4000)), "ys": [0] * 4000, "big": lambda v: v * 2 + 1}
    for _ in range(3):
        execute(spec, range(4000), copy.deepcopy(env), force_backend="thread")
    assert spec.key not in dispatch._memo


def test_calibrate_threshold_min_gain_gates():
    src = block(
        ["ys[i] = xs[i] * 2"],
        "for i in range(5000):",
        clauses="calibrate=threshold(min_gain=100000.0)",
    )
    env = {"xs": list(range(5000)), "ys": [0] * 5000}
    got, _, spec = run(src, env)
    assert got["ys"] == golden(src, env)["ys"]
    assert dispatch.get_block_stats()[spec.key]["parallel_runs"] == 0


def test_on_fallback_hard_promotes_unprofitable():
    src = block(["ys[i] = xs[i] + 1"], "for i in range(150):", clauses="on_fallback=hard")
    env = {"xs": list(range(150)), "ys": [0] * 150}
    analysis, decision, artifact = build(src)
    spec = make_spec(analysis, decision, artifact)
    with pytest.raises(UnprofitableParallelismError):
        execute(spec, range(150), env, force_backend="thread")


def test_on_fallback_custom_handler_receives_reason():
    seen = []
    src = block(
        ["ys[i] = xs[i] + 1"], "for i in range(150):", clauses="on_fallback=custom(handler=cb)"
    )
    env = {"xs": list(range(150)), "ys": [0] * 150, "cb": lambda rec: seen.append(rec.error)}
    run(src, env)
    assert "PARALLEL_UNPROFITABLE" in seen


def test_progress_callback_reports_completed_total():
    marks = []
    src = block(
        ["ys[i] = xs[i] * 2"],
        "for i in range(len(xs)):",
        clauses="calibrate=false, progress=callback(cb)",
    )
    env = {
        "xs": list(range(400)),
        "ys": [0] * 400,
        "cb": lambda done, total: marks.append((done, total)),
    }
    run(src, env)
    assert marks
    assert marks[-1] == (400, 400)
    assert all(t == 400 for _, t in marks)


def test_clamp_reports_and_caps():
    assert clamp(64, 8, "pool_size", "f.py", 3) == 8
    recs = get_fallback_report()
    assert any(r.error == "LimitClamp" for r in recs)


def test_clamp_noop_under_ceiling():
    assert clamp(4, 8, "pool_size", "f.py", 3) == 4
    assert not get_fallback_report()


def test_defaults_fill_only_gaps(tmp_path):
    toml = tmp_path / "lucen.toml"
    toml.write_text('[defaults]\npool_size = 3\ncalibrate = "false"\n', encoding="utf-8")
    cfg = config.load(str(toml))
    assert cfg.default_for("pool_size") == 3
    assert cfg.default_for("calibrate") == "false"


def test_limits_veto_experimental(tmp_path):
    toml = tmp_path / "lucen.toml"
    toml.write_text(
        '[limits]\nallow_experimental = false\n[experimental]\nenabled = ["early_exit"]\n',
        encoding="utf-8",
    )
    cfg = config.load(str(toml))
    assert cfg.experimental == frozenset()


def test_ci_mode_forces_strict(tmp_path):
    toml = tmp_path / "lucen.toml"
    toml.write_text('[strict]\nci_mode = true\nallow = ["monotonic"]\n', encoding="utf-8")
    cfg = config.load(str(toml))
    assert cfg.strict_default is True
    assert cfg.strict_allow == frozenset()


def test_unknown_config_key_rejected(tmp_path):
    from lucen.support.errors import ClauseValueError

    toml = tmp_path / "lucen.toml"
    toml.write_text("[defaults]\nprocess_wait = true\n", encoding="utf-8")
    with pytest.raises(ClauseValueError):
        config.load(str(toml))


def test_malformed_toml_raises_branded_error(tmp_path):
    from lucen.support.errors import ClauseValueError

    toml = tmp_path / "lucen.toml"
    toml.write_text("[[[ not toml at all\nmode = = ??\n", encoding="utf-8")
    with pytest.raises(ClauseValueError) as info:
        config.load(str(toml))
    assert "not valid TOML" in str(info.value)


@pytest.mark.parametrize(
    "body",
    [
        "[defaults]\npool_size = 0\n",
        "[defaults]\nchunks = -4\n",
        "[limits]\nmax_threads_per_block = 0\n",
        "[limits]\nmax_processes_per_block = -1\n",
        "[limits]\nmax_timeout_seconds = 0\n",
    ],
)
def test_degenerate_toml_values_rejected(tmp_path, body):
    from lucen.support.errors import ClauseValueError

    toml = tmp_path / "lucen.toml"
    toml.write_text(body, encoding="utf-8")
    with pytest.raises(ClauseValueError):
        config.load(str(toml))


def test_unknown_section_rejected(tmp_path):
    from lucen.support.errors import ClauseValueError

    toml = tmp_path / "lucen.toml"
    toml.write_text("[nonsense]\nx = 1\n", encoding="utf-8")
    with pytest.raises(ClauseValueError):
        config.load(str(toml))


def test_invalid_errors_mode_rejected(tmp_path):
    from lucen.support.errors import ClauseValueError

    toml = tmp_path / "lucen.toml"
    toml.write_text('[errors]\nmode = "loud"\n', encoding="utf-8")
    with pytest.raises(ClauseValueError):
        config.load(str(toml))


def test_pool_size_default_from_config_applied(tmp_path):
    toml = tmp_path / "lucen.toml"
    toml.write_text("[defaults]\npool_size = 2\n", encoding="utf-8")
    config.set_active(config.load(str(toml)))
    src = block(["ys[i] = xs[i] * 2"], "for i in range(len(xs)):")
    env = {"xs": list(range(4000)), "ys": [0] * 4000}
    got, _, spec = run(src, env)
    assert got["ys"] == golden(src, env)["ys"]
    assert dispatch.get_block_stats()[spec.key]["workers"] <= 2
