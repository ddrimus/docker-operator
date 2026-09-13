from __future__ import annotations
from unittest.mock import patch

import apprise

from docker_operator.notify import TYPE_ERROR, TYPE_SUCCESS, notify


def test_noop_when_urls_is_none():
    with patch("apprise.Apprise") as mock_apprise:
        notify(None, "title", "body", TYPE_SUCCESS)
    mock_apprise.assert_not_called()


def test_sends_titled_markdown_message_to_every_configured_service():
    urls = "json://notify.example.com/hook, ntfys://ntfy.example.com/deploys"
    with patch("apprise.Apprise") as mock_apprise:
        notify(urls, "some title", "some **body**", TYPE_ERROR)
    apobj = mock_apprise.return_value
    apobj.add.assert_called_once_with(urls)
    apobj.notify.assert_called_once_with(body="some **body**", title="some title", notify_type=TYPE_ERROR,
                                         body_format=apprise.NotifyFormat.MARKDOWN)


def test_identifies_itself_as_docker_operator():
    with patch("apprise.Apprise") as mock_apprise:
        notify("json://notify.example.com/hook", "title", "body", TYPE_SUCCESS)
    assert mock_apprise.call_args.kwargs["asset"].app_id == "Docker Operator"


def test_types_map_to_apprise_notify_types():
    assert TYPE_SUCCESS == apprise.NotifyType.SUCCESS
    assert TYPE_ERROR == apprise.NotifyType.FAILURE


def test_invalid_url_is_logged_and_nothing_is_sent(caplog):
    with caplog.at_level("WARNING", logger="docker_operator.notify"), \
         patch("apprise.Apprise.notify") as mock_send:
        # Real Apprise URL parsing here, only delivery is mocked
        notify("definitely not a url", "title", "body", TYPE_SUCCESS)
    mock_send.assert_not_called()
    assert any("not valid Apprise URLs" in r.message for r in caplog.records)


def test_delivery_failure_is_logged_not_raised(caplog):
    with caplog.at_level("WARNING", logger="docker_operator.notify"), \
         patch("apprise.Apprise") as mock_apprise:
        mock_apprise.return_value.notify.return_value = False
        notify("json://notify.example.com/hook", "title", "body", TYPE_ERROR)
    assert any("failed to send notification" in r.message for r in caplog.records)


def test_unexpected_error_is_swallowed_not_raised():
    with patch("apprise.Apprise", side_effect=OSError("connection refused")):
        # Must not raise
        notify("json://notify.example.com/hook", "title", "body", TYPE_ERROR)
