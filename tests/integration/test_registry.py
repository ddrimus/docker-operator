from __future__ import annotations
import subprocess
from pathlib import Path
from unittest.mock import patch

from conftest import make_settings

from docker_operator.registry import login


def test_noop_when_registry_host_unset(tmp_path: Path):
    settings = make_settings(tmp_path, git_repo_url=str(tmp_path))
    with patch("subprocess.run") as mock_run:
        login(settings)
    mock_run.assert_not_called()


def test_logs_in_with_password_over_stdin(tmp_path: Path):
    settings = make_settings(
        tmp_path, git_repo_url=str(tmp_path),
        registry_host="registry.example.com", registry_username="user", registry_password="s3cr3t",
    )
    with patch("subprocess.run") as mock_run:
        mock_run.return_value.returncode = 0
        mock_run.return_value.stderr = ""
        login(settings)
    args, kwargs = mock_run.call_args
    assert args[0] == ["docker", "login", "registry.example.com", "-u", "user", "--password-stdin"]
    assert kwargs["input"] == "s3cr3t"


def test_failure_is_logged_not_raised(tmp_path: Path):
    settings = make_settings(
        tmp_path, git_repo_url=str(tmp_path),
        registry_host="registry.example.com", registry_username="user", registry_password="wrong",
    )
    with patch("subprocess.run") as mock_run:
        mock_run.return_value.returncode = 1
        mock_run.return_value.stderr = "unauthorized"
        # Must not raise
        login(settings)


def test_timeout_is_logged_not_raised(tmp_path: Path):
    settings = make_settings(
        tmp_path, git_repo_url=str(tmp_path),
        registry_host="registry.example.com", registry_username="user", registry_password="s3cr3t",
    )
    with patch("subprocess.run", side_effect=subprocess.TimeoutExpired(cmd="docker login", timeout=30)):
        # Must not raise
        login(settings)


def test_missing_docker_binary_is_logged_not_raised(tmp_path: Path):
    settings = make_settings(
        tmp_path, git_repo_url=str(tmp_path),
        registry_host="registry.example.com", registry_username="user", registry_password="s3cr3t",
    )
    with patch("subprocess.run", side_effect=FileNotFoundError("docker")):
        # Must not raise: registry login failing must never take the whole operator down at startup
        login(settings)
