"""Both distributions must declare the same runtime dependencies.

Lucen ships two [project] tables - the root maturin build for the native abi3
wheel and packaging/pure/ for the py3-none-any fallback - so a dependency added
to one is silently absent from the other. `lucen.support.config` is imported
lazily, so such a wheel still imports cleanly and only fails once something
first touches config.
"""

from __future__ import annotations

import ast
import re
import sys
from pathlib import Path
from typing import Dict, Iterable, Set

import pytest

try:
    import tomllib
except ModuleNotFoundError:
    import tomli as tomllib  # type: ignore[no-redef]

_REPO_ROOT = Path(__file__).resolve().parents[2]
_NATIVE = _REPO_ROOT / "pyproject.toml"
_PURE = _REPO_ROOT / "packaging" / "pure" / "pyproject.toml"

# Hand-rolled rather than packaging.Requirement, which would make a check on
# dependency declarations itself depend on an undeclared dependency.
_REQUIREMENT_NAME = re.compile(r"^\s*([A-Za-z0-9][A-Za-z0-9._-]*)")


def _project(path: Path) -> Dict[str, object]:
    with open(path, "rb") as f:
        return dict(tomllib.load(f)["project"])


def _requirement_names(requirements: Iterable[str]) -> Set[str]:
    names = set()
    for req in requirements:
        match = _REQUIREMENT_NAME.match(req)
        assert match is not None, f"unparseable requirement {req!r}"
        names.add(match.group(1).lower().replace("_", "-"))
    return names


# An import guarded by try/except ImportError is satisfied by whichever half
# the interpreter provides, so neither half is a hard requirement - the backport
# has its own check above. Judging them here would mean trusting
# sys.stdlib_module_names, which GraalPy ships incomplete: it omits tomllib on a
# 3.11 interpreter that imports tomllib fine.
def _guarded_imports(tree: ast.Module) -> Set[int]:
    guarded: Set[int] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Try):
            continue
        caught: Set[str] = set()
        for handler in node.handlers:
            raised = handler.type
            parts = raised.elts if isinstance(raised, ast.Tuple) else [raised]
            caught.update(p.id for p in parts if isinstance(p, ast.Name))
        if not caught & {"ImportError", "ModuleNotFoundError"}:
            continue
        for stmt in [*node.body, *(s for h in node.handlers for s in h.body)]:
            for inner in ast.walk(stmt):
                if isinstance(inner, (ast.Import, ast.ImportFrom)):
                    guarded.add(id(inner))
    return guarded


def _third_party_imports() -> Dict[str, Set[str]]:
    imports: Dict[str, Set[str]] = {}
    for source in sorted((_REPO_ROOT / "lucen").rglob("*.py")):
        tree = ast.parse(source.read_text(encoding="utf-8"), filename=str(source))
        guarded = _guarded_imports(tree)
        for node in ast.walk(tree):
            if id(node) in guarded:
                continue
            if isinstance(node, ast.Import):
                modules = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                modules = [node.module]
            else:
                continue
            for module in modules:
                top = module.split(".")[0]
                if top != "lucen" and top not in sys.stdlib_module_names:
                    imports.setdefault(top, set()).add(source.relative_to(_REPO_ROOT).as_posix())
    return imports


def test_both_distributions_declare_the_same_runtime_dependencies():
    native = _project(_NATIVE)
    pure = _project(_PURE)
    assert native.get("dependencies", []) == pure.get("dependencies", []), (
        f"{_NATIVE} declares {native.get('dependencies', [])} and "
        f"{_PURE} declares {pure.get('dependencies', [])}"
    )


def test_both_distributions_support_the_same_python_range():
    # A `python_version < '3.11'` marker is only correct while both wheels
    # claim the same floor.
    assert _project(_NATIVE)["requires-python"] == _project(_PURE)["requires-python"]


def test_tomli_backport_is_declared_for_pythons_without_tomllib():
    # tomllib is stdlib only from 3.11; the marker has to match the fallback
    # boundary in lucen/support/config.py.
    for path in (_NATIVE, _PURE):
        declared = _project(path).get("dependencies", [])
        tomli = [req for req in declared if _requirement_names([req]) == {"tomli"}]
        assert tomli, f"{path} declares no tomli backport"
        assert "python_version < '3.11'" in tomli[0], f"{path} declares {tomli[0]!r}"


# Gated on the API existing, not on a version: what the set *contains* varies by
# interpreter and cannot be inferred from sys.version_info.
@pytest.mark.skipif(
    not hasattr(sys, "stdlib_module_names"), reason="sys.stdlib_module_names is 3.10+"
)
def test_every_unguarded_third_party_import_is_a_declared_dependency():
    declared = _requirement_names(_project(_NATIVE).get("dependencies", []))
    for module, sources in sorted(_third_party_imports().items()):
        assert module.lower().replace("_", "-") in declared, (
            f"{module} is imported by {sorted(sources)} but neither pyproject.toml declares it"
        )
