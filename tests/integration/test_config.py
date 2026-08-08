from __future__ import annotations
from pathlib import Path

import pytest

from docker_operator.config import load_settings, local_repo_path

REQUIRED = {"WEBHOOK_SECRET": "s", "GIT_REPO_URL": "https://example.com/repo.git"}


def _set_env(monkeypatch, **overrides):
    monkeypatch.delenv("WEBHOOK_SECRET", raising=False)
    monkeypatch.delenv("GIT_REPO_URL", raising=False)
    env = {**REQUIRED, **overrides}
    for k, v in env.items():
        monkeypatch.setenv(k, v)


# --- local_repo_path detection ---

@pytest.mark.parametrize("url,expected", [
    ("/mnt/forgejo-repo", Path("/mnt/forgejo-repo")),
    ("./relative-repo", Path("./relative-repo")),
    ("../up-one/repo", Path("../up-one/repo")),
    ("file:///mnt/forgejo-repo", Path("/mnt/forgejo-repo")),
])
def test_local_repo_path_detects_local_forms(url, expected):
    assert local_repo_path(url) == expected


@pytest.mark.parametrize("url", [
    "https://forgejo.example.com/user/repo.git",
    "http://forgejo.example.com/user/repo.git",
    "ssh://git@forgejo.example.com/user/repo.git",
    "git@forgejo.example.com:user/repo.git",
])
def test_local_repo_path_returns_none_for_network_urls(url):
    assert local_repo_path(url) is None


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


def test_deploy_uid_gid_default_to_none(monkeypatch):
    _set_env(monkeypatch)
    monkeypatch.delenv("DEPLOY_UID", raising=False)
    monkeypatch.delenv("DEPLOY_GID", raising=False)
    s = load_settings()
    assert s.deploy_uid is None
    assert s.deploy_gid is None


def test_deploy_uid_gid_parsed_when_set(monkeypatch):
    _set_env(monkeypatch, DEPLOY_UID="1000", DEPLOY_GID="1001")
    s = load_settings()
    assert s.deploy_uid == 1000
    assert s.deploy_gid == 1001


def test_invalid_deploy_uid_exits_fatal(monkeypatch):
    _set_env(monkeypatch, DEPLOY_UID="not-a-number")
    with pytest.raises(SystemExit):
        load_settings()


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


# --- SOPS_AGE_KEY_FILE validation ---

def test_sops_key_file_must_exist_if_set(monkeypatch, tmp_path):
    _set_env(monkeypatch, SOPS_AGE_KEY_FILE=str(tmp_path / "nope.key"))
    with pytest.raises(SystemExit):
        load_settings()


def test_sops_key_file_none_when_unset(monkeypatch):
    _set_env(monkeypatch)
    monkeypatch.delenv("SOPS_AGE_KEY_FILE", raising=False)
    assert load_settings().sops_age_key_file is None


def test_sops_key_file_ok_when_present(monkeypatch, tmp_path):
    key = tmp_path / "age.key"
    key.write_text("AGE-SECRET-KEY-1FAKEKEYFORTESTONLY")
    _set_env(monkeypatch, SOPS_AGE_KEY_FILE=str(key))
    assert load_settings().sops_age_key_file == key


# --- GIT_REPO_URL local-path validation ---

def test_local_git_repo_url_must_exist(monkeypatch, tmp_path):
    _set_env(monkeypatch, GIT_REPO_URL=str(tmp_path / "does-not-exist"))
    with pytest.raises(SystemExit):
        load_settings()


def test_local_git_repo_url_ok_with_bare_repo_head(monkeypatch, tmp_path):
    # A bare repo has HEAD directly at its root.
    repo = tmp_path / "bare.git"
    repo.mkdir()
    (repo / "HEAD").write_text("ref: refs/heads/main\n")
    _set_env(monkeypatch, GIT_REPO_URL=str(repo))
    assert load_settings().git_repo_url == str(repo)


def test_local_git_repo_url_ok_with_dot_git_subdir(monkeypatch, tmp_path):
    # A normal (non-bare) checkout has HEAD under .git/.
    repo = tmp_path / "checkout"
    (repo / ".git").mkdir(parents=True)
    (repo / ".git" / "HEAD").write_text("ref: refs/heads/main\n")
    _set_env(monkeypatch, GIT_REPO_URL=str(repo))
    assert load_settings().git_repo_url == str(repo)


def test_network_git_repo_url_skips_local_validation_entirely(monkeypatch):
    # Must not try to stat a URL as a filesystem path.
    _set_env(monkeypatch, GIT_REPO_URL="https://forgejo.example.com/user/repo.git")
    assert load_settings().git_repo_url == "https://forgejo.example.com/user/repo.git"
