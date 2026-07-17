from __future__ import annotations

import ast
import importlib.abc
import importlib.machinery
import importlib.util
import os
import sys
import tokenize
from fnmatch import fnmatch
from io import BytesIO
from typing import List, Optional, Tuple

from lucen.analysis import trust
from lucen.analysis.rewriter import analyze_source
from lucen.analysis.scanner import PREFILTER_TOKEN, scan_source
from lucen.analysis.selector import select
from lucen.codegen import generate
from lucen.execution import dispatch
from lucen.support import cache, config

_finder: Optional["_Finder"] = None


def install(root: str) -> None:
    global _finder
    if _finder is not None:
        return
    _finder = _Finder(os.path.abspath(root))
    sys.meta_path.insert(0, _finder)


def uninstall() -> None:
    global _finder
    if _finder is not None:
        try:
            sys.meta_path.remove(_finder)
        except ValueError:
            pass
        _finder = None


class _Finder(importlib.abc.MetaPathFinder):
    def __init__(self, root: str):
        self.root = root

    def find_spec(self, fullname, path=None, target=None):
        spec = importlib.machinery.PathFinder.find_spec(fullname, path)
        if spec is None or spec.origin is None or not spec.origin.endswith(".py"):
            return None
        origin = os.path.abspath(spec.origin)
        if not origin.startswith(self.root):
            return None
        if not _in_scope(os.path.relpath(origin, self.root)):
            return None
        spec.loader = _Loader(fullname, origin, self.root)
        return spec


def _in_scope(relpath: str) -> bool:
    cfg = config.active()
    rel = relpath.replace(os.sep, "/")
    if cfg.scope_exclude and any(
        fnmatch(rel, pat) or rel.startswith(pat.rstrip("/*") + "/") for pat in cfg.scope_exclude
    ):
        return False
    if cfg.scope_include:
        return any(
            fnmatch(rel, pat) or rel.startswith(pat.rstrip("/*") + "/") for pat in cfg.scope_include
        )
    return True


class _Loader(importlib.abc.Loader):
    def __init__(self, fullname: str, path: str, root: str):
        self.fullname = fullname
        self.path = path
        self.root = root

    def create_module(self, spec):
        return None

    def get_filename(self, fullname=None) -> str:
        return self.path

    def exec_module(self, module) -> None:
        with open(self.path, "rb") as f:
            raw = f.read()
        if PREFILTER_TOKEN.encode() not in raw:
            code = compile(raw, self.path, "exec")
            exec(code, module.__dict__)
            return
        source = raw.decode(_encoding(raw))
        entry = cache.load(self.root, self.path, source)
        if entry is None:
            entry = rewrite_module(source, self.path)
            cache.store(self.root, self.path, source, entry)
        if entry.rewritten is None:
            exec(compile(source, self.path, "exec"), module.__dict__)
            return
        module.__dict__["_lucen_rt"] = dispatch
        for line, spec in entry.specs:
            module.__dict__[f"_PLX_SPEC_{line}"] = spec
        exec(compile(entry.rewritten, self.path, "exec"), module.__dict__)


def _encoding(raw: bytes) -> str:
    return tokenize.detect_encoding(BytesIO(raw).readline)[0]


def run_path(path: str, run_name: str = "__main__") -> dict:
    path = os.path.abspath(path)
    with open(path, "rb") as f:
        raw = f.read()
    namespace: dict = {"__name__": run_name, "__file__": path, "__builtins__": __builtins__}
    if PREFILTER_TOKEN.encode() not in raw:
        exec(compile(raw, path, "exec"), namespace)
        return namespace
    source = raw.decode(_encoding(raw))
    root = os.path.dirname(path)
    entry = cache.load(root, path, source)
    if entry is None:
        entry = rewrite_module(source, path)
        cache.store(root, path, source, entry)
    if entry.rewritten is None:
        exec(compile(source, path, "exec"), namespace)
        return namespace
    namespace["_lucen_rt"] = dispatch
    for line, spec in entry.specs:
        namespace[f"_PLX_SPEC_{line}"] = spec
    exec(compile(entry.rewritten, path, "exec"), namespace)
    return namespace


def rewrite_module(source: str, filename: str) -> cache.Entry:
    scan = scan_source(source, filename)
    if not scan.blocks and not scan.trusted:
        return cache.Entry(None, [])
    experimental = config.active().experimental
    analyses = analyze_source(source, scan, filename, experimental=experimental)
    tree = ast.parse(source, filename)
    dropped = trust.blocks_to_drop(scan, analyses, tree, filename)

    replacements: List[Tuple[int, int, List[str]]] = []
    specs: List[Tuple[int, object]] = []
    lines = source.splitlines()
    for analysis in analyses:
        if not analysis.ok or analysis.block.start.lineno in dropped:
            continue
        decision = select(analysis, experimental=experimental)
        artifact = generate(analysis, decision, filename)
        if artifact is None:
            continue
        spec = dispatch.make_spec(analysis, decision, artifact)
        specs.append((spec.line, spec))
        for_node = analysis.for_node
        indent = lines[for_node.lineno - 1][
            : len(lines[for_node.lineno - 1]) - len(lines[for_node.lineno - 1].lstrip())
        ]
        replacements.append(
            (for_node.lineno, for_node.end_lineno, _call_site(analysis, spec, indent))
        )

    if not replacements:
        return cache.Entry(None, [])
    for start, end, new_lines in sorted(replacements, reverse=True):
        lines[start - 1 : end] = new_lines
    return cache.Entry("\n".join(lines) + "\n", specs)


def _call_site(analysis, spec, indent: str) -> List[str]:
    artifact = spec.artifact
    for_node = analysis.for_node
    if artifact.domain == "enumerate":
        iter_src = ast.unparse(for_node.iter.args[0])
    else:
        iter_src = ast.unparse(for_node.iter)
    env_items = ", ".join(f"{name!r}: {name}" for name in spec.arg_names)
    rebind = ", ".join(
        artifact.loop_targets + [r.scalar for r in artifact.reductions if "." not in r.scalar]
    )
    result = f"_plx_r_{spec.line}"
    return [
        f"{indent}{result} = _lucen_rt.execute(_PLX_SPEC_{spec.line}, "
        f"{iter_src}, {{{env_items}}}, globals())",
        f"{indent}if {result} is not None:",
        f"{indent}    ({rebind},) = {result}",
    ]
