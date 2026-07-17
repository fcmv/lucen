from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

from lucen.codegen import ChunkArtifact


@dataclass
class _Plan:
    domain: str
    base: Optional[range]
    seq: Optional[list]
    n: int

    def indices(self, a: int, b: int):
        if self.domain == "range":
            return self.base[a:b]
        return range(a, b)

    def last_element(self):
        if self.domain == "range":
            return self.base[-1]
        if self.domain == "enumerate":
            return (self.n - 1, self.seq[-1])
        return self.seq[-1]

    def remaining_iter(self, start: int):
        if self.domain == "range":
            return self.base[start:]
        if self.domain == "enumerate":
            return zip(range(start, self.n), self.seq[start:])
        return self.seq[start:]

    def sub_iter(self, a: int, b: int):
        if self.domain == "range":
            return self.base[a:b]
        if self.domain == "enumerate":
            return zip(range(a, b), self.seq[a:b])
        return self.seq[a:b]

    def element_at(self, pos: int):
        if self.domain == "range":
            return self.base[pos]
        if self.domain == "enumerate":
            return (pos, self.seq[pos])
        return self.seq[pos]


@dataclass
class _Record:
    idx: int
    a: int
    b: int
    slabs: Dict[str, Any]
    sites: Dict[str, list]
    errors: List[Tuple[int, BaseException]]
    error: Optional[BaseException] = None
    exit_pos: Optional[int] = None


def _plan_domain(artifact: ChunkArtifact, iterable) -> _Plan:
    if artifact.domain == "range":
        return _Plan("range", iterable, None, len(iterable))
    seq = iterable if isinstance(iterable, (list, tuple)) else list(iterable)
    return _Plan(artifact.domain, None, list(seq), len(seq))


def _bounds(n: int, n_chunks: int) -> List[Tuple[int, int]]:
    step = max(1, -(-n // max(n_chunks, 1)))
    return [(a, min(a + step, n)) for a in range(0, n, step)]
