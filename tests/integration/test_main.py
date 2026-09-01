from __future__ import annotations
import stat
import sys
import threading
import time
from unittest.mock import patch

import pytest

from docker_operator.__main__ import main, _healthcheck
from docker_operator import server as server_mod
from conftest import make_settings


def _argv(monkeypatch, *args):
    monkeypatch.setattr(sys, "argv", ["docker-operator", *args])


def test_force_without_once_exits_nonzero(monkeypatch):
    _argv(monkeypatch, "--force", "traefik")
    with pytest.raises(SystemExit) as exc_info:
        main()
    assert exc_info.value.code != 0


def test_force_all_without_once_also_rejected(monkeypatch):
    _argv(monkeypatch, "--force", "all")
    with pytest.raises(SystemExit):
        main()


def test_once_without_force_calls_reconcile_with_no_force(monkeypatch, tmp_path):
    _argv(monkeypatch, "--once")
    monkeypatch.setenv("WEBHOOK_SECRET", "s")
    monkeypatch.setenv("GIT_REPO_URL", "https://example.com/x.git")
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("DEPLOY_DIR", str(tmp_path / "deploy"))
    monkeypatch.setenv("LOG_DIR", str(tmp_path / "logs"))

    with patch("docker_operator.__main__.reconcile") as mock_reconcile:
        main()

    mock_reconcile.assert_called_once()
    _, kwargs = mock_reconcile.call_args
    assert kwargs["force"] is None


def test_once_with_force_passes_the_stack_set_through(monkeypatch, tmp_path):
    _argv(monkeypatch, "--once", "--force", "traefik", "--force", "forgejo")
    monkeypatch.setenv("WEBHOOK_SECRET", "s")
    monkeypatch.setenv("GIT_REPO_URL", "https://example.com/x.git")
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("DEPLOY_DIR", str(tmp_path / "deploy"))
    monkeypatch.setenv("LOG_DIR", str(tmp_path / "logs"))

    with patch("docker_operator.__main__.reconcile") as mock_reconcile:
        main()

    _, kwargs = mock_reconcile.call_args
    assert kwargs["force"] == {"traefik", "forgejo"}


def test_default_mode_starts_the_server(monkeypatch, tmp_path):
    # No flags at all
    _argv(monkeypatch)
    monkeypatch.setenv("WEBHOOK_SECRET", "s")
    monkeypatch.setenv("GIT_REPO_URL", "https://example.com/x.git")
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("DEPLOY_DIR", str(tmp_path / "deploy"))
    monkeypatch.setenv("LOG_DIR", str(tmp_path / "logs"))

    with patch("docker_operator.__main__.run") as mock_run:
        main()

    mock_run.assert_called_once()


def test_data_deploy_and_log_dirs_created_with_0700(monkeypatch, tmp_path):
    _argv(monkeypatch, "--once")
    data_dir = tmp_path / "fresh-data"
    deploy_dir = tmp_path / "fresh-deploy"
    log_dir = tmp_path / "fresh-logs"
    monkeypatch.setenv("WEBHOOK_SECRET", "s")
    monkeypatch.setenv("GIT_REPO_URL", "https://example.com/x.git")
    monkeypatch.setenv("DATA_DIR", str(data_dir))
    monkeypatch.setenv("DEPLOY_DIR", str(deploy_dir))
    monkeypatch.setenv("LOG_DIR", str(log_dir))

    with patch("docker_operator.__main__.reconcile"):
        main()

    assert stat.S_IMODE(data_dir.stat().st_mode) == 0o700
    assert stat.S_IMODE(deploy_dir.stat().st_mode) == 0o700
    assert stat.S_IMODE(log_dir.stat().st_mode) == 0o700
    assert (log_dir / "docker-operator.log").is_file()


def test_preexisting_loose_permissions_get_tightened(monkeypatch, tmp_path):
    # Docker auto-creating a missing bind-mount source commonly leaves it at 0755; mkdir's mode= alone won't tighten it after the fact
    data_dir = tmp_path / "data"
    data_dir.mkdir(mode=0o755)
    deploy_dir = tmp_path / "deploy"
    deploy_dir.mkdir(mode=0o755)
    log_dir = tmp_path / "logs"
    log_dir.mkdir(mode=0o755)

    _argv(monkeypatch, "--once")
    monkeypatch.setenv("WEBHOOK_SECRET", "s")
    monkeypatch.setenv("GIT_REPO_URL", "https://example.com/x.git")
    monkeypatch.setenv("DATA_DIR", str(data_dir))
    monkeypatch.setenv("DEPLOY_DIR", str(deploy_dir))
    monkeypatch.setenv("LOG_DIR", str(log_dir))

    with patch("docker_operator.__main__.reconcile"):
        main()

    assert stat.S_IMODE(data_dir.stat().st_mode) == 0o700
    assert stat.S_IMODE(deploy_dir.stat().st_mode) == 0o700
    assert stat.S_IMODE(log_dir.stat().st_mode) == 0o700


# --- --healthcheck ---

def test_healthcheck_exits_1_when_nothing_listening(monkeypatch):
    monkeypatch.setenv("LISTEN_PORT", "18099")
    with pytest.raises(SystemExit) as exc_info:
        _healthcheck()
    assert exc_info.value.code == 1


def test_healthcheck_succeeds_against_a_real_running_server(monkeypatch, tmp_path):
    settings = make_settings(tmp_path, git_repo_url="https://example.com/x.git", listen_port=0)
    httpd = server_mod._Server((settings.listen_host, 0), server_mod.make_handler(settings))
    port = httpd.server_address[1]
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    try:
        time.sleep(0.1)
        monkeypatch.setenv("LISTEN_PORT", str(port))
        # Must not raise/exit
        _healthcheck()
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_healthcheck_does_not_require_full_settings(monkeypatch):
    # No WEBHOOK_SECRET/GIT_REPO_URL set at all; an unrelated config problem shouldn't make Docker think the listener itself is down
    monkeypatch.delenv("WEBHOOK_SECRET", raising=False)
    monkeypatch.delenv("GIT_REPO_URL", raising=False)
    monkeypatch.setenv("LISTEN_PORT", "18098")
    with pytest.raises(SystemExit) as exc_info:
        _healthcheck()
    # Fails because nothing's listening, not a config error
    assert exc_info.value.code == 1


def test_healthcheck_flag_short_circuits_before_settings_validation(monkeypatch):
    # --healthcheck must not blow up even with a totally invalid config.
    _argv(monkeypatch, "--healthcheck")
    monkeypatch.delenv("WEBHOOK_SECRET", raising=False)
    monkeypatch.delenv("GIT_REPO_URL", raising=False)
    monkeypatch.setenv("LISTEN_PORT", "18097")
    with pytest.raises(SystemExit) as exc_info:
        main()
    assert exc_info.value.code == 1


def test_main_healthcheck_flag_returns_cleanly_on_success(monkeypatch, tmp_path):
    settings = make_settings(tmp_path, git_repo_url="https://example.com/x.git", listen_port=0)
    httpd = server_mod._Server((settings.listen_host, 0), server_mod.make_handler(settings))
    port = httpd.server_address[1]
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    try:
        time.sleep(0.1)
        _argv(monkeypatch, "--healthcheck")
        monkeypatch.setenv("LISTEN_PORT", str(port))
        # Must return normally, no SystemExit
        main()
    finally:
        httpd.shutdown()
        httpd.server_close()
