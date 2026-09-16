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


def test_down_calls_docker_compose_down():
    with patch("subprocess.run", return_value=_completed()) as mock_run:
        compose.down(Path("c.yaml"), Path(".env"), "proj", Path("."), timeout=30)
    args = mock_run.call_args.args[0]
    assert args[:2] == ["docker", "compose"]
    assert "down" in args


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


@pytest.mark.parametrize("stderr", [
    "Error response from daemon: Head \"https://reg/v2/x/y/manifests/v1\": unauthorized",
    "Error response from daemon: unknown: 404 page not found",
])
def test_up_retries_login_and_pull_once_on_auth_looking_pull_failure(stderr):
    results = iter([_completed(returncode=1, stderr=stderr), _completed(), _completed()])
    with patch("subprocess.run", side_effect=lambda *a, **k: next(results)) as mock_run:
        login_calls = []
        compose.up(Path("c.yaml"), Path(".env"), "proj", Path("."), pull=True, timeout=30,
                    retry_login=lambda: login_calls.append(1))
    assert login_calls == [1]
    calls = [c.args[0] for c in mock_run.call_args_list]
    assert sum(1 for c in calls if "pull" in c) == 2
    assert sum(1 for c in calls if "up" in c and "-d" in c) == 1


def test_up_does_not_retry_login_when_pull_failure_is_not_auth_looking():
    with patch("subprocess.run", return_value=_completed(returncode=1, stderr="port 80 already allocated\n")):
        login_calls = []
        with pytest.raises(compose.DeployError):
            compose.up(Path("c.yaml"), Path(".env"), "proj", Path("."), pull=True, timeout=30,
                        retry_login=lambda: login_calls.append(1))
    assert login_calls == []


def test_up_raises_if_retry_login_pull_fails_again():
    with patch("subprocess.run", return_value=_completed(returncode=1, stderr="unauthorized")):
        login_calls = []
        with pytest.raises(compose.DeployError):
            compose.up(Path("c.yaml"), Path(".env"), "proj", Path("."), pull=True, timeout=30,
                        retry_login=lambda: login_calls.append(1))
    assert login_calls == [1]


def test_up_without_retry_login_callback_raises_immediately_on_auth_failure():
    with patch("subprocess.run", return_value=_completed(returncode=1, stderr="unauthorized")) as mock_run:
        with pytest.raises(compose.DeployError):
            compose.up(Path("c.yaml"), Path(".env"), "proj", Path("."), pull=True, timeout=30)
    calls = [c.args[0] for c in mock_run.call_args_list]
    assert sum(1 for c in calls if "pull" in c) == 1
