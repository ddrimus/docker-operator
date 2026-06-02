from __future__ import annotations
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from docker_operator import compose


def _completed(returncode=0, stdout="", stderr=""):
    return subprocess.CompletedProcess(args=["docker", "compose"], returncode=returncode,
                                        stdout=stdout, stderr=stderr)


def test_up_calls_pull_then_up_when_pull_true():
    with patch("subprocess.run", return_value=_completed()) as mock_run:
        compose.up(Path("c.yaml"), Path(".env"), "proj", Path("."), pull=True, timeout=30)
    calls = [c.args[0] for c in mock_run.call_args_list]
    assert any("pull" in c for c in calls)
    assert any("up" in c and "-d" in c for c in calls)
    # pull must come before up
    pull_idx = next(i for i, c in enumerate(calls) if "pull" in c)
    up_idx = next(i for i, c in enumerate(calls) if "up" in c and "-d" in c)
    assert pull_idx < up_idx


def test_up_skips_pull_when_pull_false():
    with patch("subprocess.run", return_value=_completed()) as mock_run:
        compose.up(Path("c.yaml"), Path(".env"), "proj", Path("."), pull=False, timeout=30)
    calls = [c.args[0] for c in mock_run.call_args_list]
    assert len(calls) == 1
    assert "up" in calls[0]
    assert "--remove-orphans" in calls[0]


def test_resolve_config_returns_stdout():
    with patch("subprocess.run", return_value=_completed(stdout='{"networks":{}}')):
        result = compose.resolve_config(Path("c.yaml"), Path(".env"), "proj", Path("."), timeout=30)
    assert result == '{"networks":{}}'


def test_base_args_includes_project_flags():
    args = compose._base_args(Path("/x/compose.yaml"), Path("/x/.env"), "myproj", Path("/x"))
    assert args == ["docker", "compose", "-f", "/x/compose.yaml", "--env-file", "/x/.env",
                     "--project-directory", "/x", "-p", "myproj"]


def test_failure_raises_deploy_error_with_stderr_attached():
    with patch("subprocess.run", return_value=_completed(returncode=1, stderr="port 80 already allocated\n")):
        with pytest.raises(compose.DeployError) as exc_info:
            compose.up(Path("c.yaml"), Path(".env"), "proj", Path("."), pull=False, timeout=30)
    assert exc_info.value.stderr == "port 80 already allocated\n"


def test_timeout_raises_deploy_error():
    def _raise_timeout(*a, **k):
        raise subprocess.TimeoutExpired(cmd="docker compose up", timeout=30, output="", stderr="hung")
    with patch("subprocess.run", side_effect=_raise_timeout):
        with pytest.raises(compose.DeployError) as exc_info:
            compose.up(Path("c.yaml"), Path(".env"), "proj", Path("."), pull=False, timeout=30)
    assert "timed out" in str(exc_info.value)
    assert exc_info.value.stderr == "hung"


def test_timeout_with_no_stderr_captured_does_not_crash():
    def _raise_timeout(*a, **k):
        raise subprocess.TimeoutExpired(cmd="x", timeout=30, output=None, stderr=None)
    with patch("subprocess.run", side_effect=_raise_timeout):
        with pytest.raises(compose.DeployError) as exc_info:
            compose.up(Path("c.yaml"), Path(".env"), "proj", Path("."), pull=False, timeout=30)
    assert exc_info.value.stderr == ""
