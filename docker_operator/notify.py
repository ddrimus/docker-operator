# Sends best-effort notifications through Apprise, to any service it supports
from __future__ import annotations
import logging

import apprise

log = logging.getLogger("docker_operator.notify")

# Services that show a sender name, user agent or link use this instead of Apprise's own defaults
_ASSET = apprise.AppriseAsset(app_id="Docker Operator", app_desc="Docker Operator",
                              app_url="https://github.com/ddrimus/docker-operator")


# Best-effort notification to every Apprise URL in urls (comma or space separated, e.g. "tgram://token/chat, ntfys://host/topic"); no-op if urls is unset
def notify(urls: str | None, text: str) -> None:
    if not urls:
        return
    try:
        apobj = apprise.Apprise(asset=_ASSET)
        if not apobj.add(urls):
            log.warning("one or more NOTIFY_URLS entries are not valid Apprise URLs and were skipped")
        if not apobj:
            return
        if not apobj.notify(body=text):
            log.warning("failed to send notification to one or more services")
    except Exception as exc:
        log.warning("failed to send notification: %s", exc)
