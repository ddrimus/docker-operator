from __future__ import annotations
import json
from unittest.mock import patch

import pytest

from docker_operator import state as state_mod
from docker_operator.reconcile import reconcile
from conftest import add_stack, make_settings


def _no_networks_json(*a, **k) -> str:
    return json.dumps({"networks": {}})


# Track every `compose.up`/`compose.down` call, in order, so tests can assert both that and in what order something deployed
@pytest.fixture
def deployed(tmp_path):
    # (action, project_name) pairs
    calls: list[tuple[str, str]] = []

    def fake_up(compose_file, env_file, project, project_dir, *, pull, timeout):
        calls.append(("up", project))

    def fake_down(compose_file, env_file, project, project_dir, timeout):
        calls.append(("down", project))

    with patch("docker_operator.compose.up", side_effect=fake_up), \
         patch("docker_operator.compose.down", side_effect=fake_down), \
         patch("docker_operator.compose.resolve_config", side_effect=_no_networks_json):
        yield calls


def test_no_stacks_no_changes_is_a_clean_noop(git_repo, tmp_path, deployed):
    settings = make_settings(tmp_path, git_repo_url=str(git_repo))
    reconcile(settings)
    assert deployed == []


def test_new_stack_gets_staged_and_deployed(git_repo, tmp_path, deployed):
    add_stack(git_repo, "traefik")
    settings = make_settings(tmp_path, git_repo_url=str(git_repo))

    reconcile(settings)

    assert deployed == [("up", "traefik")]
    assert (settings.deploy_dir / "traefik" / "compose.yaml").is_file()
    assert (settings.deploy_dir / "traefik" / ".env").is_file()
    st = state_mod.load(settings.state_file)
    assert "traefik" in st["stacks"]


def test_unchanged_stack_is_not_redeployed_on_second_pass(git_repo, tmp_path, deployed):
    add_stack(git_repo, "traefik")
    settings = make_settings(tmp_path, git_repo_url=str(git_repo))
    reconcile(settings)
    deployed.clear()

    reconcile(settings)

    assert deployed == []


def test_changed_compose_yaml_triggers_redeploy(git_repo, tmp_path, deployed):
    add_stack(git_repo, "traefik", compose="services:\n  traefik:\n    image: traefik:v3.0\n")
    settings = make_settings(tmp_path, git_repo_url=str(git_repo))
    reconcile(settings)
    deployed.clear()

    add_stack(git_repo, "traefik", compose="services:\n  traefik:\n    image: traefik:v3.7\n")
    reconcile(settings)

    assert deployed == [("up", "traefik")]


def test_canonical_files_only_promoted_after_successful_up(git_repo, tmp_path):
    add_stack(git_repo, "traefik")
    settings = make_settings(tmp_path, git_repo_url=str(git_repo))

    with patch("docker_operator.compose.resolve_config", side_effect=_no_networks_json), \
         patch("docker_operator.compose.up", side_effect=RuntimeError("boom")):
        reconcile(settings)

    # Nothing promoted, nothing recorded: next reconcile retries cleanly.
    assert not (settings.deploy_dir / "traefik" / "compose.yaml").exists()
    st = state_mod.load(settings.state_file)
    assert "traefik" not in st["stacks"]
    # Staged files are cleaned up too, not left as litter.
    assert not (settings.deploy_dir / "traefik" / "compose.yaml.new").exists()


def test_validation_failure_does_not_block_other_stacks(git_repo, tmp_path):
    add_stack(git_repo, "good")
    add_stack(git_repo, "bad")
    settings = make_settings(tmp_path, git_repo_url=str(git_repo))

    def resolve_side_effect(compose_file, env_file, project, project_dir, timeout):
        if project == "bad":
            raise RuntimeError("invalid compose file")
        return _no_networks_json()

    calls = []
    with patch("docker_operator.compose.resolve_config", side_effect=resolve_side_effect), \
         patch("docker_operator.compose.up", side_effect=lambda *a, **k: calls.append(k)):
        reconcile(settings)

    st = state_mod.load(settings.state_file)
    assert "good" in st["stacks"]
    assert "bad" not in st["stacks"]


def test_missing_age_key_with_secrets_file_fails_that_stack_only(git_repo, tmp_path, deployed):
    add_stack(git_repo, "needs-secrets", env_secrets_encrypted=b"doesnt-matter-validation-fails-first")
    add_stack(git_repo, "plain")
    settings = make_settings(tmp_path, git_repo_url=str(git_repo), sops_age_key_file=None)

    reconcile(settings)

    assert ("up", "plain") in deployed
    assert ("up", "needs-secrets") not in deployed
    st = state_mod.load(settings.state_file)
    assert "plain" in st["stacks"]
    assert "needs-secrets" not in st["stacks"]


def test_reconcile_lock_is_released_after_call_allows_second_call(git_repo, tmp_path, deployed):
    add_stack(git_repo, "traefik")
    settings = make_settings(tmp_path, git_repo_url=str(git_repo))
    # If the flock leaked, this would hang forever
    reconcile(settings)
    reconcile(settings)
    # Reaching here at all proves the lock was released
    assert True


def test_self_heals_after_remote_becomes_unreachable_then_recovers(git_repo, tmp_path, deployed):
    add_stack(git_repo, "traefik")
    settings = make_settings(tmp_path, git_repo_url=str(git_repo))
    reconcile(settings)
    deployed.clear()

    moved_away = git_repo.parent / "moved-away"
    git_repo.rename(moved_away)
    try:
        # forgejo "down": must not touch anything already running
        reconcile(settings)
        assert deployed == []
    finally:
        moved_away.rename(git_repo)

    add_stack(git_repo, "forgejo")
    # forgejo "back up": picks up what changed while it was down
    reconcile(settings)
    assert deployed == [("up", "forgejo")]
