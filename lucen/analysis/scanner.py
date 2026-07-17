from __future__ import annotations

import io
import re
import tokenize
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Set, Tuple

from lucen.clauses.registry import validate_clause
from lucen.support.errors import (
    ClauseValueError,
    FallbackRecord,
    LucenError,
    PragmaStructureError,
    PragmaSyntaxError,
    TrustPragmaScopeError,
    raise_or_fallback,
)

# a prefilter may only ever false-positive; the bare keyword still catches
# "#  LUCEN START" spacing
PREFILTER_TOKEN = "LUCEN"

_PRAGMA_RE = re.compile(r"[ \t]*LUCEN[ \t]+(START|END|TRUST)\b[ \t]*(.*)$")

_DEF_RE = re.compile(r"(?:async[ \t]+)?def\b")
_DEF_NAME_RE = re.compile(r"(?:async[ \t]+)?def[ \t]+(\w+)")


@dataclass(frozen=True)
class CallValue:
    base: "ClauseValue"
    args: Tuple["ClauseValue", ...]
    kwargs: Dict[str, "ClauseValue"]


@dataclass(frozen=True)
class ClauseValue:
    raw: str
    kind: str
    value: Any


@dataclass(frozen=True)
class Pragma:
    kind: str
    lineno: int
    col_offset: int
    clauses: Dict[str, ClauseValue]


@dataclass(frozen=True)
class MarkedBlock:
    start: Pragma
    end: Pragma


@dataclass
class ScanResult:
    pragmas: List[Pragma] = field(default_factory=list)
    blocks: List[MarkedBlock] = field(default_factory=list)
    trusted: List[Pragma] = field(default_factory=list)
    trusted_names: Set[str] = field(default_factory=set)
    fallbacks: List[FallbackRecord] = field(default_factory=list)


_TOKEN_RE = re.compile(
    r"""(?P<ws>[ \t]+)
      | (?P<number>-?\d+(?:\.\d+)?)
      | (?P<name>[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*)
      | (?P<string>'[^']*'|"[^"]*")
      | (?P<sym>[=(),\[\]])
    """,
    re.VERBOSE,
)


@dataclass(frozen=True)
class _Tok:
    kind: str
    text: str
    pos: int
    end: int


def _lex(text: str, filename: str, line: int) -> List[_Tok]:
    toks: List[_Tok] = []
    i = 0
    while i < len(text):
        m = _TOKEN_RE.match(text, i)
        if m is None:
            raise ClauseValueError(
                f"unexpected character {text[i]!r} in pragma clauses "
                f"(column offset {i} of {text!r})",
                file=filename,
                line=line,
            )
        i = m.end()
        if m.lastgroup == "ws":
            continue
        toks.append(_Tok(m.lastgroup, m.group(), m.start(), m.end()))
    return toks


class _ClauseParser:
    def __init__(self, text: str, filename: str, line: int):
        self.text = text
        self.filename = filename
        self.line = line
        self.toks = _lex(text, filename, line)
        self.i = 0
        self.last_end = 0

    def _err(self, msg: str) -> "None":
        raise ClauseValueError(msg, file=self.filename, line=self.line)

    def _peek(self, ahead: int = 0) -> Optional[_Tok]:
        j = self.i + ahead
        return self.toks[j] if j < len(self.toks) else None

    def _next(self, expected: str = "more clause text") -> _Tok:
        tok = self._peek()
        if tok is None:
            self._err(f"unexpected end of pragma clauses (expected {expected})")
        self.i += 1
        self.last_end = tok.end
        return tok

    def _expect_sym(self, sym: str) -> None:
        tok = self._next(f"'{sym}'")
        if not (tok.kind == "sym" and tok.text == sym):
            self._err(f"expected '{sym}', got {tok.text!r}")

    def parse(self) -> Dict[str, ClauseValue]:
        clauses: Dict[str, ClauseValue] = {}
        if self._peek() is None:
            return clauses
        while True:
            key_tok = self._next("a clause name")
            if key_tok.kind != "name" or "." in key_tok.text:
                self._err(f"expected a clause name, got {key_tok.text!r}")
            self._expect_sym("=")
            cv = self.value()
            if key_tok.text in clauses:
                self._err(f"duplicate clause '{key_tok.text}'")
            clauses[key_tok.text] = cv
            nxt = self._peek()
            if nxt is None:
                break
            if nxt.kind == "sym" and nxt.text == ",":
                self._next()
                continue
            self._err(f"expected ',' between clauses, got {nxt.text!r}")
        return clauses

    def value(self) -> ClauseValue:
        start_tok = self._peek()
        if start_tok is None:
            self._err("a clause value")
        start = start_tok.pos
        prim = self.primary()
        nxt = self._peek()
        if nxt is not None and nxt.kind == "sym" and nxt.text == "(":
            self._next()
            args, kwargs = self._arglist()
            raw = self.text[start : self.last_end].strip()
            return ClauseValue(raw, "call", CallValue(prim, tuple(args), kwargs))
        return prim

    def primary(self) -> ClauseValue:
        tok = self._next("a value")
        if tok.kind == "number":
            val: Any = float(tok.text) if "." in tok.text else int(tok.text)
            return ClauseValue(tok.text, "literal", val)
        if tok.kind == "string":
            return ClauseValue(tok.text, "literal", tok.text[1:-1])
        if tok.kind == "name":
            if tok.text in ("true", "True"):
                return ClauseValue(tok.text, "literal", True)
            if tok.text in ("false", "False"):
                return ClauseValue(tok.text, "literal", False)
            return ClauseValue(tok.text, "name", tok.text)
        if tok.kind == "sym" and tok.text == "[":
            start = tok.pos
            items: List[ClauseValue] = []
            nxt = self._peek()
            if nxt is not None and nxt.kind == "sym" and nxt.text == "]":
                self._next()
            else:
                while True:
                    items.append(self.primary())
                    sep = self._next("',' or ']'")
                    if sep.kind == "sym" and sep.text == "]":
                        break
                    if sep.kind == "sym" and sep.text == ",":
                        continue
                    self._err(f"expected ',' or ']' in list, got {sep.text!r}")
            raw = self.text[start : self.last_end].strip()
            return ClauseValue(raw, "list", tuple(items))
        self._err(f"unexpected {tok.text!r} in clause value")
        raise AssertionError("unreachable")

    def _arglist(self) -> Tuple[List[ClauseValue], Dict[str, ClauseValue]]:
        args: List[ClauseValue] = []
        kwargs: Dict[str, ClauseValue] = {}
        nxt = self._peek()
        if nxt is not None and nxt.kind == "sym" and nxt.text == ")":
            self._next()
            return args, kwargs
        while True:
            tok = self._peek()
            two = self._peek(1)
            is_kwarg = (
                tok is not None
                and tok.kind == "name"
                and "." not in tok.text
                and two is not None
                and two.kind == "sym"
                and two.text == "="
            )
            if is_kwarg:
                name_tok = self._next()
                self._next()
                v = self.value()
                if name_tok.text in kwargs:
                    self._err(f"duplicate sub-argument '{name_tok.text}'")
                kwargs[name_tok.text] = v
            else:
                if kwargs:
                    self._err("positional sub-argument after a keyword sub-argument")
                args.append(self.value())
            sep = self._next("',' or ')'")
            if sep.kind == "sym" and sep.text == ")":
                break
            if sep.kind == "sym" and sep.text == ",":
                continue
            self._err(f"expected ',' or ')' in sub-arguments, got {sep.text!r}")
        return args, kwargs


def parse_clause_text(
    text: str, *, filename: str = "<clause>", line: int = 0
) -> Dict[str, ClauseValue]:
    return _ClauseParser(text, filename, line).parse()


def scan_source(source: str, filename: str = "<string>") -> ScanResult:
    result = ScanResult()
    if PREFILTER_TOKEN not in source:
        return result
    if not source.endswith("\n"):
        source += "\n"
    try:
        toks = list(tokenize.generate_tokens(io.StringIO(source).readline))
    except (tokenize.TokenError, IndentationError, SyntaxError):
        return result

    for tok in toks:
        if tok.type != tokenize.COMMENT:
            continue
        m = _PRAGMA_RE.match(tok.string[1:])
        if m is None:
            continue
        kind, rest = m.group(1), m.group(2).strip()
        lineno, col = tok.start

        if tok.line[:col].strip():
            _fallback(
                result,
                PragmaSyntaxError(
                    f"LUCEN {kind} pragma must be on its own line, not trailing a statement",
                    file=filename,
                    line=lineno,
                ),
            )
            continue

        if kind == "END":
            if rest:
                raise ClauseValueError(
                    f"LUCEN END takes no clauses (got {rest!r})", file=filename, line=lineno
                )
            clauses: Dict[str, ClauseValue] = {}
        else:
            clauses = parse_clause_text(rest, filename=filename, line=lineno) if rest else {}
            for key, cv in clauses.items():
                try:
                    validate_clause(kind, key, cv)
                except ClauseValueError as e:
                    raise ClauseValueError(e.message, file=filename, line=lineno) from None

        result.pragmas.append(Pragma(kind, lineno, col, clauses))

    _structural_pass(result, source, filename)
    return result


def _structural_pass(result: ScanResult, source: str, filename: str) -> None:
    lines = source.splitlines()
    open_start: Optional[Pragma] = None
    for p in result.pragmas:
        if p.kind == "START":
            if open_start is not None:
                _fallback(
                    result,
                    PragmaStructureError(
                        f"LUCEN START inside an already-open block "
                        f"(opened at line {open_start.lineno})",
                        file=filename,
                        line=p.lineno,
                    ),
                )
                continue
            open_start = p
        elif p.kind == "END":
            if open_start is None:
                _fallback(
                    result,
                    PragmaStructureError(
                        "LUCEN END without a matching START", file=filename, line=p.lineno
                    ),
                )
                continue
            result.blocks.append(MarkedBlock(open_start, p))
            open_start = None
        else:
            if _next_code_line_is_def(lines, p.lineno):
                result.trusted.append(p)
                name = _next_def_name(lines, p.lineno)
                if name:
                    result.trusted_names.add(name)
            else:
                _fallback(
                    result,
                    TrustPragmaScopeError(
                        "# LUCEN TRUST must immediately precede a def/async def",
                        file=filename,
                        line=p.lineno,
                    ),
                )
    if open_start is not None:
        _fallback(
            result,
            PragmaStructureError(
                "LUCEN START without a matching END", file=filename, line=open_start.lineno
            ),
        )


def _next_def_name(lines: List[str], pragma_lineno: int) -> Optional[str]:
    for raw in lines[pragma_lineno:]:
        stripped = raw.strip()
        if not stripped or stripped.startswith("#"):
            continue
        match = _DEF_NAME_RE.match(stripped)
        return match.group(1) if match else None
    return None


def _next_code_line_is_def(lines: List[str], pragma_lineno: int) -> bool:
    for raw in lines[pragma_lineno:]:
        stripped = raw.strip()
        if not stripped or stripped.startswith("#"):
            continue
        return _DEF_RE.match(stripped) is not None
    return False


def _fallback(result: ScanResult, exc: LucenError) -> None:
    result.fallbacks.append(raise_or_fallback(exc))
