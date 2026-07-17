from __future__ import annotations

import ast
import copy
import textwrap
from typing import Dict, List, Optional, Set, Tuple

from lucen.analysis.analyzer import DependencyShape
from lucen.analysis.rewriter import AuditTier, BlockAnalysis, Classification, _chain
from lucen.analysis.selector import BlockDecision, Eligibility
from lucen.codegen.artifact import ChunkArtifact, ReductionPlan, SlabPlan
from lucen.support.errors import IllegalSyntaxInBlockError, raise_or_fallback

_SPECIAL_PARAMS = ("_plx_indices", "_plx_seq")


class _Unsupported(Exception):
    pass


def generate(
    analysis: BlockAnalysis, decision: BlockDecision, filename: str
) -> Optional[ChunkArtifact]:
    if decision.eligibility is Eligibility.SEQUENTIAL:
        return None
    if analysis.has_return:
        raise AssertionError("selector must route return blocks to SEQUENTIAL")
    if analysis.has_break and decision.eligibility is not Eligibility.EARLY_EXIT:
        raise AssertionError("selector must route break blocks to SEQUENTIAL or EARLY_EXIT")
    try:
        return _generate(analysis, decision)
    except _Unsupported as exc:
        raise_or_fallback(
            IllegalSyntaxInBlockError(
                f"v1 codegen does not support this form: {exc}",
                file=filename,
                line=analysis.block.start.lineno,
            )
        )
        return None


def _generate(analysis: BlockAnalysis, decision: BlockDecision) -> ChunkArtifact:
    for_node = analysis.for_node
    line = analysis.block.start.lineno
    domain, index_var, item_target = _domain_plan(for_node)

    slab_plans: Dict[str, SlabPlan] = {}
    transactional = True
    for path, info in sorted(analysis.targets.items()):
        cls = info.classification
        if getattr(info, "in_place", False) and cls in (
            Classification.SHARED_INDEXED_SAFE,
            Classification.READ_AFTER_WRITE,
        ):
            transactional = False
            continue
        if cls is Classification.SHARED_INDEXED_SAFE:
            root = path.split(".", 1)[0]
            if "." in path and analysis.domain and root in analysis.domain.values:
                transactional = False
                continue
            if info.audit_tier is AuditTier.BY_PROOF:
                slab_plans[path] = SlabPlan(_slab_name(path), path, "list")
            else:
                slab_plans[path] = SlabPlan(_slab_name(path), path, "dict")
        elif cls is Classification.READ_AFTER_WRITE:
            shape = decision.shapes.get(path)
            recognized = shape is not None and shape.shape in (
                DependencyShape.SELF_CONTAINED,
                DependencyShape.RECOGNIZED_DAG,
            )
            if recognized:
                cell = (
                    _cell_name(path)
                    if any(isinstance(r, ast.Name) and r.id == index_var for r in info.read_indexes)
                    else None
                )
                slab_plans[path] = SlabPlan(_slab_name(path), path, "list", cell=cell)
            else:
                slab_plans[path] = SlabPlan(_slab_name(path), path, "dict")
        elif cls is Classification.SHARED_INDEXED_UNRESOLVED:
            slab_plans[path] = SlabPlan(_slab_name(path), path, "dict")

    early_exit = decision.eligibility is Eligibility.EARLY_EXIT
    gindex = index_var if domain != "sequence" else "_plx_pos"
    tx = _Tx(
        slab_plans,
        dict(decision.reduction_ops),
        index_var,
        early_exit_gindex=gindex if early_exit else None,
    )
    body = [tx.visit(copy.deepcopy(stmt)) for stmt in for_node.body]
    body = _flatten(body)

    prologue: List[ast.stmt] = []
    for plan in slab_plans.values():
        if plan.cell is not None:
            prologue.append(_stmt(f"{plan.cell} = {plan.container}[{index_var}]"))

    clauses = analysis.block.start.clauses
    collect = "on_error" in clauses
    per_task_deadline = _call_kwarg_true(clauses.get("timeout"), "per_task")
    progress_per_task = _call_kwarg_true(clauses.get("progress"), "per_task")
    inner = prologue + body
    extra_params: List[str] = []
    if early_exit:
        extra_params.append("_plx_exit")
    if collect:
        guard = ast.parse("try:\n    pass\nexcept Exception as _plx_e:\n    pass").body[0]
        guard.body = inner
        guard.handlers[0].body = [_stmt(f"_plx_errors.append(({gindex}, _plx_e))")]
        inner = [guard]
        extra_params.append("_plx_errors")
    if progress_per_task:
        inner = [_stmt(f"_plx_progress({gindex})")] + inner
        extra_params.append("_plx_progress")
    if per_task_deadline:
        inner = [_stmt("if _plx_clock() > _plx_deadline:\n    raise _plx_timeout_error")] + inner
        extra_params.extend(["_plx_clock", "_plx_deadline", "_plx_timeout_error"])

    chunk_source, chunk_params = _assemble_chunk(
        line,
        domain,
        index_var,
        item_target,
        inner,
        [p.param for p in slab_plans.values()],
        tx.site_params,
        extra_params,
    )
    seq_source, seq_params, loop_targets, bare_scalars = _assemble_seq(
        line, for_node, sorted(tx.sites)
    )

    reductions = [
        ReductionPlan(scalar, tx.reduction_ops[scalar], tuple(tx.sites[scalar]))
        for scalar in sorted(tx.sites)
    ]
    no_raw = not any(
        info.classification is Classification.READ_AFTER_WRITE for info in analysis.targets.values()
    )
    instrumented = any(k in clauses for k in ("on_error", "timeout", "progress"))
    buffer_fast_path = (
        decision.eligibility is Eligibility.THREAD_CAPABLE
        and transactional
        and not early_exit
        and not reductions
        and no_raw
        and not instrumented
        and bool(slab_plans)
        and all(p.kind == "list" and p.cell is None for p in slab_plans.values())
    )
    return ChunkArtifact(
        block_line=line,
        name=f"_plx_chunk_L{line}",
        source=chunk_source,
        params=chunk_params,
        seq_name=f"_plx_seq_L{line}",
        seq_source=seq_source,
        seq_params=seq_params,
        domain=domain,
        loop_targets=loop_targets,
        target_source=ast.unparse(for_node.target),
        slabs=list(slab_plans.values()),
        reductions=reductions,
        transactional=transactional,
        collect_errors=collect,
        per_task_deadline=per_task_deadline,
        progress_per_task=progress_per_task,
        early_exit=early_exit,
        buffer_fast_path=buffer_fast_path,
        inplace_mutation=analysis.has_inplace_mutation,
        structured_payload=analysis.reads_aggregate,
        sliceable=(
            _sliceable_params(for_node, index_var, set(chunk_params)) if domain == "range" else ()
        ),
        # straight-line body writes every index: a typed slab cannot gap
        dense=buffer_fast_path and not analysis.has_control_flow,
    )


def _domain_plan(for_node: ast.For) -> Tuple[str, Optional[str], Optional[ast.expr]]:
    it = for_node.iter
    if isinstance(it, ast.Call) and isinstance(it.func, ast.Name):
        if it.func.id == "range" and isinstance(for_node.target, ast.Name):
            return "range", for_node.target.id, None
        if (
            it.func.id == "enumerate"
            and len(it.args) == 1
            and not it.keywords
            and isinstance(for_node.target, ast.Tuple)
            and len(for_node.target.elts) == 2
            and isinstance(for_node.target.elts[0], ast.Name)
        ):
            return ("enumerate", for_node.target.elts[0].id, for_node.target.elts[1])
    return "sequence", None, for_node.target


def _slab_name(path: str) -> str:
    return "_plx_slab_" + path.replace(".", "_")


def _cell_name(path: str) -> str:
    return "_plx_cell_" + path.replace(".", "_")


def _stmt(source: str) -> ast.stmt:
    return ast.parse(source).body[0]


def _flatten(items: List) -> List[ast.stmt]:
    out: List[ast.stmt] = []
    for item in items:
        out.extend(item) if isinstance(item, list) else out.append(item)
    return out


class _Tx(ast.NodeTransformer):
    def __init__(
        self,
        slab_plans: Dict[str, SlabPlan],
        reduction_ops: Dict[str, str],
        index_var: Optional[str],
        early_exit_gindex: Optional[str] = None,
    ):
        self.plans = slab_plans
        self.reduction_ops = reduction_ops
        self.index_var = index_var
        self.early_exit_gindex = early_exit_gindex
        self.sites: Dict[str, List[str]] = {}
        self.site_params: List[str] = []
        self._loop_depth = 0
        self._temp = 0

    def visit_Break(self, node: ast.Break):
        if self.early_exit_gindex is None:
            return node
        return [_stmt(f"_plx_exit[0] = {self.early_exit_gindex}"), node]

    def _new_site(self, scalar: str, node: ast.stmt) -> str:
        if self._loop_depth:
            raise _Unsupported(
                "a reduction update inside a nested loop "
                "(contribution count per outer iteration is unbounded)"
            )
        param = f"_plx_red_{scalar.replace('.', '_')}_{len(self.sites.get(scalar, []))}"
        self.sites.setdefault(scalar, []).append(param)
        self.site_params.append(param)
        return param

    def visit_While(self, node: ast.While) -> ast.While:
        self._loop_depth += 1
        self.generic_visit(node)
        self._loop_depth -= 1
        return node

    def visit_For(self, node: ast.For) -> ast.For:
        self._loop_depth += 1
        self.generic_visit(node)
        self._loop_depth -= 1
        return node

    def visit_Delete(self, node: ast.Delete):
        for target in node.targets:
            chain = _chain(target) if isinstance(target, (ast.Attribute, ast.Subscript)) else None
            if chain is not None and chain[1] in self.plans:
                raise _Unsupported(f"del on shared container '{chain[1]}'")
        self.generic_visit(node)
        return node

    def visit_AugAssign(self, node: ast.AugAssign):
        path = _store_path(node.target)
        if path is not None and path in self.reduction_ops:
            site = self._new_site(path, node)
            value = self.visit(node.value)
            return _stmt(f"{site}[_plx_j] = {ast.unparse(value)}")
        if isinstance(node.target, ast.Subscript):
            return self._aug_subscript(node)
        self.generic_visit(node)
        return node

    def _aug_subscript(self, node: ast.AugAssign):
        chain = _chain(node.target)
        plan = self.plans.get(chain[1]) if chain else None
        node.value = self.visit(node.value)
        if plan is None:
            node.target = self._visit_slices(node.target)
            return node
        index_src = ast.unparse(self.visit(chain[2]))
        value_src = ast.unparse(node.value)
        op_src = _AUG_SRC.get(type(node.op).__name__)
        if op_src is None:
            raise _Unsupported(f"augmented operator on '{plan.container}'")
        if plan.kind == "list":
            if plan.cell is None:
                raise AssertionError("aug on a list-slab container implies a cell")
            return [
                _stmt(f"{plan.cell} = {plan.cell} {op_src} ({value_src})"),
                _stmt(f"{plan.param}[_plx_j] = {plan.cell}"),
            ]
        key = self._fresh("_plx_k")
        return [
            _stmt(f"{key} = {index_src}"),
            _stmt(
                f"{plan.param}[{key}] = ({plan.param}[{key}] if {key} in "
                f"{plan.param} else {plan.container}[{key}]) {op_src} ({value_src})"
            ),
        ]

    def visit_Assign(self, node: ast.Assign):
        single = node.targets[0] if len(node.targets) == 1 else None
        path = _store_path(single) if single is not None else None
        if path is not None and path in self.reduction_ops:
            contribution = _contribution(path, node.value)
            if contribution is None or _references(contribution, path):
                raise _Unsupported(f"reduction value for '{path}' must not re-read the accumulator")
            site = self._new_site(path, node)
            value = self.visit(contribution)
            return _stmt(f"{site}[_plx_j] = {ast.unparse(value)}")
        node.value = self.visit(node.value)
        posts: List[ast.stmt] = []
        node.targets = [self._tx_target(t, posts) for t in node.targets]
        return [node] + posts if posts else node

    def _tx_target(self, target: ast.expr, posts: List[ast.stmt]) -> ast.expr:
        if isinstance(target, ast.Name):
            return target
        if isinstance(target, (ast.Tuple, ast.List)):
            target.elts = [self._tx_target(e, posts) for e in target.elts]
            return target
        if isinstance(target, ast.Starred):
            target.value = self._tx_target(target.value, posts)
            return target
        if isinstance(target, ast.Subscript):
            chain = _chain(target)
            plan = self.plans.get(chain[1]) if chain else None
            target = self._visit_slices(target)
            if plan is None:
                return target
            if plan.kind == "list":
                if plan.cell is not None:
                    posts.append(_stmt(f"{plan.param}[_plx_j] = {plan.cell}"))
                    return ast.Name(id=plan.cell, ctx=ast.Store())
                return _stmt(f"{plan.param}[_plx_j] = 0").targets[0]
            return ast.parse(f"{plan.param}[{ast.unparse(target.slice)}]").body[0].value
        if isinstance(target, ast.Attribute):
            target.value = self.visit(target.value)
            return target
        raise _Unsupported("assignment target form")

    def _visit_slices(self, node: ast.Subscript) -> ast.Subscript:
        node.slice = self.visit(node.slice)
        if isinstance(node.value, ast.Subscript):
            node.value = self._visit_slices(node.value)
        return node

    def visit_Subscript(self, node: ast.Subscript):
        self.generic_visit(node)
        if not isinstance(node.ctx, ast.Load):
            return node
        chain = _chain(node)
        if chain is None:
            return node
        plan = self.plans.get(chain[1])
        if (
            plan is not None
            and plan.cell is not None
            and isinstance(node.slice, ast.Name)
            and node.slice.id == self.index_var
        ):
            return ast.Name(id=plan.cell, ctx=ast.Load())
        return node

    def _fresh(self, base: str) -> str:
        self._temp += 1
        return f"{base}{self._temp}"


_AUG_SRC = {
    "Add": "+",
    "Sub": "-",
    "Mult": "*",
    "Div": "/",
    "FloorDiv": "//",
    "Mod": "%",
    "Pow": "**",
    "BitAnd": "&",
    "BitOr": "|",
    "BitXor": "^",
    "LShift": "<<",
    "RShift": ">>",
}


def _store_path(target: Optional[ast.expr]) -> Optional[str]:
    if isinstance(target, ast.Name):
        return target.id
    if isinstance(target, ast.Attribute):
        chain = _chain(target)
        if chain is not None and chain[2] is None:
            return chain[1]
    return None


def _contribution(path: str, value: ast.expr) -> Optional[ast.expr]:
    if isinstance(value, ast.BinOp):
        if _matches_path(value.left, path):
            return value.right
        if _matches_path(value.right, path):
            return value.left
    if (
        isinstance(value, ast.Call)
        and isinstance(value.func, ast.Name)
        and value.func.id in ("min", "max")
        and len(value.args) == 2
    ):
        if _matches_path(value.args[0], path):
            return value.args[1]
        if _matches_path(value.args[1], path):
            return value.args[0]
    return None


def _matches_path(node: ast.expr, path: str) -> bool:
    if isinstance(node, ast.Name):
        return node.id == path
    if isinstance(node, ast.Attribute):
        chain = _chain(node)
        return chain is not None and chain[2] is None and chain[1] == path
    return False


def _references(node: ast.expr, path: str) -> bool:
    return any(
        _matches_path(n, path) for n in ast.walk(node) if isinstance(n, (ast.Name, ast.Attribute))
    )


def _call_kwarg_true(cv, key: str) -> bool:
    return (
        cv is not None
        and cv.kind == "call"
        and key in cv.value.kwargs
        and cv.value.kwargs[key].value is True
    )


# params read only as P[i] are safe to ship as per-chunk slices; any other
# use (bare, P[i-1], P[j], writes) disqualifies
def _sliceable_params(
    for_node: ast.For, index_var: Optional[str], params: Set[str]
) -> Tuple[str, ...]:
    if index_var is None:
        return ()
    loads: Dict[str, int] = {}
    indexed: Dict[str, int] = {}
    for stmt in for_node.body:
        for node in ast.walk(stmt):
            if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load):
                loads[node.id] = loads.get(node.id, 0) + 1
            if (
                isinstance(node, ast.Subscript)
                and isinstance(node.value, ast.Name)
                and isinstance(node.slice, ast.Name)
                and node.slice.id == index_var
            ):
                indexed[node.value.id] = indexed.get(node.value.id, 0) + 1
    return tuple(
        sorted(p for p in params if indexed.get(p, 0) > 0 and loads.get(p, 0) == indexed[p])
    )


def _assemble_chunk(
    line: int,
    domain: str,
    index_var: Optional[str],
    item_target: Optional[ast.expr],
    body: List[ast.stmt],
    slab_params: List[str],
    site_params: List[str],
    extra_params: List[str],
) -> Tuple[str, List[str]]:
    if domain == "range":
        head = [f"    for {index_var} in _plx_indices:", "        _plx_j += 1"]
    elif domain == "enumerate":
        head = [
            f"    for {index_var} in _plx_indices:",
            "        _plx_j += 1",
            f"        {ast.unparse(item_target)} = _plx_seq[{index_var}]",
        ]
    else:
        head = [
            "    for _plx_pos in _plx_indices:",
            "        _plx_j += 1",
            f"        {ast.unparse(item_target)} = _plx_seq[_plx_pos]",
        ]
    body_src = textwrap.indent("\n".join(ast.unparse(stmt) for stmt in body), " " * 8)
    name = f"_plx_chunk_L{line}"
    skeleton = "\n".join([f"def {name}():", "    _plx_j = -1", *head, body_src])
    frees = _free_names(ast.parse(skeleton).body[0])
    special = (
        set(slab_params) | set(site_params) | set(extra_params) | {"_plx_e"} | set(_SPECIAL_PARAMS)
    )
    user_params = sorted(frees - special)
    params = ["_plx_indices"]
    if domain != "range":
        params.append("_plx_seq")
    params += user_params + list(slab_params) + list(site_params) + list(extra_params)
    source = skeleton.replace(f"def {name}():", f"def {name}({', '.join(params)}):", 1)
    return source, params


def _assemble_seq(
    line: int, for_node: ast.For, scalars: List[str]
) -> Tuple[str, List[str], List[str], List[str]]:
    from lucen.analysis.rewriter import _target_names

    loop_targets = _target_names(for_node.target)
    bare_scalars = [s for s in scalars if "." not in s]
    name = f"_plx_seq_L{line}"
    body_src = textwrap.indent(
        "\n".join(ast.unparse(copy.deepcopy(s)) for s in for_node.body), " " * 8
    )
    init = [f"    {t} = _plx_skip" for t in loop_targets]
    returns = ", ".join(loop_targets + bare_scalars) or "None"
    skeleton = "\n".join(
        [
            f"def {name}():",
            *init,
            f"    for {ast.unparse(for_node.target)} in _plx_iter:",
            body_src,
            f"    return ({returns},)" if loop_targets or bare_scalars else "    return None",
        ]
    )
    frees = _free_names(ast.parse(skeleton).body[0]) - {"_plx_iter", "_plx_skip"}
    params = ["_plx_iter", "_plx_skip"] + sorted(frees)
    source = skeleton.replace(f"def {name}():", f"def {name}({', '.join(params)}):", 1)
    return source, params, loop_targets, bare_scalars


def _free_names(fn: ast.FunctionDef) -> Set[str]:
    # s = s + x reads the accumulator before writing; the twin takes it as a
    # parameter, both for += and for the hand-written plain-assign form
    reads_self: Set[int] = set()
    for node in ast.walk(fn):
        if isinstance(node, ast.AugAssign) and isinstance(node.target, ast.Name):
            reads_self.add(id(node.target))
        elif isinstance(node, ast.Assign):
            loaded = {
                n.id
                for n in ast.walk(node.value)
                if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load)
            }
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id in loaded:
                    reads_self.add(id(target))
    bound: Set[str] = set()
    loads: Set[str] = set()
    self_targets: Set[str] = set()
    for node in ast.walk(fn):
        if isinstance(node, ast.Name):
            if isinstance(node.ctx, ast.Load):
                loads.add(node.id)
            elif id(node) in reads_self:
                self_targets.add(node.id)
            else:
                bound.add(node.id)
        elif isinstance(node, ast.arg):
            bound.add(node.arg)
        elif isinstance(node, ast.ExceptHandler) and node.name:
            bound.add(node.name)
    return (loads | (self_targets - bound)) - bound
