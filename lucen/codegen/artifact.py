from __future__ import annotations

import builtins
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional, Tuple


@dataclass(frozen=True)
class SlabPlan:
    param: str
    container: str
    kind: str
    cell: Optional[str] = None


@dataclass(frozen=True)
class ReductionPlan:
    scalar: str
    op: str
    site_params: Tuple[str, ...]


@dataclass
class ChunkArtifact:
    block_line: int
    name: str
    source: str
    params: List[str]
    seq_name: str
    seq_source: str
    seq_params: List[str]
    domain: str
    loop_targets: List[str]
    target_source: str
    slabs: List[SlabPlan]
    reductions: List[ReductionPlan]
    transactional: bool
    collect_errors: bool = False
    per_task_deadline: bool = False
    progress_per_task: bool = False
    early_exit: bool = False
    buffer_fast_path: bool = False
    inplace_mutation: bool = False
    structured_payload: bool = False
    sliceable: Tuple[str, ...] = ()
    dense: bool = False

    def compile_pair(self) -> Tuple[Callable, Callable]:
        chunk_ns: Dict[str, Any] = {}
        exec(compile(self.source, f"<lucen:{self.name}>", "exec"), {"__builtins__": {}}, chunk_ns)
        seq_ns: Dict[str, Any] = {}
        exec(
            compile(self.seq_source, f"<lucen:{self.seq_name}>", "exec"),
            {"__builtins__": vars(builtins)},
            seq_ns,
        )
        return chunk_ns[self.name], seq_ns[self.seq_name]
