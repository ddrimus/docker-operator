# HTTP server handling git webhooks and periodic/background reconcile triggers
from __future__ import annotations
import json
import logging
import signal
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .config import Settings
from .reconcile import reconcile
from .security import verify_signature, verify_token

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


def _poll_loop(settings: Settings, stop: threading.Event) -> None:
    if settings.poll_interval_seconds <= 0:
        return
    while not stop.wait(settings.poll_interval_seconds):
        request_resync()


# How each git server signs and labels its webhooks: (secret header, HMAC-signed body or plain secret, event header, push event); checked in this order since Forgejo also sends Gitea's and Gogs' headers and Gitea also sends GitHub's, so the first match is always the server's own
_PROVIDERS = (
    ("X-Forgejo-Signature", True, "X-Forgejo-Event", "push"),
    ("X-Gitea-Signature", True, "X-Gitea-Event", "push"),
    ("X-Gogs-Signature", True, "X-Gogs-Event", "push"),
    ("X-Hub-Signature-256", True, "X-GitHub-Event", "push"),
    ("X-Gitlab-Token", False, "X-Gitlab-Event", "Push Hook"),
)


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
            try:
                self._handle_webhook()
            except Exception:
                # Keeps a client disconnect mid-body or similar off the raw stderr traceback socketserver would otherwise print
                log.exception("unhandled error handling webhook from %s", self.client_address[0])
                self._send(500, "internal error")

        def _handle_webhook(self):
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

            # The first secret header present says which server sent it; a wrong secret there is rejected outright, never retried against another server's header
            provider = next((p for p in _PROVIDERS if self.headers.get(p[0]) is not None), None)
            if provider is None:
                valid = False
            else:
                secret_header, signed, event_header, push_event = provider
                sent = self.headers.get(secret_header)
                valid = (verify_signature(settings.webhook_secret, body, sent) if signed
                         else verify_token(settings.webhook_secret, sent))
            if not valid:
                log.warning("rejected webhook: invalid signature from %s", self.client_address[0])
                self._send(401, "invalid signature")
                return

            event = self.headers.get(event_header, "")
            if event == "ping":
                self._send(200, "pong")
                return
            if event != push_event:
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


# No connection-count bound: the 15s socket timeout and 5 MB body cap keep every connection cheap, and only signed pushes ever trigger work; meant to be reachable from your git server on the LAN, hosted ones that can't reach it can rely on polling instead
class _Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def run(settings: Settings) -> None:
    stop = threading.Event()
    worker = threading.Thread(target=_worker_loop, args=(settings, stop), daemon=True, name="resync-worker")
    poller = threading.Thread(target=_poll_loop, args=(settings, stop), daemon=True, name="resync-poller")
    worker.start()
    poller.start()

    # Self-heal after downtime / missed webhooks
    request_resync()

    httpd = _Server((settings.listen_host, settings.listen_port), make_handler(settings))

    def _handle_sigterm(signum, frame) -> None:
        log.info("received SIGTERM, shutting down")
        # Calling httpd.shutdown() inline would deadlock since this handler runs on the thread blocked in serve_forever()
        threading.Thread(target=httpd.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, _handle_sigterm)

    log.info("listening on %s:%s%s", settings.listen_host, settings.listen_port, settings.webhook_path)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        log.info("received SIGINT, shutting down")
    finally:
        stop.set()
        # No-op if the SIGTERM path already triggered it
        httpd.shutdown()
        # Both threads are daemons and would otherwise be killed mid-reconcile once this function returns
        worker.join(timeout=5)
        poller.join(timeout=5)
        log.info("shutdown complete")
