from __future__ import annotations

import json
import textwrap

import pytest

import lucen
from lucen.cli import main
from lucen.execution import dispatch
from lucen.support import config
from lucen.support.errors import ErrorsMode, clear_fallback_report, set_errors_mode

SCRIPT = """\
xs = list(range(3000))
ys = [0] * 3000
# LUCEN START calibrate=false, backend=thread
for i in range(len(xs)):
    ys[i] = xs[i] * 2
# LUCEN END
assert ys[10] == 20
assert i == 2999
print("script done")
"""


@pytest.fixture(autouse=True)
def _clean_state():
    set_errors_mode(ErrorsMode.REPORT)
    clear_fallback_report()
    dispatch.reset_runtime_state()
    config.set_active(config.Config())
    yield
    lucen.deactivate()
    set_errors_mode(ErrorsMode.REPORT)
    clear_fallback_report()
    dispatch.reset_runtime_state()
    config.set_active(config.Config())


def test_profile_export_json(tmp_path, capsys):
    script = tmp_path / "job.py"
    script.write_text(textwrap.dedent(SCRIPT), encoding="utf-8")
    out = tmp_path / "prof.json"
    assert main(["profile", str(script), "--export", str(out)]) == 0
    printed = capsys.readouterr().out
    assert "script done" in printed
    payload = json.loads(out.read_text(encoding="utf-8"))
    assert payload["wall_seconds"] > 0
    (block_stats,) = payload["blocks"].values()
    assert block_stats["parallel_runs"] == 1
    assert block_stats["backend"] == "thread"


def test_profile_text_per_block(tmp_path, capsys):
    script = tmp_path / "job.py"
    script.write_text(textwrap.dedent(SCRIPT), encoding="utf-8")
    assert main(["profile", str(script), "--per-block"]) == 0
    out = capsys.readouterr().out
    assert "lucen profile" in out
    assert "backend=thread" in out
    assert "Fallbacks: 0" in out


def test_profile_rewrites_loop_in_imported_module(tmp_path, capsys):
    # The marked loop lives in a module the entry script imports, not in the
    # entry script itself. The profiler must add the script's directory to
    # sys.path and root the hook there, or the import fails and the loop is
    # never rewritten.
    (tmp_path / "worker.py").write_text(
        textwrap.dedent(
            """\
            def run():
                xs = list(range(3000))
                ys = [0] * 3000
                # LUCEN START calibrate=false, backend=thread
                for i in range(len(xs)):
                    ys[i] = xs[i] * 2
                # LUCEN END
                return ys[-1]
            """
        ),
        encoding="utf-8",
    )
    entry = tmp_path / "entry.py"
    entry.write_text(
        textwrap.dedent(
            """\
            import lucen
            lucen.activate()
            import worker

            if __name__ == "__main__":
                print("worker result:", worker.run())
            """
        ),
        encoding="utf-8",
    )
    assert main(["profile", str(entry), "--per-block"]) == 0
    out = capsys.readouterr().out
    assert "worker result: 5998" in out
    assert "worker.py:" in out
    assert "backend=thread" in out
    assert "parallel=1" in out


def test_run_rewrites_and_executes_entry_script(tmp_path, capsys):
    script = tmp_path / "job.py"
    script.write_text(textwrap.dedent(SCRIPT), encoding="utf-8")
    assert main(["run", str(script)]) == 0
    assert "script done" in capsys.readouterr().out
    (block_stats,) = dispatch.get_block_stats().values()
    assert block_stats["parallel_runs"] == 1
    assert block_stats["backend"] == "thread"
