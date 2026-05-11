# Extracts human-readable failure detail from subprocess exceptions
from __future__ import annotations


# Return the last line of captured stderr if present, else str(exc), so failures show the actual reason instead of a bare exception name
def exc_detail(exc: Exception) -> str:
    stderr = getattr(exc, "stderr", None)
    if stderr and stderr.strip():
        return stderr.strip().splitlines()[-1]
    return str(exc)
