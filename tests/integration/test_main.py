from __future__ import annotations
import sys
from unittest.mock import patch

from docker_operator.__main__ import main


def _argv(monkeypatch, *args):
    monkeypatch.setattr(sys, "argv", ["docker-operator", *args])


def _env(monkeypatch, tmp_path):
    monkeypatch.setenv("WEBHOOK_SECRET", "s")
    monkeypatch.setenv("GIT_REPO_URL", "https://example.com/x.git")
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("DEPLOY_DIR", str(tmp_path / "deploy"))


def test_runs_a_single_reconcile_pass_with_no_force(monkeypatch, tmp_path):
    _argv(monkeypatch)
    _env(monkeypatch, tmp_path)

    with patch("docker_operator.__main__.reconcile") as mock_reconcile:
        main()

    mock_reconcile.assert_called_once()
    _, kwargs = mock_reconcile.call_args
    assert kwargs["force"] is None


def test_force_passes_the_stack_set_through(monkeypatch, tmp_path):
    _argv(monkeypatch, "--force", "traefik", "--force", "forgejo")
    _env(monkeypatch, tmp_path)

    with patch("docker_operator.__main__.reconcile") as mock_reconcile:
        main()

    _, kwargs = mock_reconcile.call_args
    assert kwargs["force"] == {"traefik", "forgejo"}
