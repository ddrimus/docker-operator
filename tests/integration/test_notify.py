from __future__ import annotations
from unittest.mock import patch

from docker_operator.notify import notify


def test_noop_when_urls_is_none():
    with patch("apprise.Apprise") as mock_apprise:
        notify(None, "hello")
    mock_apprise.assert_not_called()


def test_sends_text_to_every_configured_service():
    urls = "json://notify.example.com/hook, ntfys://ntfy.example.com/deploys"
    with patch("apprise.Apprise") as mock_apprise:
        notify(urls, "docker-operator: something failed")
    apobj = mock_apprise.return_value
    apobj.add.assert_called_once_with(urls)
    apobj.notify.assert_called_once_with(body="docker-operator: something failed")


def test_invalid_url_is_logged_and_nothing_is_sent(caplog):
    with caplog.at_level("WARNING", logger="docker_operator.notify"), \
         patch("apprise.Apprise.notify") as mock_send:
        # Real Apprise URL parsing here, only delivery is mocked
        notify("definitely not a url", "hello")
    mock_send.assert_not_called()
    assert any("not valid Apprise URLs" in r.message for r in caplog.records)


def test_delivery_failure_is_logged_not_raised(caplog):
    with caplog.at_level("WARNING", logger="docker_operator.notify"), \
         patch("apprise.Apprise") as mock_apprise:
        mock_apprise.return_value.notify.return_value = False
        notify("json://notify.example.com/hook", "hello")
    assert any("failed to send notification" in r.message for r in caplog.records)


def test_unexpected_error_is_swallowed_not_raised():
    with patch("apprise.Apprise", side_effect=OSError("connection refused")):
        # Must not raise
        notify("json://notify.example.com/hook", "hello")
