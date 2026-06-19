from __future__ import annotations
import json
from unittest.mock import patch

from docker_operator.notify import notify


def test_noop_when_url_is_none():
    with patch("urllib.request.urlopen") as mock_urlopen:
        notify(None, "hello")
    mock_urlopen.assert_not_called()


def test_posts_json_content_to_url():
    with patch("urllib.request.urlopen") as mock_urlopen:
        notify("https://discord.example.com/webhook", "docker-operator: something failed")
    assert mock_urlopen.call_count == 1
    req = mock_urlopen.call_args.args[0]
    assert req.full_url == "https://discord.example.com/webhook"
    body = json.loads(req.data)
    assert body == {"content": "docker-operator: something failed"}
    assert req.headers.get("Content-type") == "application/json"


def test_long_message_truncated_under_discord_limit():
    long_text = "x" * 5000
    with patch("urllib.request.urlopen") as mock_urlopen:
        notify("https://discord.example.com/webhook", long_text)
    body = json.loads(mock_urlopen.call_args.args[0].data)
    assert len(body["content"]) <= 2000
    assert body["content"].endswith("…")


def test_short_message_not_truncated():
    with patch("urllib.request.urlopen") as mock_urlopen:
        notify("https://discord.example.com/webhook", "short")
    body = json.loads(mock_urlopen.call_args.args[0].data)
    assert body["content"] == "short"


def test_network_failure_is_swallowed_not_raised():
    with patch("urllib.request.urlopen", side_effect=OSError("connection refused")):
        # Must not raise
        notify("https://discord.example.com/webhook", "hello")
