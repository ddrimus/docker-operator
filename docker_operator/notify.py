# Sends best-effort Discord-compatible webhook notifications as rich embeds
from __future__ import annotations
import json
import logging
import urllib.request

log = logging.getLogger("docker_operator.notify")

# Discord's embed description hard cap is 4096; leave headroom for our own truncation marker
_DESCRIPTION_LIMIT = 4000

# Decimal color ints Discord embeds expect; green/orange/red read as success/warning/error at a glance
COLOR_SUCCESS = 0x2ECC71
COLOR_WARNING = 0xFF9900
COLOR_ERROR = 0xE74C3C


# Best-effort Discord-compatible embed notification; no-op if url is unset
def notify(url: str | None, title: str, description: str, color: int) -> None:
    if not url:
        return
    if len(description) > _DESCRIPTION_LIMIT:
        description = description[:_DESCRIPTION_LIMIT - 1] + "…"
    body = json.dumps({
        "username": "Docker Operator",
        "embeds": [{"title": title, "description": description, "color": color}],
    }).encode()
    headers = {"Content-Type": "application/json", "User-Agent": "docker-operator-notify/1.0"}
    req = urllib.request.Request(url, data=body, headers=headers)
    try:
        urllib.request.urlopen(req, timeout=10)
    except Exception as exc:
        log.warning("failed to send notification: %s", exc)
