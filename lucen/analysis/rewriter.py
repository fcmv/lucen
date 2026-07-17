from __future__ import annotations

import ast
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Dict, FrozenSet, List, Optional, Set, Tuple

from lucen.analysis.scanner import MarkedBlock, ScanResult
from lucen.support.errors import (
    BranchMergeConflictError,
    FallbackRecord,
    IllegalSyntaxInBlockError,
    LucenError,
    raise_or_fallback,
)


class Classification(Enum):
    LOOP_LOCAL = auto()
    OUTER_READONLY = auto()
    SHARED_SCALAR = auto()
    SHARED_INDEXED_SAFE = auto()
    SHARED_INDEXED_UNRESOLVED = auto()
    READ_AFTER_WRITE = auto()


class AuditTier(Enum):
    BY_PROOF = auto()
    BY_ASSUMPTION = auto()
    ASSERTED = auto()


@dataclass(frozen=True)
class LoopDomain:
    proven: FrozenSet[str]
    values: FrozenSet[str]

    @property
    def names(self) -> FrozenSet[str]:
        return self.proven | self.values


@dataclass
class TargetInfo:
    classification: Classification
    audit_tier: Optional[AuditTier] = None
    reduce_op: Optional[str] = None
    write_indexes: List[ast.expr] = field(default_factory=list)
    read_indexes: List[ast.expr] = field(default_factory=list)
    in_place: bool = False
    nested_reduction: bool = False


@dataclass
class BlockAnalysis:
    block: MarkedBlock
    filename: str
    for_node: Optional[ast.For] = None
    domain: Optional[LoopDomain] = None
    targets: Dict[str, TargetInfo] = field(default_factory=dict)
    has_break: bool = False
    has_return: bool = False
    has_inplace_mutation: bool = False
    reads_aggregate: bool = False
    has_control_flow: bool = False
    called_paths: FrozenSet[str] = frozenset()
    trusted_names: FrozenSet[str] = frozenset()
    branch_sensitive: bool = False
    fallbacks: List[FallbackRecord] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.for_node is not None and not self.fallbacks


_READ = "read"
_WRITE = "write"
_AUG = "aug"
_SUBREAD = "subread"
_SUBWRITE = "subwrite"
_OPAQUE = "opaque"

_AUG_OPS = {ast.Add: "+", ast.Mult: "*", ast.BitAnd: "&", ast.BitOr: "|", ast.BitXor: "^"}

_MUTATING_METHODS = frozenset(
    {
        "append",
        "extend",
        "insert",
        "remove",
        "pop",
        "clear",
        "update",
        "setdefault",
        "popitem",
        "add",
        "discard",
        "sort",
        "reverse",
        "write",
        "writelines",
    }
)

_TRY_TYPES: Tuple[type, ...] = (ast.Try,) + ((ast.TryStar,) if hasattr(ast, "TryStar") else ())


@dataclass(frozen=True)
class _Event:
    kind: str
    path: str
    ctx: Tuple[str, ...]
    lineno: int
    index: Optional[ast.expr] = None
    op: Optional[str] = None
    selfref: bool = False
    nested: bool = False
    in_loop: bool = False


def analyze_source(
    source: str,
    scan: ScanResult,
    filename: str = "<string>",
    experimental: Optional[FrozenSet[str]] = None,
) -> List[BlockAnalysis]:
    branch_sensitive = bool(experimental and "branch_sensitive_deps" in experimental)
    try:
        tree = ast.parse(source, filename)
    except SyntaxError:
        return []
    parents: Dict[ast.AST, ast.AST] = {}
    for node in ast.walk(tree):
        for child in ast.iter_child_nodes(node):
            parents[child] = node
    statements = [n for n in ast.walk(tree) if isinstance(n, ast.stmt)]
    trusted = frozenset(getattr(scan, "trusted_names", ()) or ())
    analyses = [
        _analyze_block(b, statements, parents, filename, branch_sensitive) for b in scan.blocks
    ]
    for analysis in analyses:
        analysis.trusted_names = trusted
    return analyses


def _analyze_block(
    block: MarkedBlock,
    statements: List[ast.stmt],
    parents: Dict[ast.AST, ast.AST],
    filename: str,
    branch_sensitive: bool = False,
) -> BlockAnalysis:
    analysis = BlockAnalysis(block, filename)
    analysis.branch_sensitive = branch_sensitive
    try:
        _analyze_into(analysis, statements, parents)
    except LucenError as exc:
        analysis.fallbacks.append(raise_or_fallback(exc))
    except Exception as exc:
        wrapped = IllegalSyntaxInBlockError(
            f"analysis failed: {type(exc).__name__}: {exc}", file=filename, line=block.start.lineno
        )
        analysis.fallbacks.append(raise_or_fallback(wrapped))
    return analysis


def _analyze_into(
    analysis: BlockAnalysis, statements: List[ast.stmt], parents: Dict[ast.AST, ast.AST]
) -> None:
    block, filename = analysis.block, analysis.filename
    lo, hi = block.start.lineno, block.end.lineno
    inside = [s for s in statements if s.lineno > lo and (s.end_lineno or s.lineno) < hi]
    inside_ids = {id(s) for s in inside}
    top = [s for s in inside if not _has_ancestor_in(s, parents, inside_ids)]
    if len(top) != 1 or not isinstance(top[0], ast.For):
        raise IllegalSyntaxInBlockError(
            "a marked block must contain exactly one for loop", file=filename, line=lo
        )
    for_node = top[0]
    if for_node.orelse:
        raise IllegalSyntaxInBlockError(
            "for/else is not supported in a marked block", file=filename, line=for_node.lineno
        )

    domain = _loop_domain(for_node)
    collector = _Collector(filename)
    collector.load(for_node.iter, ())
    collector.body(for_node.body, ())

    targets = _classify(collector.events, domain, filename, analysis.branch_sensitive)
    _check_conditions(collector.conditions, targets, domain, filename)

    analysis.for_node = for_node
    analysis.domain = domain
    analysis.targets = targets
    analysis.has_break = collector.has_break
    analysis.has_return = collector.has_return
    analysis.has_inplace_mutation = collector.has_inplace_mutation
    analysis.reads_aggregate = collector.reads_aggregate
    analysis.has_control_flow = _has_control_flow(for_node)
    analysis.called_paths = frozenset(collector.called_paths)


_CONTROL_FLOW: Tuple[type, ...] = (
    ast.If,
    ast.While,
    ast.For,
    ast.Break,
    ast.Continue,
    ast.Return,
    ast.Raise,
    ast.Assert,
) + _TRY_TYPES


def _has_control_flow(for_node: ast.For) -> bool:
    return any(isinstance(node, _CONTROL_FLOW) for stmt in for_node.body for node in ast.walk(stmt))


def _has_ancestor_in(node: ast.AST, parents: Dict[ast.AST, ast.AST], candidates: Set[int]) -> bool:
    parent = parents.get(node)
    while parent is not None:
        if id(parent) in candidates:
            return True
        parent = parents.get(parent)
    return False


def _loop_domain(node: ast.For) -> LoopDomain:
    iterator = node.iter
    if isinstance(iterator, ast.Call) and isinstance(iterator.func, ast.Name):
        fn = iterator.func.id
        if fn == "range" and isinstance(node.target, ast.Name):
            return LoopDomain(frozenset({node.target.id}), frozenset())
        if (
            fn == "enumerate"
            and len(iterator.args) == 1
            and not iterator.keywords
            and isinstance(node.target, ast.Tuple)
            and len(node.target.elts) == 2
            and isinstance(node.target.elts[0], ast.Name)
        ):
            values = frozenset(_target_names(node.target.elts[1]))
            return LoopDomain(frozenset({node.target.elts[0].id}), values)
    return LoopDomain(frozenset(), frozenset(_target_names(node.target)))


def _target_names(node: ast.AST) -> List[str]:
    return [n.id for n in ast.walk(node) if isinstance(n, ast.Name)]


class _Collector:
    def __init__(self, filename: str):
        self.filename = filename
        self.events: List[_Event] = []
        self.conditions: List[Tuple[int, Set[str]]] = []
        self._suppressed: List[Set[str]] = []
        self._loop_depth = 0
        self.has_break = False
        self.has_return = False
        self.has_inplace_mutation = False
        self.reads_aggregate = False
        self.called_paths: Set[str] = set()

    def _illegal(self, message: str, node: ast.AST) -> None:
        raise IllegalSyntaxInBlockError(
            message, file=self.filename, line=getattr(node, "lineno", None)
        )

    def _suppressed_name(self, name: str) -> bool:
        return any(name in scope for scope in self._suppressed)

    def _emit(
        self,
        kind: str,
        path: str,
        ctx: Tuple[str, ...],
        node: ast.AST,
        index: Optional[ast.expr] = None,
        op: Optional[str] = None,
        selfref: bool = False,
        nested: bool = False,
    ) -> None:
        self.events.append(
            _Event(
                kind,
                path,
                ctx,
                getattr(node, "lineno", 0),
                index,
                op,
                selfref,
                nested,
                in_loop=self._loop_depth > 0,
            )
        )

    def body(self, stmts: List[ast.stmt], ctx: Tuple[str, ...]) -> None:
        for stmt in stmts:
            self.stmt(stmt, ctx)

    def stmt(self, node: ast.stmt, ctx: Tuple[str, ...]) -> None:
        if isinstance(node, ast.Assign):
            self._assign(node, ctx)
        elif isinstance(node, ast.AnnAssign):
            if node.value is not None:
                self.load(node.value, ctx)
            self.store(node.target, ctx)
        elif isinstance(node, ast.AugAssign):
            self.load(node.value, ctx)
            self._aug_store(node.target, ctx, _AUG_OPS.get(type(node.op)))
        elif isinstance(node, ast.Expr):
            self.load(node.value, ctx)
        elif isinstance(node, ast.If):
            self._if_chain(node, ctx)
        elif isinstance(node, ast.While):
            self._condition(node.test, ctx)
            self._loop_depth += 1
            self.body(node.body, ctx + (f"while@{node.lineno}",))
            self.body(node.orelse, ctx + (f"while@{node.lineno}:else",))
            self._loop_depth -= 1
        elif isinstance(node, ast.For):
            self._nested_for(node, ctx)
        elif isinstance(node, _TRY_TYPES):
            self._try(node, ctx)
        elif isinstance(node, ast.Break):
            if self._loop_depth == 0:
                self.has_break = True
        elif isinstance(node, ast.Continue):
            pass
        elif isinstance(node, ast.Return):
            if node.value is not None:
                self.load(node.value, ctx)
            self.has_return = True
        elif isinstance(node, ast.Pass):
            pass
        elif isinstance(node, ast.Raise):
            for child in (node.exc, node.cause):
                if child is not None:
                    self.load(child, ctx)
        elif isinstance(node, ast.Assert):
            self.load(node.test, ctx)
            if node.msg is not None:
                self.load(node.msg, ctx)
        elif isinstance(node, ast.Delete):
            for target in node.targets:
                chain = _chain(target)
                if chain is None:
                    self._illegal("unsupported del target", node)
                _, base, _, slices = chain
                for s in slices:
                    self.load(s, ctx)
                self._emit(_OPAQUE, base, ctx, node)
        elif isinstance(node, (ast.Global, ast.Nonlocal)):
            self._illegal("global/nonlocal is not supported in a marked block", node)
        else:
            self._illegal(f"{type(node).__name__} is not supported in a marked block", node)

    def _assign(self, node: ast.Assign, ctx: Tuple[str, ...]) -> None:
        single = node.targets[0] if len(node.targets) == 1 else None
        if isinstance(single, ast.Name):
            name = single.id
            selfref = any(isinstance(n, ast.Name) and n.id == name for n in ast.walk(node.value))
            if selfref:
                self._suppressed.append({name})
                self.load(node.value, ctx)
                self._suppressed.pop()
            else:
                self.load(node.value, ctx)
            op = _selfref_op(name, node.value) if selfref else None
            self._emit(_WRITE, name, ctx, node, op=op, selfref=selfref)
            return
        self.load(node.value, ctx)
        for target in node.targets:
            self.store(target, ctx)

    def _if_chain(self, node: ast.If, ctx: Tuple[str, ...]) -> None:
        base = f"if@{node.lineno}"
        arm = 0
        cur = node
        while True:
            self._condition(cur.test, ctx)
            self.body(cur.body, ctx + (f"{base}:{arm}",))
            arm += 1
            orelse = cur.orelse
            if (
                len(orelse) == 1
                and isinstance(orelse[0], ast.If)
                and orelse[0].col_offset == cur.col_offset
            ):
                cur = orelse[0]
                continue
            if orelse:
                self.body(orelse, ctx + (f"{base}:{arm}",))
            return

    def _nested_for(self, node: ast.For, ctx: Tuple[str, ...]) -> None:
        if isinstance(node.iter, ast.Subscript):
            self.reads_aggregate = True
        self.load(node.iter, ctx)
        bound = set(_target_names(node.target))
        self._suppressed.append(bound)
        self._loop_depth += 1
        self.body(node.body, ctx + (f"for@{node.lineno}",))
        self.body(node.orelse, ctx + (f"for@{node.lineno}:else",))
        self._loop_depth -= 1
        self._suppressed.pop()

    def _try(self, node: ast.stmt, ctx: Tuple[str, ...]) -> None:
        base = f"try@{node.lineno}"
        arm = 0
        self.body(node.body, ctx + (f"{base}:{arm}",))
        for handler in node.handlers:
            arm += 1
            if handler.type is not None:
                self.load(handler.type, ctx)
            self._suppressed.append({handler.name} if handler.name else set())
            self.body(handler.body, ctx + (f"{base}:{arm}",))
            self._suppressed.pop()
        for stmts in (node.orelse, node.finalbody):
            if stmts:
                arm += 1
                self.body(stmts, ctx + (f"{base}:{arm}",))

    def _condition(self, test: ast.expr, ctx: Tuple[str, ...]) -> None:
        mark = len(self.events)
        self.load(test, ctx)
        names = {e.path for e in self.events[mark:] if e.kind in (_READ, _SUBREAD)}
        self.conditions.append((getattr(test, "lineno", 0), names))

    def store(self, node: ast.expr, ctx: Tuple[str, ...]) -> None:
        if isinstance(node, ast.Name):
            self._emit(_WRITE, node.id, ctx, node)
        elif isinstance(node, (ast.Tuple, ast.List)):
            for elt in node.elts:
                self.store(elt, ctx)
        elif isinstance(node, ast.Starred):
            self.store(node.value, ctx)
        elif isinstance(node, (ast.Attribute, ast.Subscript)):
            chain = _chain(node)
            if chain is None:
                self._illegal("unsupported assignment target", node)
            root, base, first_index, slices = chain
            self._emit(_READ, root, ctx, node)
            for s in slices:
                self.load(s, ctx)
            if first_index is None:
                self._emit(_WRITE, base, ctx, node)
            else:
                nested = len(slices) >= 2 or isinstance(node, ast.Attribute)
                if nested:
                    self.has_inplace_mutation = True
                self._emit(_SUBWRITE, base, ctx, node, index=first_index, nested=nested)
        else:
            self._illegal("unsupported assignment target", node)

    def _aug_store(self, node: ast.expr, ctx: Tuple[str, ...], op: Optional[str]) -> None:
        if isinstance(node, ast.Name):
            self._emit(_AUG, node.id, ctx, node, op=op)
            return
        chain = _chain(node)
        if chain is None:
            self._illegal("unsupported augmented-assignment target", node)
        root, base, first_index, slices = chain
        self._emit(_READ, root, ctx, node)
        for s in slices:
            self.load(s, ctx)
        if first_index is None:
            self._emit(_AUG, base, ctx, node, op=op)
        else:
            self._emit(_SUBREAD, base, ctx, node, index=first_index)
            self._emit(_SUBWRITE, base, ctx, node, index=first_index)

    def load(self, node: Optional[ast.expr], ctx: Tuple[str, ...]) -> None:
        if node is None:
            return
        if isinstance(node, ast.Name):
            if not self._suppressed_name(node.id):
                self._emit(_READ, node.id, ctx, node)
        elif isinstance(node, (ast.Attribute, ast.Subscript)):
            chain = _chain(node)
            if chain is None:
                self._generic_load(node, ctx)
                return
            root, base, first_index, slices = chain
            if not self._suppressed_name(root):
                self._emit(_READ, root, ctx, node)
                if first_index is None:
                    if "." in base:
                        self._emit(_READ, base, ctx, node)
                else:
                    self._emit(_SUBREAD, base, ctx, node, index=first_index)
                    if len(slices) >= 2:
                        self.reads_aggregate = True
            for s in slices:
                self.load(s, ctx)
        elif isinstance(node, ast.Call):
            self._call(node, ctx)
        elif isinstance(node, ast.NamedExpr):
            self.load(node.value, ctx)
            self.store(node.target, ctx)
        elif isinstance(node, ast.Lambda):
            self._illegal(
                "lambda inside a marked block is only supported as a direct call argument", node
            )
        elif isinstance(node, ast.GeneratorExp):
            self._illegal(
                "generator expression inside a marked block is only "
                "supported as a direct call argument",
                node,
            )
        elif isinstance(node, (ast.ListComp, ast.SetComp, ast.DictComp)):
            bound: Set[str] = set()
            for gen in node.generators:
                bound.update(_target_names(gen.target))
            self._suppressed.append(bound)
            for gen in node.generators:
                self.load(gen.iter, ctx)
                for cond in gen.ifs:
                    self.load(cond, ctx)
            if isinstance(node, ast.DictComp):
                self.load(node.key, ctx)
                self.load(node.value, ctx)
            else:
                self.load(node.elt, ctx)
            self._suppressed.pop()
        elif isinstance(node, (ast.Await, ast.Yield, ast.YieldFrom)):
            self._illegal(f"{type(node).__name__.lower()} is not supported in a marked block", node)
        else:
            self._generic_load(node, ctx)

    def _generic_load(self, node: ast.expr, ctx: Tuple[str, ...]) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, ast.expr):
                self.load(child, ctx)

    def _call(self, node: ast.Call, ctx: Tuple[str, ...]) -> None:
        func = node.func
        if isinstance(func, ast.Name):
            if not self._suppressed_name(func.id):
                self.called_paths.add(func.id)
        elif isinstance(func, ast.Attribute):
            target = _chain(func)
            if (
                target is not None
                and target[2] is None
                and not target[3]
                and not self._suppressed_name(target[0])
            ):
                self.called_paths.add(target[1])
        if isinstance(func, ast.Name):
            if not self._suppressed_name(func.id):
                self._emit(_READ, func.id, ctx, node)
        elif isinstance(func, ast.Attribute):
            chain = _chain(func.value)
            if (
                chain is not None
                and func.attr in _MUTATING_METHODS
                and not self._suppressed_name(chain[0])
            ):
                root, base, first_index, slices = chain
                self._emit(_READ, root, ctx, node)
                for s in slices:
                    self.load(s, ctx)
                if first_index is None:
                    self._emit(_OPAQUE, base, ctx, node)
                else:
                    self.has_inplace_mutation = True
                    self._emit(_SUBREAD, base, ctx, node, index=first_index)
                    self._emit(_SUBWRITE, base, ctx, node, index=first_index)
            else:
                self.load(func.value, ctx)
        else:
            self.load(func, ctx)
        for arg in node.args:
            self._call_arg(arg, ctx)
        for kw in node.keywords:
            self._call_arg(kw.value, ctx)

    def _call_arg(self, node: ast.expr, ctx: Tuple[str, ...]) -> None:
        if isinstance(node, ast.Lambda):
            for default in list(node.args.defaults) + [d for d in node.args.kw_defaults if d]:
                self.load(default, ctx)
            self._suppressed.append({a.arg for a in _all_args(node.args)})
            self.load(node.body, ctx)
            self._suppressed.pop()
            return
        if isinstance(node, ast.GeneratorExp):
            bound: Set[str] = set()
            for gen in node.generators:
                bound.update(_target_names(gen.target))
            self._suppressed.append(bound)
            for gen in node.generators:
                self.load(gen.iter, ctx)
                for cond in gen.ifs:
                    self.load(cond, ctx)
            self.load(node.elt, ctx)
            self._suppressed.pop()
            return
        self.load(node, ctx)


def _chain(node: ast.expr) -> Optional[Tuple[str, str, Optional[ast.expr], List[ast.expr]]]:
    elems: List[ast.expr] = []
    cur: ast.expr = node
    while isinstance(cur, (ast.Attribute, ast.Subscript)):
        elems.append(cur)
        cur = cur.value
    if not isinstance(cur, ast.Name):
        return None
    elems.reverse()
    parts = [cur.id]
    first_index: Optional[ast.expr] = None
    slices: List[ast.expr] = []
    for elem in elems:
        if isinstance(elem, ast.Attribute):
            if first_index is None:
                parts.append(elem.attr)
        else:
            if first_index is None:
                first_index = elem.slice
            slices.append(elem.slice)
    return cur.id, ".".join(parts), first_index, slices


def _all_args(args: ast.arguments) -> List[ast.arg]:
    out = list(args.posonlyargs) + list(args.args) + list(args.kwonlyargs)
    if args.vararg:
        out.append(args.vararg)
    if args.kwarg:
        out.append(args.kwarg)
    return out


def _selfref_op(name: str, value: ast.expr) -> Optional[str]:
    if isinstance(value, ast.BinOp):
        for side in (value.left, value.right):
            if isinstance(side, ast.Name) and side.id == name:
                return _AUG_OPS.get(type(value.op))
    if (
        isinstance(value, ast.Call)
        and isinstance(value.func, ast.Name)
        and value.func.id in ("min", "max")
        and any(isinstance(a, ast.Name) and a.id == name for a in value.args)
    ):
        return value.func.id
    return None


def _classify(
    events: List[_Event], domain: LoopDomain, filename: str, branch_sensitive: bool = False
) -> Dict[str, TargetInfo]:
    grouped: Dict[str, List[_Event]] = {}
    for e in events:
        grouped.setdefault(e.path, []).append(e)

    written = {e.path for e in events if e.kind in (_WRITE, _AUG)}
    proven = domain.proven - written
    values = domain.values - written

    targets: Dict[str, TargetInfo] = {}
    for path, evs in grouped.items():
        if "." in path or path in domain.names:
            continue
        targets[path] = _classify_path(path, evs, proven, values, filename, branch_sensitive)
    for path, evs in grouped.items():
        if "." not in path:
            continue
        root = path.split(".", 1)[0]
        if all(e.kind in (_READ, _SUBREAD) for e in evs):
            continue
        if root in domain.names:
            if root in values:
                targets[path] = TargetInfo(
                    Classification.SHARED_INDEXED_SAFE, audit_tier=AuditTier.BY_ASSUMPTION
                )
            else:
                targets[path] = TargetInfo(Classification.SHARED_INDEXED_UNRESOLVED)
            continue
        root_info = targets.get(root)
        if root_info is not None and root_info.classification is Classification.LOOP_LOCAL:
            continue
        targets[path] = _classify_path(path, evs, proven, values, filename, branch_sensitive)
    return targets


def _classify_path(
    path: str,
    evs: List[_Event],
    proven: FrozenSet[str],
    values: FrozenSet[str],
    filename: str,
    branch_sensitive: bool = False,
) -> TargetInfo:
    first = evs[0]
    if (
        first.kind == _WRITE
        and not first.selfref
        and all(_ctx_prefix(first.ctx, e.ctx) for e in evs)
        and (first.ctx == () or any(e.kind == _READ for e in evs))
    ):
        return TargetInfo(Classification.LOOP_LOCAL)

    kinds = {e.kind for e in evs}
    if _SUBWRITE in kinds:
        if _OPAQUE in kinds or _WRITE in kinds or _AUG in kinds:
            return TargetInfo(Classification.SHARED_INDEXED_UNRESOLVED)
        relaxed = _check_branch_agreement(path, evs, proven, values, filename, branch_sensitive)
        if relaxed:
            return TargetInfo(
                Classification.SHARED_INDEXED_SAFE,
                audit_tier=AuditTier.ASSERTED,
                write_indexes=[e.index for e in evs if e.kind == _SUBWRITE],
            )
        write_indexes = [e.index for e in evs if e.kind == _SUBWRITE]
        read_indexes = [e.index for e in evs if e.kind == _SUBREAD]
        in_place = any(e.nested for e in evs if e.kind == _SUBWRITE)
        if read_indexes:
            return TargetInfo(
                Classification.READ_AFTER_WRITE,
                write_indexes=write_indexes,
                read_indexes=read_indexes,
                in_place=in_place,
            )
        classes = {_index_class(ix, proven, values) for ix in write_indexes}
        if classes == {"proven"}:
            return TargetInfo(
                Classification.SHARED_INDEXED_SAFE,
                audit_tier=AuditTier.BY_PROOF,
                write_indexes=write_indexes,
                in_place=in_place,
            )
        if classes == {"value"}:
            return TargetInfo(
                Classification.SHARED_INDEXED_SAFE,
                audit_tier=AuditTier.BY_ASSUMPTION,
                write_indexes=write_indexes,
                in_place=in_place,
            )
        return TargetInfo(Classification.SHARED_INDEXED_UNRESOLVED, write_indexes=write_indexes)
    if _OPAQUE in kinds:
        return TargetInfo(Classification.SHARED_INDEXED_UNRESOLVED)
    if _AUG in kinds or _WRITE in kinds:
        nested_reduction = any(e.in_loop for e in evs if e.kind in (_AUG, _WRITE))
        return TargetInfo(
            Classification.SHARED_SCALAR,
            reduce_op=_scalar_op(evs),
            nested_reduction=nested_reduction,
        )
    return TargetInfo(Classification.OUTER_READONLY)


def _check_branch_agreement(
    path: str,
    evs: List[_Event],
    proven: FrozenSet[str],
    values: FrozenSet[str],
    filename: str,
    branch_sensitive: bool = False,
) -> bool:
    touched = [e for e in evs if e.kind in (_SUBREAD, _SUBWRITE)]
    arms = sorted({e.ctx for e in touched if e.ctx})
    if len(arms) < 2:
        return False
    write_classes = {_index_class(e.index, proven, values) for e in touched if e.kind == _SUBWRITE}
    if len(write_classes) <= 1:
        return False
    any_read = any(e.kind == _SUBREAD for e in touched)
    if branch_sensitive and not any_read:
        return True
    lines = sorted({e.lineno for e in touched})
    raise BranchMergeConflictError(
        f"branch arms disagree on the write index of '{path}' "
        f"(lines {', '.join(map(str, lines))}); every branch must write it at "
        "the same provably-distinct index (spec §5.3.3)",
        file=filename,
        line=lines[0],
    )


def _ctx_prefix(prefix: Tuple[str, ...], ctx: Tuple[str, ...]) -> bool:
    return len(prefix) <= len(ctx) and ctx[: len(prefix)] == prefix


def _index_class(index: Optional[ast.expr], proven: FrozenSet[str], values: FrozenSet[str]) -> str:
    if isinstance(index, ast.Name):
        if index.id in proven:
            return "proven"
        if index.id in values:
            return "value"
    return "other"


def _scalar_op(evs: List[_Event]) -> Optional[str]:
    if any(e.kind == _READ for e in evs):
        return None
    if any(e.in_loop for e in evs if e.kind in (_AUG, _WRITE)):
        return None
    ops: Set[Optional[str]] = set()
    for e in evs:
        if e.kind == _AUG:
            ops.add(e.op)
        elif e.kind == _WRITE:
            if not e.selfref:
                return None
            ops.add(e.op)
    if len(ops) == 1:
        return ops.pop()
    return None


def _check_conditions(
    conditions: List[Tuple[int, Set[str]]],
    targets: Dict[str, TargetInfo],
    domain: LoopDomain,
    filename: str,
) -> None:
    allowed = (Classification.LOOP_LOCAL, Classification.OUTER_READONLY)
    for lineno, names in conditions:
        for path in sorted(names):
            root = path.split(".", 1)[0]
            if root in domain.names:
                continue
            info = targets.get(path) or targets.get(root)
            if info is not None and info.classification not in allowed:
                raise IllegalSyntaxInBlockError(
                    f"condition references '{path}' ({info.classification.name}); "
                    "conditions may only use LOOP_LOCAL or OUTER_READONLY names "
                    "(spec §5.3.3)",
                    file=filename,
                    line=lineno,
                )
