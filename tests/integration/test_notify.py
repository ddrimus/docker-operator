from __future__ import annotations
import threading
import time
from unittest.mock import patch

import apprise

from docker_operator.notify import TYPE_ERROR, TYPE_SUCCESS, flush, notify


def test_noop_when_urls_is_none():
    with patch("apprise.Apprise") as mock_apprise:
        notify(None, "title", "body", TYPE_SUCCESS)
        flush()
    mock_apprise.assert_not_called()


def test_sends_titled_markdown_message_to_every_configured_service():
    urls = "json://notify.example.com/hook, ntfys://ntfy.example.com/deploys"
    with patch("apprise.Apprise") as mock_apprise:
        notify(urls, "some title", "some **body**", TYPE_ERROR)
        flush()
    apobj = mock_apprise.return_value
    apobj.add.assert_called_once_with(urls)
    apobj.notify.assert_called_once_with(body="some **body**", title="some title", notify_type=TYPE_ERROR,
                                         body_format=apprise.NotifyFormat.MARKDOWN)


def test_sender_name_is_left_to_the_title():
    with patch("apprise.Apprise") as mock_apprise:
        notify("json://notify.example.com/hook", "title", "body", TYPE_SUCCESS)
        flush()
    # Empty, so services that show an author line don't repeat the "Docker Operator" every title already has
    assert mock_apprise.call_args.kwargs["asset"].app_id == ""


def test_types_map_to_apprise_notify_types():
    assert TYPE_SUCCESS == apprise.NotifyType.SUCCESS
    assert TYPE_ERROR == apprise.NotifyType.FAILURE


def test_invalid_url_is_logged_and_nothing_is_sent(caplog):
    with caplog.at_level("WARNING", logger="docker_operator.notify"), \
         patch("apprise.Apprise.notify") as mock_send:
        # Real Apprise URL parsing here, only delivery is mocked
        notify("definitely not a url", "title", "body", TYPE_SUCCESS)
        flush()
    mock_send.assert_not_called()
    assert any("not valid Apprise URLs" in r.message for r in caplog.records)


def test_delivery_failure_is_logged_not_raised(caplog):
    with caplog.at_level("WARNING", logger="docker_operator.notify"), \
         patch("apprise.Apprise") as mock_apprise:
        mock_apprise.return_value.notify.return_value = False
        notify("json://notify.example.com/hook", "title", "body", TYPE_ERROR)
        flush()
    assert any("failed to send notification" in r.message for r in caplog.records)


def test_unexpected_error_is_swallowed_not_raised(caplog):
    with caplog.at_level("WARNING", logger="docker_operator.notify"), \
         patch("apprise.Apprise", side_effect=OSError("connection refused")):
        # Must not raise
        notify("json://notify.example.com/hook", "title", "body", TYPE_ERROR)
        flush()
    assert any("failed to send notification" in r.message for r in caplog.records)


# --- notify() must return immediately, the actual delivery happens on a background sender thread (see flush()); these prove that by making the mocked delivery itself slow and asserting notify() doesn't wait around for it ---

def test_notify_returns_before_the_delivery_completes():
    release = threading.Event()

    with patch("apprise.Apprise") as mock_apprise:
        mock_apprise.return_value.notify.side_effect = lambda **kw: release.wait(5)
        start = time.monotonic()
        notify("json://notify.example.com/hook", "title", "body", TYPE_SUCCESS)
        elapsed = time.monotonic() - start
        release.set()
        flush()
    assert elapsed < 1.0


def test_flush_waits_for_a_slow_send_to_actually_finish():
    with patch("apprise.Apprise") as mock_apprise:
        mock_apprise.return_value.notify.side_effect = lambda **kw: time.sleep(0.2) or True
        notify("json://notify.example.com/hook", "title", "body", TYPE_SUCCESS)
        assert flush(timeout=5) is True
    assert mock_apprise.return_value.notify.call_count == 1


def test_flush_is_a_noop_when_nothing_was_ever_queued():
    # No NOTIFY_URLS configured -> notify() never even touches the queue; flush() must not hang waiting on it
    assert flush(timeout=1) is True


def test_flush_times_out_and_logs_if_sends_are_still_pending(caplog):
    release = threading.Event()

    with caplog.at_level("WARNING", logger="docker_operator.notify"), \
         patch("apprise.Apprise") as mock_apprise:
        mock_apprise.return_value.notify.side_effect = lambda **kw: release.wait(5)
        notify("json://notify.example.com/hook", "title", "body", TYPE_SUCCESS)
        assert flush(timeout=0.1) is False
        release.set()
        flush()
    assert any("timed out" in r.message for r in caplog.records)


def test_queue_full_drops_the_notification_and_logs_instead_of_blocking(caplog):
    import docker_operator.notify as notify_mod

    started = threading.Event()
    release = threading.Event()

    def blocking_send(**kw):
        started.set()
        return release.wait(5)

    with caplog.at_level("WARNING", logger="docker_operator.notify"), \
         patch("apprise.Apprise") as mock_apprise:
        mock_apprise.return_value.notify.side_effect = blocking_send
        # First one is picked up by the sender thread and blocks it there; wait for that pickup so the queue is confirmed empty before filling it, rather than racing the worker's own scheduling
        notify("json://notify.example.com/hook", "first", "b", TYPE_SUCCESS)
        assert started.wait(5)
        # Fill the queue completely behind it
        for i in range(notify_mod._QUEUE_MAXSIZE):
            notify("json://notify.example.com/hook", f"queued-{i}", "b", TYPE_SUCCESS)
        # One more must overflow: dropped and logged, never blocking this call waiting for room
        start = time.monotonic()
        notify("json://notify.example.com/hook", "overflow", "b", TYPE_SUCCESS)
        elapsed = time.monotonic() - start
        release.set()
        flush()

    assert elapsed < 1.0
    assert any("dropping notification" in r.message and "overflow" in r.message for r in caplog.records)
