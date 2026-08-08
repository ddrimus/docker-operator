# Extracts human-readable failure detail from subprocess exceptions
from __future__ import annotations
import os
from pathlib import Path


# Return the last line of captured stderr if present, else str(exc), so failures show the actual reason instead of a bare exception name
def exc_detail(exc: Exception) -> str:
    stderr = getattr(exc, "stderr", None)
    if stderr and stderr.strip():
        return stderr.strip().splitlines()[-1]
    return str(exc)


# chown a directory tree to make it browsable/editable by a host user; no-op when both are unset (the default, root-owned) behavior
def chown_recursive(path: Path, uid: int | None, gid: int | None) -> None:
    if uid is None and gid is None:
        return
    u = uid if uid is not None else -1
    g = gid if gid is not None else -1
    for root, _dirs, files in os.walk(path):
        os.chown(root, u, g)
        for name in files:
            os.chown(os.path.join(root, name), u, g)
