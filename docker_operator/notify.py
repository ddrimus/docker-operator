# Sends best-effort Discord-compatible webhook notifications
from __future__ import annotations
import json
import logging
import urllib.request

log = logging.getLogger("docker_operator.notify")

# Discord's hard cap is 2000; leave a little headroom
_DISCORD_CONTENT_LIMIT = 1900


# Best-effort Discord-compatible webhook notification; no-op if url is unset
def notify(url: str | None, text: str) -> None:
    if not url:
        return
    if len(text) > _DISCORD_CONTENT_LIMIT:
        text = text[:_DISCORD_CONTENT_LIMIT - 1] + "…"
    body = json.dumps({"content": text}).encode()
    req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"})
    try:
        urllib.request.urlopen(req, timeout=10)
    except Exception as exc:
        log.warning("failed to send notification: %s", exc)
