from __future__ import annotations

import hashlib
import os
import pickle
import sys
import tempfile
from dataclasses import dataclass
from typing import List, Optional, Tuple

from lucen import __version__

_DIR_NAME = ".lucen_cache"

_SCHEMA = "2"


@dataclass
class Entry:
    rewritten: Optional[str]
    specs: List[Tuple[int, object]]


def _key(source: str) -> str:
    h = hashlib.sha256()
    h.update(source.encode("utf-8"))
    h.update(__version__.encode())
    h.update(_SCHEMA.encode())
    h.update(sys.version.split()[0].encode())
    return h.hexdigest()[:16]


def _path(cache_root: str, filename: str, key: str) -> str:
    base = os.path.splitext(os.path.basename(filename))[0]
    return os.path.join(cache_root, _DIR_NAME, f"{base}.{key}.plxc")


def load(cache_root: str, filename: str, source: str) -> Optional[Entry]:
    path = _path(cache_root, filename, _key(source))
    try:
        with open(path, "rb") as f:
            payload = pickle.load(f)
        return Entry(payload["rewritten"], payload["specs"])
    except (OSError, pickle.UnpicklingError, KeyError, EOFError):
        return None


def store(cache_root: str, filename: str, source: str, entry: Entry) -> None:
    directory = os.path.join(cache_root, _DIR_NAME)
    os.makedirs(directory, exist_ok=True)
    path = _path(cache_root, filename, _key(source))
    payload = {"rewritten": entry.rewritten, "specs": entry.specs}
    fd, tmp = tempfile.mkstemp(dir=directory, suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as f:
            pickle.dump(payload, f, protocol=pickle.HIGHEST_PROTOCOL)
        os.replace(tmp, path)
    except OSError:
        try:
            os.unlink(tmp)
        except OSError:
            pass
