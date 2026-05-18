from __future__ import annotations
from pathlib import Path

import pytest

from docker_operator.config import load_settings

REQUIRED = {"WEBHOOK_SECRET": "s", "GIT_REPO_URL": "https://example.com/repo.git"}


def _set_env(monkeypatch, **overrides):
    monkeypatch.delenv("WEBHOOK_SECRET", raising=False)
    monkeypatch.delenv("GIT_REPO_URL", raising=False)
    env = {**REQUIRED, **overrides}
    for k, v in env.items():
        monkeypatch.setenv(k, v)


# --- load_settings: required vars ---

def test_missing_webhook_secret_exits_fatal(monkeypatch):
    monkeypatch.delenv("WEBHOOK_SECRET", raising=False)
    monkeypatch.setenv("GIT_REPO_URL", "https://example.com/repo.git")
    with pytest.raises(SystemExit):
        load_settings()


def test_missing_git_repo_url_exits_fatal(monkeypatch):
    monkeypatch.setenv("WEBHOOK_SECRET", "s")
    monkeypatch.delenv("GIT_REPO_URL", raising=False)
    with pytest.raises(SystemExit):
        load_settings()


# --- load_settings: defaults ---

def test_defaults_when_optional_vars_unset(monkeypatch):
    _set_env(monkeypatch)
    for name in ("GIT_BRANCH", "DATA_DIR", "DEPLOY_DIR", "COMPOSE_SUBDIR", "LISTEN_PORT",
                 "PULL_IMAGES", "PRUNE_REMOVED_STACKS", "POLL_INTERVAL_SECONDS", "LOG_LEVEL"):
        monkeypatch.delenv(name, raising=False)
    s = load_settings()
    assert s.git_branch == "main"
    assert s.data_dir == Path("/data")
    # Falls back to data_dir/deploy when unset
    assert s.deploy_dir == Path("/data/deploy")
    assert s.compose_subdir == "compose"
    assert s.listen_port == 8080
    assert s.pull_images is True
    assert s.prune_removed_stacks is False
    assert s.poll_interval_seconds == 300
    assert s.log_level == "INFO"


def test_deploy_dir_independent_of_data_dir_when_set(monkeypatch):
    _set_env(monkeypatch, DATA_DIR="/data", DEPLOY_DIR="/deploy")
    s = load_settings()
    assert s.data_dir == Path("/data")
    assert s.deploy_dir == Path("/deploy")


@pytest.mark.parametrize("val,expected", [("1", True), ("true", True), ("YES", True), ("on", True),
                                          ("0", False), ("false", False), ("no", False), ("", False)])
def test_bool_env_parsing(monkeypatch, val, expected):
    _set_env(monkeypatch, PULL_IMAGES=val)
    assert load_settings().pull_images is expected


def test_invalid_int_env_exits_fatal(monkeypatch):
    _set_env(monkeypatch, POLL_INTERVAL_SECONDS="not-a-number")
    with pytest.raises(SystemExit):
        load_settings()


def test_derived_properties(monkeypatch):
    _set_env(monkeypatch, DATA_DIR="/data", COMPOSE_SUBDIR="stacks")
    s = load_settings()
    assert s.repo_dir == Path("/data/repo")
    assert s.state_file == Path("/data/state.json")
    assert s.compose_root == Path("/data/repo/stacks")
