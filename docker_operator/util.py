# Extracts human-readable failure detail from subprocess exceptions
from __future__ import annotations
import re


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
