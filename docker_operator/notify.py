# Sends best-effort notifications through Apprise as titled markdown messages, to any service it supports
from __future__ import annotations
import logging

import apprise

log = logging.getLogger("docker_operator.notify")

# Services that show a sender name, user agent or link use this instead of Apprise's own defaults
_ASSET = apprise.AppriseAsset(app_id="Docker Operator", app_desc="Docker Operator",
                              app_url="https://github.com/ddrimus/docker-operator")

# Apprise notification types; every service renders them its own way (color, icon or priority), so success/warning/error read at a glance anywhere
TYPE_SUCCESS = apprise.NotifyType.SUCCESS
TYPE_WARNING = apprise.NotifyType.WARNING
TYPE_ERROR = apprise.NotifyType.FAILURE


# Best-effort notification to every Apprise URL in urls (comma or space separated, e.g. "tgram://token/chat, ntfys://host/topic"); no-op if urls is unset. Body is markdown, Apprise converts it per service (HTML for email, plain text where nothing richer is supported) and splits or trims it to each service's own size limit
def notify(urls: str | None, title: str, body: str, notify_type: str) -> None:
    if not urls:
        return
    try:
        apobj = apprise.Apprise(asset=_ASSET)
        if not apobj.add(urls):
            log.warning("one or more NOTIFY_URLS entries are not valid Apprise URLs and were skipped")
        if not apobj:
            return
        if not apobj.notify(body=body, title=title, notify_type=notify_type,
                            body_format=apprise.NotifyFormat.MARKDOWN):
            log.warning("failed to send notification to one or more services: %s", title)
    except Exception as exc:
        log.warning("failed to send notification: %s", exc)
