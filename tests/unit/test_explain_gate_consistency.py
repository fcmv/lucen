import pytest

from lucen.analysis.rewriter import analyze_source
from lucen.analysis.scanner import scan_source
from lucen.analysis.selector import select
from lucen.cli.explain import _predicted_backend
from lucen.codegen import generate
from lucen.execution import dispatch
from lucen.execution.dispatch import execute, make_spec
from lucen.support import config


@pytest.fixture(autouse=True)
def _clean_state():
    config.set_active(config.Config())
    dispatch.reset_runtime_state()
    yield
    dispatch.reset_runtime_state()
    config.set_active(config.Config())


CASES = [
    (
        "map",
        ["ys[i] = xs[i] * xs[i] + 1"],
        "for i in range(len(xs)):",
        "process",
        lambda n: {"xs": list(range(n)), "ys": [0] * n},
    ),
    (
        "reduction",
        ["total += xs[i]"],
        "for i in range(len(xs)):",
        "process",
        lambda n: {"xs": list(range(n)), "total": 0},
    ),
    (
        "no-output side effect",
        ["matrix[i].append(xs[i])"],
        "for i in range(len(xs)):",
        "thread",
        lambda n: {"xs": list(range(n)), "matrix": [[k] for k in range(n)]},
    ),
]


def _build(src):
    scan = scan_source(src, "t.py")
    analysis = analyze_source(src, scan, "t.py")[0]
    decision = select(analysis, workers=8)
    artifact = generate(analysis, decision, "t.py")
    return analysis, decision, make_spec(analysis, decision, artifact)


@pytest.mark.skipif(dispatch.free_threaded(), reason="FT promotes process->thread at runtime")
@pytest.mark.parametrize("name,body,header,expected,factory", CASES, ids=[c[0] for c in CASES])
def test_explain_predicted_backend_matches_gate(name, body, header, expected, factory):
    lines = "\n".join("    " + b for b in body)
    src = f"# LUCEN START calibrate=false\n{header}\n{lines}\n# LUCEN END\n"
    analysis, decision, spec = _build(src)

    predicted = _predicted_backend(analysis, decision)
    assert predicted == expected

    n = 4000
    env = factory(n)
    execute(spec, range(n), env, force_backend=None)
    actual = list(dispatch.get_block_stats().values())[-1]["backend"]
    assert actual == predicted
