# Sends best-effort Discord-compatible webhook notifications as rich embeds
from __future__ import annotations
import json
import logging
import queue
import threading
import urllib.request

log = logging.getLogger("docker_operator.notify")

# Discord's embed description hard cap is 4096; leave headroom for our own truncation marker
_DESCRIPTION_LIMIT = 4000

# Decimal color ints Discord embeds expect; green/orange/red read as success/warning/error at a glance
COLOR_SUCCESS = 0x2ECC71
COLOR_WARNING = 0xFF9900
COLOR_ERROR = 0xE74C3C

# Generous for real use (a homelab reconcile pass rarely queues more than a handful at once) but bounded so a long Discord outage can't grow this without limit; past this, a new notification is dropped and logged rather than blocking the caller to wait for room
_QUEUE_MAXSIZE = 100

# One background sender thread, started lazily on the first real notification, drains this FIFO so every call to notify() below returns immediately; a slow or unreachable webhook can only ever delay other queued notifications behind it, never the reconcile pass that queued them
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


def _worker() -> None:
    while True:
        req = _queue.get()
        try:
            urllib.request.urlopen(req, timeout=10)
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


# Best-effort Discord-compatible embed notification; no-op if url is unset. Only builds the request and queues it here, the actual HTTP POST happens on the background sender thread, see flush() for why that matters
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

    _ensure_worker()
    _adjust_pending(1)
    try:
        _queue.put_nowait(req)
    except queue.Full:
        _adjust_pending(-1)
        log.warning("dropping notification, send queue is full (webhook unreachable?): %s", title)


# Blocks until every notification queued so far has actually been sent (or failed and logged), up to timeout seconds, returning False if that timeout was hit with sends still pending: the sender thread is a daemon and gets killed outright the instant the interpreter exits, so anything still queued at that point would simply never go out, call this right before the process would otherwise exit; a no-op, returning True immediately, if nothing was ever queued. Default is short since server.py's shutdown path shares Docker's stop_grace_period with its own thread joins; the CLI's --once path isn't under that same deadline and passes a longer timeout explicitly
def flush(timeout: float = 5.0) -> bool:
    done = _idle.wait(timeout)
    if not done:
        log.warning("timed out after %ss waiting for %d queued notification(s) to send", timeout, _pending)
    return done
