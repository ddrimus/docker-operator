# Extracts human-readable failure detail from subprocess exceptions
from __future__ import annotations
import os
import re
from pathlib import Path


# A URL's scheme and userinfo, e.g. https://user:token@host/... or https://token@host/...; greedy up to the last @ before the host, so an @ inside the password is covered too
_URL_USERINFO_RE = re.compile(r"([a-zA-Z][a-zA-Z0-9+.-]*)://([^/\s]+)@")


# Keep a username but hide its password, and hide a lone value outright since that's how tokens are usually passed (https://TOKEN@github.com); ssh URLs never carry a password, so they're left alone
def _hide_userinfo(match: re.Match) -> str:
    scheme, userinfo = match.group(1), match.group(2)
    if "ssh" in scheme.lower():
        return match.group(0)
    user, sep, _ = userinfo.partition(":")
    return f"{scheme}://{user}:***@" if sep else f"{scheme}://***@"


# Hide credentials in every URL inside text, so a token in GIT_REPO_URL never reaches logs or notifications
def redact(text: str) -> str:
    return _URL_USERINFO_RE.sub(_hide_userinfo, text)


# Return the last line of captured stderr if present, else str(exc), so failures show the actual reason instead of a bare exception name; always redacted since git puts the clone URL in both
def exc_detail(exc: Exception) -> str:
    stderr = getattr(exc, "stderr", None)
    if stderr and stderr.strip():
        return redact(stderr.strip().splitlines()[-1])
    return redact(str(exc))


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
