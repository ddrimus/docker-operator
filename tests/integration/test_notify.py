from __future__ import annotations
import json
import threading
import time
from unittest.mock import patch

from docker_operator.notify import COLOR_ERROR, COLOR_SUCCESS, flush, notify


def test_noop_when_url_is_none():
    with patch("urllib.request.urlopen") as mock_urlopen:
        notify(None, "title", "description", COLOR_SUCCESS)
        flush()
    mock_urlopen.assert_not_called()


def test_posts_embed_to_url():
    with patch("urllib.request.urlopen") as mock_urlopen:
        notify("https://discord.example.com/webhook", "some title", "some description", COLOR_ERROR)
        flush()
    assert mock_urlopen.call_count == 1
    req = mock_urlopen.call_args.args[0]
    assert req.full_url == "https://discord.example.com/webhook"
    body = json.loads(req.data)
    assert body["username"] == "Docker Operator"
    assert body["embeds"] == [{"title": "some title", "description": "some description", "color": COLOR_ERROR}]
    assert req.headers.get("Content-type") == "application/json"


def test_long_description_truncated_under_discord_limit():
    long_text = "x" * 5000
    with patch("urllib.request.urlopen") as mock_urlopen:
        notify("https://discord.example.com/webhook", "title", long_text, COLOR_SUCCESS)
        flush()
    description = json.loads(mock_urlopen.call_args.args[0].data)["embeds"][0]["description"]
    assert len(description) <= 4096
    assert description.endswith("…")


def test_short_description_not_truncated():
    with patch("urllib.request.urlopen") as mock_urlopen:
        notify("https://discord.example.com/webhook", "title", "short", COLOR_SUCCESS)
        flush()
    description = json.loads(mock_urlopen.call_args.args[0].data)["embeds"][0]["description"]
    assert description == "short"


def test_network_failure_is_swallowed_not_raised(caplog):
    with caplog.at_level("WARNING", logger="docker_operator.notify"), \
         patch("urllib.request.urlopen", side_effect=OSError("connection refused")):
        # Must not raise
        notify("https://discord.example.com/webhook", "title", "description", COLOR_ERROR)
        flush()
    assert any("failed to send notification" in r.message for r in caplog.records)


# --- notify() must return immediately, the actual POST happens on a background sender thread (see flush()); these prove that by making the mocked network call itself slow and asserting notify() doesn't wait around for it ---

def test_notify_returns_before_the_webhook_call_completes():
    release = threading.Event()

    def slow_urlopen(req, timeout=None):
        release.wait(5)

    with patch("urllib.request.urlopen", side_effect=slow_urlopen):
        start = time.monotonic()
        notify("https://discord.example.com/webhook", "title", "description", COLOR_SUCCESS)
        elapsed = time.monotonic() - start
        release.set()
        flush()
    assert elapsed < 1.0


def test_flush_waits_for_a_slow_send_to_actually_finish():
    def slow_urlopen(req, timeout=None):
        time.sleep(0.2)

    with patch("urllib.request.urlopen", side_effect=slow_urlopen) as mock_urlopen:
        notify("https://discord.example.com/webhook", "title", "description", COLOR_SUCCESS)
        assert flush(timeout=5) is True
    assert mock_urlopen.call_count == 1


def test_flush_is_a_noop_when_nothing_was_ever_queued():
    # No webhook configured -> notify() never even touches the queue; flush() must not hang waiting on it
    assert flush(timeout=1) is True


def test_flush_times_out_and_logs_if_sends_are_still_pending(caplog):
    release = threading.Event()

    def slow_urlopen(req, timeout=None):
        release.wait(5)

    with caplog.at_level("WARNING", logger="docker_operator.notify"), \
         patch("urllib.request.urlopen", side_effect=slow_urlopen):
        notify("https://discord.example.com/webhook", "title", "description", COLOR_SUCCESS)
        assert flush(timeout=0.1) is False
        release.set()
        flush()
    assert any("timed out" in r.message for r in caplog.records)


def test_queue_full_drops_the_notification_and_logs_instead_of_blocking(caplog):
    import docker_operator.notify as notify_mod

    started = threading.Event()
    release = threading.Event()

    def blocking_urlopen(req, timeout=None):
        started.set()
        release.wait(5)

    with caplog.at_level("WARNING", logger="docker_operator.notify"), \
         patch("urllib.request.urlopen", side_effect=blocking_urlopen):
        # First one is picked up by the sender thread and blocks it there; wait for that pickup so the queue is confirmed empty before filling it, rather than racing the worker's own scheduling
        notify("https://discord.example.com/webhook", "first", "d", COLOR_SUCCESS)
        assert started.wait(5)
        # Fill the queue completely behind it
        for i in range(notify_mod._QUEUE_MAXSIZE):
            notify("https://discord.example.com/webhook", f"queued-{i}", "d", COLOR_SUCCESS)
        # One more must overflow: dropped and logged, never blocking this call waiting for room
        start = time.monotonic()
        notify("https://discord.example.com/webhook", "overflow", "d", COLOR_SUCCESS)
        elapsed = time.monotonic() - start
        release.set()
        flush()

    assert elapsed < 1.0
    assert any("dropping notification" in r.message and "overflow" in r.message for r in caplog.records)
