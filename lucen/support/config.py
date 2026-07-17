from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any, Dict, FrozenSet, Optional, Tuple, TypeVar

try:
    import tomllib
except ModuleNotFoundError:
    import tomli as tomllib  # type: ignore[no-redef]

from lucen.support.errors import ClauseValueError, report_fallback, set_errors_mode

_DEFAULTS_KEYS = frozenset(
    {
        "backend",
        "pool_size",
        "chunks",
        "calibrate",
        "on_error",
        "strict",
        "reduction_order",
        "progress",
    }
)
_LIMITS_KEYS = frozenset(
    {
        "max_threads_per_block",
        "max_total_threads",
        "max_processes_per_block",
        "max_timeout_seconds",
        "allow_experimental",
    }
)
_SECTIONS = frozenset(
    {
        "scope",
        "defaults",
        "limits",
        "strict",
        "errors",
        "experimental",
        "trust",
        "observability",
    }
)


@dataclass(frozen=True)
class Config:
    scope_include: Tuple[str, ...] = ()
    scope_exclude: Tuple[str, ...] = ()
    defaults: Dict[str, Any] = field(default_factory=dict)
    max_threads_per_block: Optional[int] = None
    max_total_threads: Optional[int] = None
    max_processes_per_block: Optional[int] = None
    max_timeout_seconds: Optional[float] = None
    allow_experimental: bool = True
    strict_default: bool = False
    strict_allow: FrozenSet[str] = frozenset()
    errors_mode: str = "report"
    experimental: FrozenSet[str] = frozenset()
    trust_callables: FrozenSet[str] = frozenset()
    log_downgrades: bool = True

    def default_for(self, key: str) -> Any:
        return self.defaults.get(key)


_active = Config()


def active() -> Config:
    return _active


def set_active(cfg: Config) -> None:
    global _active
    _active = cfg
    set_errors_mode(cfg.errors_mode)


def load(path: str) -> Config:
    with open(path, "rb") as f:
        try:
            raw = tomllib.load(f)
        except tomllib.TOMLDecodeError as exc:
            raise ClauseValueError(f"lucen.toml is not valid TOML: {exc}", file=path) from exc
    unknown = set(raw) - _SECTIONS
    if unknown:
        raise ClauseValueError(f"lucen.toml: unknown section(s) {sorted(unknown)}", file=path)

    defaults = dict(raw.get("defaults", {}))
    bad = set(defaults) - _DEFAULTS_KEYS
    if bad:
        raise ClauseValueError(
            f"lucen.toml [defaults]: unknown or non-configurable key(s) "
            f"{sorted(bad)} (callable-valued expert clauses are never "
            "TOML-settable, spec 7)",
            file=path,
        )

    limits = dict(raw.get("limits", {}))
    bad = set(limits) - _LIMITS_KEYS
    if bad:
        raise ClauseValueError(f"lucen.toml [limits]: unknown key(s) {sorted(bad)}", file=path)

    _require_positive_int(defaults, "pool_size", "[defaults]", path)
    _require_positive_int(defaults, "chunks", "[defaults]", path)
    for key in ("max_threads_per_block", "max_total_threads", "max_processes_per_block"):
        _require_positive_int(limits, key, "[limits]", path)
    timeout_ceiling = limits.get("max_timeout_seconds")
    if timeout_ceiling is not None and (
        not isinstance(timeout_ceiling, (int, float))
        or isinstance(timeout_ceiling, bool)
        or timeout_ceiling <= 0
    ):
        raise ClauseValueError(
            f"lucen.toml [limits].max_timeout_seconds: {timeout_ceiling!r} "
            "must be a number greater than 0",
            file=path,
        )

    strict = dict(raw.get("strict", {}))
    strict_default = bool(strict.get("default", False))
    strict_allow = frozenset(strict.get("allow", []))
    if strict.get("ci_mode", False):
        strict_default, strict_allow = True, frozenset()

    errors = dict(raw.get("errors", {}))
    mode = errors.get("mode", "report")
    if mode not in ("report", "quiet", "hard"):
        raise ClauseValueError(
            f"lucen.toml [errors].mode: {mode!r} is not one of report, quiet, hard", file=path
        )

    scope = dict(raw.get("scope", {}))
    experimental = frozenset(raw.get("experimental", {}).get("enabled", []))
    if not limits.get("allow_experimental", True):
        experimental = frozenset()

    trust = dict(raw.get("trust", {}))
    bad = set(trust) - {"callables"}
    if bad:
        raise ClauseValueError(f"lucen.toml [trust]: unknown key(s) {sorted(bad)}", file=path)
    trust_callables = trust.get("callables", [])
    if not isinstance(trust_callables, list) or not all(
        isinstance(n, str) for n in trust_callables
    ):
        raise ClauseValueError(
            "lucen.toml [trust].callables must be a list of dotted names", file=path
        )

    return Config(
        scope_include=tuple(scope.get("include", [])),
        scope_exclude=tuple(scope.get("exclude", [])),
        defaults=defaults,
        max_threads_per_block=limits.get("max_threads_per_block"),
        max_total_threads=limits.get("max_total_threads"),
        max_processes_per_block=limits.get("max_processes_per_block"),
        max_timeout_seconds=limits.get("max_timeout_seconds"),
        allow_experimental=bool(limits.get("allow_experimental", True)),
        strict_default=strict_default,
        strict_allow=strict_allow,
        errors_mode=mode,
        experimental=experimental,
        trust_callables=frozenset(trust_callables),
        log_downgrades=bool(raw.get("observability", {}).get("log_downgrades", True)),
    )


def _require_positive_int(section: Dict[str, Any], key: str, where: str, path: str) -> None:
    value = section.get(key)
    if value is None:
        return
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise ClauseValueError(
            f"lucen.toml {where}.{key}: {value!r} must be an integer >= 1", file=path
        )


def discover(start_dir: Optional[str] = None) -> Optional[str]:
    directory = os.path.abspath(start_dir or os.getcwd())
    while True:
        candidate = os.path.join(directory, "lucen.toml")
        if os.path.isfile(candidate):
            return candidate
        parent = os.path.dirname(directory)
        if parent == directory:
            return None
        directory = parent


_Numeric = TypeVar("_Numeric", int, float)


def clamp(
    value: _Numeric, ceiling: Optional[_Numeric], what: str, filename: str, line: int
) -> _Numeric:
    if ceiling is None or value <= ceiling:
        return value
    report_fallback(
        f"{what}={value} exceeds the configured ceiling {ceiling}; clamped",
        file=filename,
        line=line,
        error="LimitClamp",
    )
    return ceiling
