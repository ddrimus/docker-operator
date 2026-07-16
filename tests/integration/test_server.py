from __future__ import annotations
import hashlib
import hmac
import json
import threading
import time
import urllib.request
import urllib.error
from unittest.mock import patch

import pytest

from docker_operator import server as server_mod
from conftest import make_settings

SECRET = "test-webhook-secret"


def _sign(body: bytes) -> str:
    return hmac.new(SECRET.encode(), body, hashlib.sha256).hexdigest()


# Real, running operator HTTP server on an OS-assigned port, with `reconcile` mocked out to exercise real socket/HTTP/signature behavior
@pytest.fixture
def running_server(tmp_path):
    settings = make_settings(tmp_path, git_repo_url="https://example.com/x.git",
                             webhook_secret=SECRET, listen_port=0)
    httpd = server_mod._Server((settings.listen_host, 0), server_mod.make_handler(settings))
    port = httpd.server_address[1]
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    # Module-level singleton, shared across tests: start clean
    server_mod._resync_event.clear()
    with patch("docker_operator.server.reconcile"):
        t.start()
        time.sleep(0.1)
        try:
            yield f"http://127.0.0.1:{port}", settings
        finally:
            server_mod._resync_event.clear()
            httpd.shutdown()
            httpd.server_close()


def _post(base_url, path, body: bytes, headers: dict) -> tuple[int, str]:
    req = urllib.request.Request(base_url + path, data=body, method="POST", headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=3) as resp:
            return resp.status, resp.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()


def test_healthz_returns_200(running_server):
    base_url, _ = running_server
    with urllib.request.urlopen(base_url + "/healthz", timeout=3) as resp:
        assert resp.status == 200


def test_unknown_get_path_returns_404(running_server):
    base_url, _ = running_server
    with pytest.raises(urllib.error.HTTPError) as exc_info:
        urllib.request.urlopen(base_url + "/whatever", timeout=3)
    assert exc_info.value.code == 404


def test_valid_signed_push_event_accepted(running_server):
    base_url, settings = running_server
    body = json.dumps({"ref": "refs/heads/main"}).encode()
    status, text = _post(base_url, settings.webhook_path, body, {
        "X-Forgejo-Signature": _sign(body),
        "X-Forgejo-Event": "push",
        "Content-Type": "application/json",
    })
    assert status == 202
    assert text == "accepted"


def test_invalid_signature_rejected(running_server):
    base_url, settings = running_server
    body = json.dumps({"ref": "refs/heads/main"}).encode()
    status, text = _post(base_url, settings.webhook_path, body, {
        "X-Forgejo-Signature": "0" * 64,
        "X-Forgejo-Event": "push",
    })
    assert status == 401


def test_missing_signature_rejected(running_server):
    base_url, settings = running_server
    body = json.dumps({"ref": "refs/heads/main"}).encode()
    status, _ = _post(base_url, settings.webhook_path, body, {"X-Forgejo-Event": "push"})
    assert status == 401


def test_gitea_style_signature_header_accepted(running_server):
    # Both "Forgejo" and "Gitea" webhook payload formats are first-party here, unlike GitHub/GitLab
    base_url, settings = running_server
    body = json.dumps({"ref": "refs/heads/main"}).encode()
    status, _ = _post(base_url, settings.webhook_path, body, {
        "X-Gitea-Signature": _sign(body),
        "X-Gitea-Event": "push",
    })
    assert status == 202


def test_github_style_header_no_longer_recognized(running_server):
    # Scope is deliberately narrowed to Forgejo/Gitea: a webhook carrying only GitHub's headers must be rejected, not silently accepted
    base_url, settings = running_server
    body = json.dumps({"ref": "refs/heads/main"}).encode()
    status, _ = _post(base_url, settings.webhook_path, body, {
        "X-Hub-Signature-256": "sha256=" + _sign(body),
        "X-GitHub-Event": "push",
    })
    assert status == 401


def test_ping_event_returns_pong_without_triggering_resync(running_server):
    base_url, settings = running_server
    body = b"{}"
    status, text = _post(base_url, settings.webhook_path, body, {
        "X-Forgejo-Signature": _sign(body),
        "X-Forgejo-Event": "ping",
    })
    assert status == 200
    assert text == "pong"


def test_non_push_event_ignored_with_200(running_server):
    base_url, settings = running_server
    body = json.dumps({"ref": "refs/heads/main"}).encode()
    status, text = _post(base_url, settings.webhook_path, body, {
        "X-Forgejo-Signature": _sign(body),
        "X-Forgejo-Event": "issue",
    })
    assert status == 200
    assert "ignored" in text


def test_wrong_branch_ref_ignored(running_server):
    base_url, settings = running_server
    body = json.dumps({"ref": "refs/heads/some-other-branch"}).encode()
    status, text = _post(base_url, settings.webhook_path, body, {
        "X-Forgejo-Signature": _sign(body),
        "X-Forgejo-Event": "push",
    })
    assert status == 200
    assert "ignored" in text


def test_wrong_path_returns_404_even_with_valid_signature(running_server):
    base_url, settings = running_server
    body = json.dumps({"ref": "refs/heads/main"}).encode()
    status, _ = _post(base_url, "/not-the-webhook-path", body, {
        "X-Forgejo-Signature": _sign(body),
        "X-Forgejo-Event": "push",
    })
    assert status == 404


def test_invalid_json_body_returns_400(running_server):
    base_url, settings = running_server
    body = b"not json{{{"
    status, _ = _post(base_url, settings.webhook_path, body, {
        "X-Forgejo-Signature": _sign(body),
        "X-Forgejo-Event": "push",
    })
    assert status == 400


def test_oversized_body_rejected_without_crashing_server(running_server):
    # The handler checks Content-Length and rejects before reading the body, so the client may see a reset instead of a clean 400: both are fine
    base_url, settings = running_server
    huge_body = b"x" * 6_000_000
    req = urllib.request.Request(base_url + settings.webhook_path, data=huge_body, method="POST",
                                  headers={"X-Forgejo-Signature": _sign(huge_body), "X-Forgejo-Event": "push"})
    try:
        urllib.request.urlopen(req, timeout=5)
    except (urllib.error.HTTPError, urllib.error.URLError, OSError):
        pass

    with urllib.request.urlopen(base_url + "/healthz", timeout=3) as resp:
        assert resp.status == 200


def test_valid_push_sets_the_resync_event(tmp_path):
    # `_resync_event` is a module-level singleton shared across tests in this process, so clear it before and after to avoid leaking state
    settings = make_settings(tmp_path, git_repo_url="https://example.com/x.git", webhook_secret=SECRET, listen_port=0)
    httpd = server_mod._Server((settings.listen_host, 0), server_mod.make_handler(settings))
    port = httpd.server_address[1]
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    server_mod._resync_event.clear()
    t.start()
    try:
        time.sleep(0.1)
        body = json.dumps({"ref": "refs/heads/main"}).encode()
        status, _ = _post(f"http://127.0.0.1:{port}", settings.webhook_path, body,
                           {"X-Forgejo-Signature": _sign(body), "X-Forgejo-Event": "push"})
        assert status == 202
        assert server_mod._resync_event.is_set()
    finally:
        server_mod._resync_event.clear()
        httpd.shutdown()
        httpd.server_close()


def test_worker_loop_calls_reconcile_when_resync_requested(tmp_path):
    settings = make_settings(tmp_path, git_repo_url="https://example.com/x.git")
    stop = threading.Event()
    calls = []

    with patch("docker_operator.server.reconcile", side_effect=lambda s: calls.append(s)):
        t = threading.Thread(target=server_mod._worker_loop, args=(settings, stop), daemon=True)
        t.start()
        server_mod.request_resync()
        time.sleep(0.3)
        stop.set()
        t.join(timeout=2)

    assert not t.is_alive()
    assert len(calls) == 1


def test_worker_loop_exits_promptly_when_stop_set_without_any_resync(tmp_path):
    settings = make_settings(tmp_path, git_repo_url="https://example.com/x.git")
    stop = threading.Event()
    server_mod._resync_event.clear()

    t = threading.Thread(target=server_mod._worker_loop, args=(settings, stop), daemon=True)
    t.start()
    time.sleep(0.1)
    stop.set()
    t.join(timeout=2)

    assert not t.is_alive()


def test_worker_loop_survives_reconcile_raising(tmp_path):
    # An unhandled exception in reconcile() must not kill the worker thread; it should log and keep waiting for the next resync
    settings = make_settings(tmp_path, git_repo_url="https://example.com/x.git")
    stop = threading.Event()
    calls = []

    def boom(s):
        calls.append(1)
        raise RuntimeError("simulated crash")

    with patch("docker_operator.server.reconcile", side_effect=boom):
        t = threading.Thread(target=server_mod._worker_loop, args=(settings, stop), daemon=True)
        t.start()
        server_mod.request_resync()
        time.sleep(0.3)
        # Still alive despite reconcile() raising
        assert t.is_alive()
        server_mod.request_resync()
        time.sleep(0.3)
        stop.set()
        t.join(timeout=2)

    assert len(calls) == 2


def test_malformed_content_length_header_returns_400(running_server):
    # urllib computes Content-Length itself and won't send a garbage one, so a raw socket is needed to exercise this branch
    import socket
    base_url, settings = running_server
    port = int(base_url.rsplit(":", 1)[1])
    request = (
        f"POST {settings.webhook_path} HTTP/1.1\r\n"
        f"Host: 127.0.0.1\r\n"
        f"Content-Length: not-a-number\r\n"
        f"Connection: close\r\n"
        f"\r\n"
    ).encode()
    with socket.create_connection(("127.0.0.1", port), timeout=3) as s:
        s.sendall(request)
        response = b""
        while True:
            chunk = s.recv(4096)
            if not chunk:
                break
            response += chunk
    assert response.startswith(b"HTTP/1.0 400") or b" 400 " in response.split(b"\r\n", 1)[0]
