# Sends best-effort notifications through Apprise as titled markdown messages, to any service it supports
from __future__ import annotations
import logging
import queue
import threading

import apprise

log = logging.getLogger("docker_operator.notify")

# Every title already starts with "Docker Operator", so the sender name stays empty: services that add an author or sender line of their own would otherwise repeat it right above the title
_ASSET = apprise.AppriseAsset(app_id="", app_desc="Docker Operator", app_url="")

# Apprise notification types; every service renders them its own way (color, icon or priority), so success/warning/error read at a glance anywhere
TYPE_SUCCESS = apprise.NotifyType.SUCCESS
TYPE_WARNING = apprise.NotifyType.WARNING
TYPE_ERROR = apprise.NotifyType.FAILURE

# Generous for real use (a homelab reconcile pass rarely queues more than a handful at once) but bounded so a long notification service outage can't grow this without limit; past this, a new notification is dropped and logged rather than blocking the caller to wait for room
_QUEUE_MAXSIZE = 100

# One background sender thread, started lazily on the first real notification, drains this FIFO so every call to notify() below returns immediately; a slow or unreachable service can only ever delay other queued notifications behind it, never the reconcile pass that queued them
_queue: queue.Queue = queue.Queue(maxsize=_QUEUE_MAXSIZE)
_lock = threading.Lock()
_pending = 0
# Set whenever nothing is queued or in flight; flush() just waits on this, cleared the moment something is added
_idle = threading.Event()
_idle.set()
_worker_started = False


def _adjust_pending(delta: int) -> None:
    global _pending
    with _lock:
        _pending += delta
        if _pending <= 0:
            _idle.set()
        else:
            _idle.clear()


# Deliver one notification to every Apprise URL in urls. Body is markdown, Apprise converts it per service (HTML for email, plain text where nothing richer is supported) and splits or trims it to each service's own size limit
def _send(urls: str, title: str, body: str, notify_type: str) -> None:
    apobj = apprise.Apprise(asset=_ASSET)
    if not apobj.add(urls):
        log.warning("one or more NOTIFY_URLS entries are not valid Apprise URLs and were skipped")
    if not apobj:
        return
    if not apobj.notify(body=body, title=title, notify_type=notify_type,
                        body_format=apprise.NotifyFormat.MARKDOWN):
        log.warning("failed to send notification to one or more services: %s", title)


def _worker() -> None:
    while True:
        item = _queue.get()
        try:
            _send(*item)
        except Exception as exc:
            log.warning("failed to send notification: %s", exc)
        finally:
            _adjust_pending(-1)


def _ensure_worker() -> None:
    global _worker_started
    if _worker_started:
        return
    with _lock:
        if _worker_started:
            return
        threading.Thread(target=_worker, name="notify-sender", daemon=True).start()
        _worker_started = True


# Best-effort notification to every Apprise URL in urls (comma or space separated, e.g. "tgram://token/chat, ntfys://host/topic"); no-op if urls is unset. Only queues it here, the actual delivery happens on the background sender thread, see flush() for why that matters
def notify(urls: str | None, title: str, body: str, notify_type: str) -> None:
    if not urls:
        return
    _ensure_worker()
    _adjust_pending(1)
    try:
        _queue.put_nowait((urls, title, body, notify_type))
    except queue.Full:
        _adjust_pending(-1)
        log.warning("dropping notification, send queue is full (notification service unreachable?): %s", title)


# Blocks until every notification queued so far has actually been sent (or failed and logged), up to timeout seconds, returning False if that timeout was hit with sends still pending: the sender thread is a daemon and gets killed outright the instant the interpreter exits, so anything still queued at that point would simply never go out, call this right before the process would otherwise exit; a no-op, returning True immediately, if nothing was ever queued. Default is short since server.py's shutdown path shares Docker's stop_grace_period with its own thread joins; the CLI's --once path isn't under that same deadline and passes a longer timeout explicitly
def flush(timeout: float = 5.0) -> bool:
    done = _idle.wait(timeout)
    if not done:
        log.warning("timed out after %ss waiting for %d queued notification(s) to send", timeout, _pending)
    return done
