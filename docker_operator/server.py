# HTTP server handling Forgejo webhooks and background reconcile triggers
from __future__ import annotations
import json
import logging
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .config import Settings
from .reconcile import reconcile
from .security import verify_signature

log = logging.getLogger("docker_operator.server")

_resync_event = threading.Event()


def request_resync() -> None:
    _resync_event.set()


def _worker_loop(settings: Settings, stop: threading.Event) -> None:
    while not stop.is_set():
        triggered = _resync_event.wait(timeout=1)
        if stop.is_set():
            return
        if triggered:
            _resync_event.clear()
            try:
                reconcile(settings)
            except Exception:
                log.exception("unhandled error during reconcile")


# Build the webhook request handler bound to this run's settings
def make_handler(settings: Settings):
    class Handler(BaseHTTPRequestHandler):
        server_version = "docker-operator/1.0"
        # Socket-level timeout, mitigates slow-client connections held open
        timeout = 15

        def log_message(self, fmt, *args):
            log.info("%s - %s", self.client_address[0], fmt % args)

        def _send(self, code: int, body: str = "") -> None:
            data = body.encode()
            self.send_response(code)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            if data:
                self.wfile.write(data)

        def do_GET(self):
            self._send(200, "ok") if self.path == "/healthz" else self._send(404, "not found")

        def do_POST(self):
            if self.path != settings.webhook_path:
                self._send(404, "not found")
                return

            try:
                length = int(self.headers.get("Content-Length", "0") or "0")
            except ValueError:
                self._send(400, "bad request")
                return
            if length <= 0 or length > 5_000_000:
                self._send(400, "bad request")
                return
            body = self.rfile.read(length)

            # Both header styles are first-party: Forgejo lets you pick "Forgejo" or "Gitea" as the webhook payload type
            sig = self.headers.get("X-Forgejo-Signature") or self.headers.get("X-Gitea-Signature")
            if not verify_signature(settings.webhook_secret, body, sig):
                log.warning("rejected webhook: invalid signature from %s", self.client_address[0])
                self._send(401, "invalid signature")
                return

            event = self.headers.get("X-Forgejo-Event") or self.headers.get("X-Gitea-Event", "")
            if event == "ping":
                self._send(200, "pong")
                return
            if event != "push":
                self._send(200, "ignored (not a push event)")
                return

            try:
                payload = json.loads(body)
            except json.JSONDecodeError:
                self._send(400, "invalid json")
                return

            ref = payload.get("ref", "")
            expected_ref = f"refs/heads/{settings.git_branch}"
            if ref != expected_ref:
                self._send(200, f"ignored (ref {ref} != {expected_ref})")
                return

            request_resync()
            self._send(202, "accepted")

    return Handler


# No connection-count bound: this port is only reachable from other containers on the "forgejo" bridge network, never published
class _Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True
