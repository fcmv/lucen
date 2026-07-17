from __future__ import annotations

import array
import ast
import copy
import sys
import types

import pytest

from lucen.analysis.rewriter import analyze_source
from lucen.analysis.scanner import scan_source
from lucen.analysis.selector import select
from lucen.codegen import generate
from lucen.execution import dispatch
from lucen.execution.dispatch import execute, make_spec
from lucen.support import config
from lucen.support.errors import (
    ErrorsMode,
    clear_fallback_report,
    get_fallback_report,
    set_errors_mode,
)


def module_double(v):
    return v * 2


def module_str(v):
    return str(v)


class UnpicklableBoom(Exception):
    def __init__(self, msg):
        super().__init__(msg)
        self.payload = lambda: None


def module_poke(v):
    if v == 333:
        raise UnpicklableBoom("boom-333")
    return v + 1


@pytest.fixture(autouse=True)
def _clean_state():
    set_errors_mode(ErrorsMode.REPORT)
    clear_fallback_report()
    dispatch.reset_runtime_state()
    config.set_active(config.Config())
    yield
    set_errors_mode(ErrorsMode.REPORT)
    clear_fallback_report()
    dispatch.reset_runtime_state()
    config.set_active(config.Config())


def build(src: str):
    scan = scan_source(src, "t.py")
    analyses = analyze_source(src, scan, "t.py")
    analysis = analyses[0]
    decision = select(analysis, workers=8)
    artifact = generate(analysis, decision, "t.py")
    assert artifact is not None
    return analysis, make_spec(analysis, decision, artifact)


def run(src: str, env: dict):
    analysis, spec = build(src)
    env = copy.deepcopy(env)
    if spec.artifact.domain == "range":
        iterable = eval(ast.unparse(analysis.for_node.iter), dict(env))
    elif spec.artifact.domain == "enumerate":
        iterable = eval(ast.unparse(analysis.for_node.iter.args[0]), dict(env))
    else:
        iterable = eval(ast.unparse(analysis.for_node.iter), dict(env))
    result = execute(spec, iterable, env, force_backend="process")
    return env, result, spec


def golden(src: str, env: dict) -> dict:
    g = copy.deepcopy(env)
    exec(src, g)
    return g


def block(body_lines, header, clauses="calibrate=false"):
    body = "\n".join("    " + line for line in body_lines)
    return f"# LUCEN START {clauses}\n{header}\n{body}\n# LUCEN END\n"


def test_process_map_equivalence():
    src = block(["ys[i] = xs[i] * 3 + 1"], "for i in range(len(xs)):")
    env = {"xs": list(range(2000)), "ys": [0] * 2000}
    p, result, spec = run(src, env)
    assert p["ys"] == golden(src, env)["ys"]
    assert result == (1999,)
    assert dispatch.get_block_stats()[spec.key]["backend"] == "process"


def test_process_reduction_bit_identity():
    src = block(["total += vals[i] * 1.0001"], "for i in range(len(vals)):")
    env = {"vals": [0.1 * k + 0.003 for k in range(1500)], "total": 0.5}
    p, result, _ = run(src, env)
    g = golden(src, env)
    assert p["total"] == g["total"]
    assert result == (1499, g["total"])


def test_process_sequence_domain_offset_view():
    src = block(["cache[key] = key * 2"], "for key in keys:")
    env = {"keys": [f"k{n}" for n in range(60)], "cache": {}}
    p, _, _ = run(src, env)
    assert list(p["cache"].items()) == list(golden(src, env)["cache"].items())


def test_unpicklable_argument_falls_back_before_workers():
    src = block(["ys[i] = transform(xs[i])"], "for i in range(len(xs)):")
    env = {"xs": list(range(40)), "ys": [0] * 40, "transform": lambda v: v + 1}
    p, _, spec = run(src, env)
    assert p["ys"] == [v + 1 for v in range(40)]
    assert any(r.error == "PreflightCheckError" for r in get_fallback_report())
    assert dispatch.get_block_stats()[spec.key]["sequential_runs"] == 1


def test_module_arguments_ship_by_reference():
    import math

    src = block(["ys[i] = math.sqrt(xs[i]) + 1"], "for i in range(len(xs)):")
    plain = {"xs": list(range(500)), "ys": [0.0] * 500}
    analysis, spec = build(src)
    env = {**copy.deepcopy(plain), "math": math}
    execute(spec, range(500), env, force_backend="process")
    g = {**copy.deepcopy(plain), "math": math}
    exec(src, g)
    assert env["ys"] == g["ys"]
    assert dispatch.get_block_stats()[spec.key]["parallel_runs"] == 1


def test_process_fail_fast_prefix():
    src = block(["ys[i] = 100 // xs[i]"], "for i in range(len(xs)):")
    xs = [1] * 400
    xs[251] = 0
    env = {"xs": xs, "ys": [-1] * 400}
    analysis, spec = build(src)
    run_env = copy.deepcopy(env)
    with pytest.raises(ZeroDivisionError):
        execute(spec, range(400), run_env, force_backend="process")
    g = copy.deepcopy(env)
    try:
        exec(src, g)
    except ZeroDivisionError:
        pass
    assert run_env["ys"][:251] == g["ys"][:251]
    assert run_env["ys"][251] == -1


def test_sliced_input_map_equivalence():
    src = block(["ys[i] = xs[i] * 3 + 1"], "for i in range(len(xs)):")
    _, spec = build(src)
    assert spec.artifact.sliceable == ("xs",)
    env = {"xs": list(range(2000)), "ys": [0] * 2000}
    p, _, _ = run(src, env)
    assert p["ys"] == golden(src, env)["ys"]


def test_sliced_input_offset_range():
    src = block(["out[i] = xs[i] * 2"], "for i in range(1, n):")
    _, spec = build(src)
    assert spec.artifact.sliceable == ("xs",)
    env = {"n": 1500, "xs": list(range(1500)), "out": [0] * 1500}
    p, _, _ = run(src, env)
    assert p["out"] == golden(src, env)["out"]


def test_sliced_input_multiple_arrays():
    src = block(["ys[i] = xs[i] + zs[i]"], "for i in range(len(xs)):")
    _, spec = build(src)
    assert spec.artifact.sliceable == ("xs", "zs")
    env = {"xs": list(range(3000)), "zs": list(range(1000, 4000)), "ys": [0] * 3000}
    p, _, _ = run(src, env)
    assert p["ys"] == golden(src, env)["ys"]


def test_neighbour_read_not_sliced_still_correct():
    src = block(
        ["ys[i] = xs[i - 1] + xs[i]"],
        "for i in range(1, len(xs)):",
        clauses="calibrate=false, depend=none",
    )
    _, spec = build(src)
    assert spec.artifact.sliceable == ()
    env = {"xs": list(range(2000)), "ys": [0] * 2000}
    p, _, _ = run(src, env)
    assert p["ys"] == golden(src, env)["ys"]


def test_bare_use_disqualifies_slicing():
    src = block(["ys[i] = xs[i] + len(xs)"], "for i in range(len(xs)):")
    _, spec = build(src)
    assert spec.artifact.sliceable == ()
    env = {"xs": list(range(1000)), "ys": [0] * 1000}
    p, _, _ = run(src, env)
    assert p["ys"] == golden(src, env)["ys"]


def _typed_buffers_on():
    config.set_active(config.Config(experimental=frozenset({"typed_buffers"})))


def test_dense_float_array_typed_slab_bit_identical():
    _typed_buffers_on()
    src = block(["ys[i] = xs[i] * 2.0 + 1.0"], "for i in range(len(xs)):")
    _, spec = build(src)
    assert spec.artifact.dense
    env = {
        "xs": array.array("d", [0.1 * k for k in range(3000)]),
        "ys": array.array("d", bytes(8 * 3000)),
    }
    p, _, _ = run(src, env)
    assert isinstance(p["ys"], array.array)
    assert list(p["ys"]) == list(golden(src, env)["ys"])


def test_dense_int_array_coercion_matches_sequential():
    _typed_buffers_on()
    src = block(["ys[i] = xs[i] * 3"], "for i in range(len(xs)):")
    env = {"xs": array.array("i", list(range(2500))), "ys": array.array("i", bytes(4 * 2500))}
    p, _, _ = run(src, env)
    assert list(p["ys"]) == list(golden(src, env)["ys"])


def test_dense_bytearray_typed_slab():
    _typed_buffers_on()
    src = block(["ys[i] = (xs[i] * 2) % 256"], "for i in range(len(xs)):")
    _, spec = build(src)
    assert spec.artifact.dense
    env = {"xs": bytearray(range(200)) * 5, "ys": bytearray(1000)}
    p, _, _ = run(src, env)
    assert bytes(p["ys"]) == bytes(golden(src, env)["ys"])


def test_control_flow_buffer_not_dense_falls_back():
    _typed_buffers_on()
    src = block(["if xs[i] > 500:", "    ys[i] = xs[i] * 2.0"], "for i in range(len(xs)):")
    _, spec = build(src)
    assert spec.artifact.dense is False
    xs = array.array("d", [float(k) for k in range(2000)])
    env = {"xs": xs, "ys": array.array("d", [9.0] * 2000)}
    p, _, _ = run(src, env)
    assert list(p["ys"]) == list(golden(src, env)["ys"])


def test_bad_value_into_typed_slab_raises_not_crashes():
    _typed_buffers_on()
    src = block(["ys[i] = tag(xs[i])"], "for i in range(len(xs)):")
    env = {
        "xs": array.array("d", [float(k) for k in range(400)]),
        "ys": array.array("d", bytes(8 * 400)),
        "tag": module_str,
    }
    analysis, spec = build(src)
    with pytest.raises(TypeError):
        execute(spec, range(400), copy.deepcopy(env), force_backend="process")


def test_typed_buffers_off_by_default_uses_list_slab_still_correct():
    assert "typed_buffers" not in config.active().experimental
    src = block(["ys[i] = xs[i] * 2.0 + 1.0"], "for i in range(len(xs)):")
    env = {
        "xs": array.array("d", [0.1 * k for k in range(3000)]),
        "ys": array.array("d", bytes(8 * 3000)),
    }
    p, _, _ = run(src, env)
    assert list(p["ys"]) == list(golden(src, env)["ys"])


def _fake_main(monkeypatch, tmp_path, source):
    from lucen.execution import process_backend

    entry = tmp_path / "entry_script.py"
    entry.write_text(source, encoding="utf-8")
    fake = types.ModuleType("__main__")
    fake.__file__ = str(entry)
    monkeypatch.setitem(sys.modules, "__main__", fake)
    process_backend._spawn_safety.clear()


def test_unguarded_entry_script_falls_back_sequential(monkeypatch, tmp_path):
    from lucen.execution import process_backend

    _fake_main(monkeypatch, tmp_path, "import math\nprint('top-level work')\nrun_everything()\n")
    src = block(["ys[i] = xs[i] + 1"], "for i in range(len(xs)):")
    env = {"xs": list(range(300)), "ys": [0] * 300}
    p, _, spec = run(src, env)
    assert p["ys"] == golden(src, env)["ys"]
    # The unguarded-__main__ refusal only applies under the spawn start method,
    # where every worker re-imports the entry module. Under fork and forkserver
    # the script is not re-executed, so the block runs in parallel with no
    # refusal; gate the spawn-only expectation on the platform's actual method.
    safe, _ = process_backend._spawn_entry_safe()
    if not safe:
        assert any(
            r.error == "PreflightCheckError" and "__main__" in r.message
            for r in get_fallback_report()
        )
        assert dispatch.get_block_stats()[spec.key]["sequential_runs"] == 1


def test_guarded_entry_script_spawns(monkeypatch, tmp_path):
    _fake_main(
        monkeypatch, tmp_path, 'def main():\n    pass\n\nif __name__ == "__main__":\n    main()\n'
    )
    src = block(["ys[i] = xs[i] + 1"], "for i in range(len(xs)):")
    env = {"xs": list(range(300)), "ys": [0] * 300}
    p, _, spec = run(src, env)
    assert p["ys"] == golden(src, env)["ys"]
    assert dispatch.get_block_stats()[spec.key]["backend"] == "process"


def test_effect_free_entry_script_spawns(monkeypatch, tmp_path):
    _fake_main(
        monkeypatch,
        tmp_path,
        '"""doc"""\nimport os\nLIMIT = 42\nNAMES = ["a", "b"]\ndef helper():\n    return LIMIT\n',
    )
    src = block(["ys[i] = xs[i] + 1"], "for i in range(len(xs)):")
    env = {"xs": list(range(300)), "ys": [0] * 300}
    p, _, spec = run(src, env)
    assert p["ys"] == golden(src, env)["ys"]
    assert dispatch.get_block_stats()[spec.key]["backend"] == "process"


def test_repl_entry_without_file_spawns(monkeypatch):
    from lucen.execution import process_backend

    fake = types.ModuleType("__main__")
    monkeypatch.setitem(sys.modules, "__main__", fake)
    process_backend._spawn_safety.clear()
    assert process_backend._spawn_entry_safe() == (True, "")


def _make_shifty(n):
    s = Shifty.__new__(Shifty)
    s.v = n
    return s


class Shifty:
    def __init__(self):
        self.v = 0

    def __reduce__(self):
        return (_make_shifty, (self.v + 1000,))


def test_unstable_pickle_falls_back_sequential():
    src = block(["ys[i] = xs[i].v"], "for i in range(len(xs)):")
    analysis, spec = build(src)
    env = {"xs": [Shifty() for _ in range(600)], "ys": [-1] * 600}
    execute(spec, range(600), env, force_backend="process")
    assert env["ys"] == [0] * 600
    assert any(
        r.error == "PreflightCheckError" and "round-trip" in r.message
        for r in get_fallback_report()
    )


def test_trust_pickle_clause_restores_parallel():
    src = block(
        ["ys[i] = xs[i].v"], "for i in range(len(xs)):", clauses="calibrate=false, trust=pickle"
    )
    analysis, spec = build(src)
    env = {"xs": [Shifty() for _ in range(600)], "ys": [-1] * 600}
    execute(spec, range(600), env, force_backend="process")
    assert dispatch.get_block_stats()[spec.key]["backend"] == "process"
    assert env["ys"] == [1000] * 600


def test_stable_payloads_unaffected_by_fixed_point_check():
    src = block(["ys[i] = xs[i] * 3 + 1"], "for i in range(len(xs)):")
    env = {"xs": list(range(2000)), "ys": [0] * 2000}
    p, _, spec = run(src, env)
    assert p["ys"] == golden(src, env)["ys"]
    assert dispatch.get_block_stats()[spec.key]["backend"] == "process"


def test_unpicklable_exception_type_rehydrated():
    src = block(["ys[i] = poke(xs[i])"], "for i in range(len(xs)):")
    env = {"xs": list(range(400)), "ys": [0] * 400, "poke": module_poke}
    analysis, spec = build(src)
    with pytest.raises(UnpicklableBoom) as info:
        execute(spec, range(400), copy.deepcopy(env), force_backend="process")
    assert "boom-333" in str(info.value)


def test_stale_worker_missing_module_falls_back_and_recycles(tmp_path):
    warm = block(["ys[i] = xs[i] + 1"], "for i in range(len(xs)):")
    run(warm, {"xs": list(range(200)), "ys": [0] * 200})

    mod = tmp_path / "stale_helper_mod.py"
    mod.write_text("def double(v):\n    return v * 2\n", encoding="utf-8")
    sys.path.insert(0, str(tmp_path))
    try:
        import stale_helper_mod

        src = block(["ys[i] = double(xs[i])"], "for i in range(len(xs)):")
        analysis, spec = build(src)
        env = {"xs": list(range(300)), "ys": [0] * 300, "double": stale_helper_mod.double}
        execute(spec, range(300), env, force_backend="process")
        assert env["ys"] == [v * 2 for v in range(300)]
        assert any(
            r.error == "MidRunSerializationError" and "reconstructed in the worker" in r.message
            for r in get_fallback_report()
        )
        assert dispatch.get_block_stats()[spec.key]["sequential_runs"] == 1
    finally:
        sys.path.remove(str(tmp_path))
        sys.modules.pop("stale_helper_mod", None)


def test_per_task_deadline_shipped_only_when_requested():
    plain = block(["ys[i] = xs[i] + 1"], "for i in range(len(xs)):")
    _, plain_spec = build(plain)
    assert plain_spec.artifact.per_task_deadline is False

    timed_src = block(
        ["ys[i] = xs[i] + 1"],
        "for i in range(len(xs)):",
        clauses="calibrate=false, timeout=5.0(per_task=true)",
    )
    _, timed_spec = build(timed_src)
    assert timed_spec.artifact.per_task_deadline is True
    env = {"xs": list(range(600)), "ys": [0] * 600}
    p, _, _ = run(timed_src, env)
    assert p["ys"] == golden(timed_src, env)["ys"]
