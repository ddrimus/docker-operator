from __future__ import annotations
import json
from unittest.mock import patch

from docker_operator.notify import COLOR_ERROR, COLOR_SUCCESS, notify


def test_noop_when_url_is_none():
    with patch("urllib.request.urlopen") as mock_urlopen:
        notify(None, "title", "description", COLOR_SUCCESS)
    mock_urlopen.assert_not_called()


def test_posts_embed_to_url():
    with patch("urllib.request.urlopen") as mock_urlopen:
        notify("https://discord.example.com/webhook", "some title", "some description", COLOR_ERROR)
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
    description = json.loads(mock_urlopen.call_args.args[0].data)["embeds"][0]["description"]
    assert len(description) <= 4096
    assert description.endswith("…")


def test_short_description_not_truncated():
    with patch("urllib.request.urlopen") as mock_urlopen:
        notify("https://discord.example.com/webhook", "title", "short", COLOR_SUCCESS)
    description = json.loads(mock_urlopen.call_args.args[0].data)["embeds"][0]["description"]
    assert description == "short"


def test_network_failure_is_swallowed_not_raised():
    with patch("urllib.request.urlopen", side_effect=OSError("connection refused")):
        # Must not raise
        notify("https://discord.example.com/webhook", "title", "description", COLOR_ERROR)
