from __future__ import annotations
import json
from unittest.mock import patch

import pytest

from docker_operator import state as state_mod
from docker_operator.notify import COLOR_ERROR, COLOR_SUCCESS, COLOR_WARNING
from docker_operator.reconcile import reconcile, validate
from conftest import add_stack, make_settings


def _no_networks_json(*a, **k) -> str:
    return json.dumps({"networks": {}})


# Track every `compose.up`/`compose.down` call, in order, so tests can assert both that and in what order something deployed
@pytest.fixture
def deployed(tmp_path):
    # (action, project_name) pairs
    calls: list[tuple[str, str]] = []

    def fake_up(compose_file, env_file, project, project_dir, *, pull, timeout, retry_login=None):
        calls.append(("up", project))

    def fake_down(compose_file, env_file, project, project_dir, timeout):
        calls.append(("down", project))

    with patch("docker_operator.compose.up", side_effect=fake_up), \
         patch("docker_operator.compose.down", side_effect=fake_down), \
         patch("docker_operator.compose.resolve_config", side_effect=_no_networks_json):
        yield calls


# Track every notify() call as (title, description, color), regardless of settings.notify_webhook_url
@pytest.fixture
def notified():
    calls: list[tuple[str, str, int]] = []

    def fake_notify(url, title, description, color):
        calls.append((title, description, color))

    with patch("docker_operator.reconcile.notify", side_effect=fake_notify):
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


def test_reconcile_summary_names_the_changed_stacks(git_repo, tmp_path, deployed, caplog):
    add_stack(git_repo, "traefik")
    add_stack(git_repo, "forgejo")
    settings = make_settings(tmp_path, git_repo_url=str(git_repo))

    with caplog.at_level("INFO", logger="docker_operator.reconcile"):
        reconcile(settings)

    summary = next(r.message for r in caplog.records if r.message.startswith("reconcile: "))
    assert "forgejo" in summary
    assert "traefik" in summary


def test_reconcile_summary_names_removed_stacks(git_repo, tmp_path, deployed, caplog):
    import subprocess, shutil
    add_stack(git_repo, "temp")
    settings = make_settings(tmp_path, git_repo_url=str(git_repo), prune_removed_stacks=True)
    reconcile(settings)

    shutil.rmtree(git_repo / "compose" / "temp")
    subprocess.run(["git", "add", "-A"], cwd=git_repo, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "remove temp"], cwd=git_repo, check=True, capture_output=True)

    with caplog.at_level("INFO", logger="docker_operator.reconcile"):
        reconcile(settings)

    summary = next(r.message for r in caplog.records if r.message.startswith("reconcile: "))
    assert "temp" in summary


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

    # Nothing promoted, nothing recorded: next reconcile retries cleanly
    assert not (settings.deploy_dir / "traefik" / "compose.yaml").exists()
    st = state_mod.load(settings.state_file)
    assert "traefik" not in st["stacks"]
    # Staged files are cleaned up too, not left as litter
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


def test_network_owner_deployed_before_dependent(git_repo, tmp_path):
    add_stack(git_repo, "traefik")
    add_stack(git_repo, "forgejo")
    settings = make_settings(tmp_path, git_repo_url=str(git_repo))

    def resolve_side_effect(compose_file, env_file, project, project_dir, timeout):
        if project == "traefik":
            return json.dumps({"networks": {"proxy": {"name": "proxy"}}})
        return json.dumps({"networks": {"proxy": {"name": "proxy", "external": True}}})

    calls: list[str] = []

    def fake_up(compose_file, env_file, project, project_dir, *, pull, timeout, retry_login=None):
        calls.append(project)

    with patch("docker_operator.compose.resolve_config", side_effect=resolve_side_effect), \
         patch("docker_operator.compose.up", side_effect=fake_up):
        reconcile(settings)

    assert calls.index("traefik") < calls.index("forgejo")


def test_deploy_priority_deploys_named_stacks_first(git_repo, tmp_path, deployed):
    # Alphabetically "forgejo" < "nginx" < "traefik"; without priority they'd deploy in that order
    add_stack(git_repo, "forgejo")
    add_stack(git_repo, "nginx")
    add_stack(git_repo, "traefik")
    settings = make_settings(tmp_path, git_repo_url=str(git_repo), deploy_priority=("nginx", "forgejo"))

    reconcile(settings)

    order = [project for action, project in deployed]
    assert order == ["nginx", "forgejo", "traefik"]


def test_deploy_priority_never_overrides_a_real_network_dependency(git_repo, tmp_path):
    # "traefik" owns network "proxy" which "forgejo" needs externally: forgejo must still wait for traefik despite being prioritized over it
    add_stack(git_repo, "traefik")
    add_stack(git_repo, "forgejo")
    settings = make_settings(tmp_path, git_repo_url=str(git_repo), deploy_priority=("forgejo", "traefik"))

    def resolve_side_effect(compose_file, env_file, project, project_dir, timeout):
        if project == "traefik":
            return json.dumps({"networks": {"proxy": {"name": "proxy"}}})
        return json.dumps({"networks": {"proxy": {"name": "proxy", "external": True}}})

    calls: list[str] = []

    def fake_up(compose_file, env_file, project, project_dir, *, pull, timeout, retry_login=None):
        calls.append(project)

    with patch("docker_operator.compose.resolve_config", side_effect=resolve_side_effect), \
         patch("docker_operator.compose.up", side_effect=fake_up):
        reconcile(settings)

    assert calls.index("traefik") < calls.index("forgejo")


def test_removed_stack_left_alone_when_prune_disabled(git_repo, tmp_path, deployed):
    add_stack(git_repo, "temp")
    settings = make_settings(tmp_path, git_repo_url=str(git_repo), prune_removed_stacks=False)
    reconcile(settings)
    deployed.clear()

    import subprocess
    (git_repo / "compose" / "temp").rename(git_repo / "compose" / "temp.bak")
    import shutil
    shutil.rmtree(git_repo / "compose" / "temp.bak")
    subprocess.run(["git", "add", "-A"], cwd=git_repo, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "remove temp"], cwd=git_repo, check=True, capture_output=True)

    reconcile(settings)

    assert ("down", "temp") not in deployed
    st = state_mod.load(settings.state_file)
    # Still tracked as "should be running"
    assert "temp" in st["stacks"]


def test_removed_stack_torn_down_when_prune_enabled(git_repo, tmp_path, deployed):
    import subprocess, shutil
    add_stack(git_repo, "temp")
    settings = make_settings(tmp_path, git_repo_url=str(git_repo), prune_removed_stacks=True)
    reconcile(settings)
    deployed.clear()

    shutil.rmtree(git_repo / "compose" / "temp")
    subprocess.run(["git", "add", "-A"], cwd=git_repo, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "remove temp"], cwd=git_repo, check=True, capture_output=True)

    reconcile(settings)

    assert ("down", "temp") in deployed
    st = state_mod.load(settings.state_file)
    assert "temp" not in st["stacks"]
    assert not (settings.deploy_dir / "temp").exists()


def test_force_single_stack_redeploys_even_when_unchanged(git_repo, tmp_path, deployed):
    add_stack(git_repo, "traefik")
    settings = make_settings(tmp_path, git_repo_url=str(git_repo))
    reconcile(settings)
    deployed.clear()

    reconcile(settings, force={"traefik"})

    assert deployed == [("up", "traefik")]


def test_force_all_redeploys_every_stack(git_repo, tmp_path, deployed):
    add_stack(git_repo, "a")
    add_stack(git_repo, "b")
    settings = make_settings(tmp_path, git_repo_url=str(git_repo))
    reconcile(settings)
    deployed.clear()

    reconcile(settings, force={"all"})

    assert {p for _, p in deployed} == {"a", "b"}


def test_force_unrelated_stack_name_does_not_affect_others(git_repo, tmp_path, deployed):
    add_stack(git_repo, "a")
    settings = make_settings(tmp_path, git_repo_url=str(git_repo))
    reconcile(settings)
    deployed.clear()

    reconcile(settings, force={"nonexistent-stack"})

    assert deployed == []


def test_force_on_a_stack_already_removed_from_repo_persists_immediately(git_repo, tmp_path, deployed):
    import subprocess, shutil
    add_stack(git_repo, "ghost")
    settings = make_settings(tmp_path, git_repo_url=str(git_repo))
    reconcile(settings)
    st = state_mod.load(settings.state_file)
    assert "ghost" in st["stacks"]

    shutil.rmtree(git_repo / "compose" / "ghost")
    subprocess.run(["git", "add", "-A"], cwd=git_repo, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "remove ghost"], cwd=git_repo, check=True, capture_output=True)

    # No other stacks changed this pass: the force-clear itself must still reach disk
    reconcile(settings, force={"ghost"})

    st = state_mod.load(settings.state_file)
    assert "ghost" not in st["stacks"]


def test_unreachable_git_with_no_previous_checkout_notifies_and_returns_cleanly(tmp_path, deployed, notified):
    settings = make_settings(tmp_path, git_repo_url=str(tmp_path / "no-such-repo"))
    # Must not raise
    reconcile(settings)
    assert deployed == []
    assert len(notified) == 1
    title, description, color = notified[0]
    # Same title/color as any other hard failure; git-unreachable is not its own bucket
    assert "Failed" in title
    assert color == COLOR_ERROR
    # The repo URL is the diff block's own item, not a generic "repository" placeholder repeated in the prose
    assert f"- {tmp_path / 'no-such-repo'} (unreachable)" in description
    assert "the repository" in description
    # No containers were ever involved here, unlike every other notification kind: "n/a", not a fake count
    assert "📦 Result: `n/a`" in description


def test_reconcile_lock_is_released_after_call_allows_second_call(git_repo, tmp_path, deployed):
    add_stack(git_repo, "traefik")
    settings = make_settings(tmp_path, git_repo_url=str(git_repo))
    # If the flock leaked, this would hang forever
    reconcile(settings)
    reconcile(settings)
    # Reaching here at all proves the lock was released
    assert True


def test_deploy_failure_stderr_detail_is_logged_not_put_in_the_notification(git_repo, tmp_path, notified, caplog):
    # The raw docker/compose error goes to the logs for debugging; the Discord card stays short: service + note only
    add_stack(git_repo, "traefik")
    settings = make_settings(tmp_path, git_repo_url=str(git_repo))
    from docker_operator.compose import DeployError

    with caplog.at_level("ERROR", logger="docker_operator.reconcile"), \
         patch("docker_operator.compose.resolve_config", side_effect=_no_networks_json), \
         patch("docker_operator.compose.up",
               side_effect=DeployError("command failed: ...", stderr="port 80 already allocated\n")):
        reconcile(settings)

    assert len(notified) == 1
    title, description, color = notified[0]
    assert "port 80 already allocated" not in description
    assert "- traefik (retry 1/3, " in description
    assert color == COLOR_WARNING
    assert any("port 80 already allocated" in r.message for r in caplog.records)


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


def test_teardown_skipped_when_nothing_promoted_to_disk_yet(git_repo, tmp_path, deployed, notified):
    # A stack tracked in state whose compose.yaml was never promoted (deleted out-of-band) must just stop being tracked, not attempt `compose down`
    import subprocess, shutil
    add_stack(git_repo, "ghost")
    settings = make_settings(tmp_path, git_repo_url=str(git_repo), prune_removed_stacks=True)
    reconcile(settings)
    deployed.clear()
    notified.clear()

    (settings.deploy_dir / "ghost" / "compose.yaml").unlink()

    shutil.rmtree(git_repo / "compose" / "ghost")
    subprocess.run(["git", "add", "-A"], cwd=git_repo, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "remove ghost"], cwd=git_repo, check=True, capture_output=True)

    reconcile(settings)

    assert ("down", "ghost") not in deployed
    title, description, color = notified[-1]
    assert "Success" in title and color == COLOR_SUCCESS
    assert "+ ghost" in description
    assert "📦 Result: `1/1 containers started`" in description
    st = state_mod.load(settings.state_file)
    assert "ghost" not in st["stacks"]


def test_successful_teardown_notification_follows_the_standard_shape(git_repo, tmp_path, deployed, notified):
    import subprocess, shutil
    add_stack(git_repo, "temp", compose="services:\n  web:\n    image: alpine:3.20\n  worker:\n    image: alpine:3.20\n")
    settings = make_settings(tmp_path, git_repo_url=str(git_repo), prune_removed_stacks=True,
                              notify_webhook_url="https://discord.example.com/webhook")

    with patch("docker_operator.compose.resolve_config", side_effect=_no_networks_json), \
         patch("docker_operator.compose.up", side_effect=lambda *a, **k: None):
        reconcile(settings)
    notified.clear()

    shutil.rmtree(git_repo / "compose" / "temp")
    subprocess.run(["git", "add", "-A"], cwd=git_repo, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "remove temp"], cwd=git_repo, check=True, capture_output=True)

    def resolve_side_effect(compose_file, env_file, project, project_dir, timeout):
        return json.dumps({"networks": {}, "services": {"web": {}, "worker": {}}})

    with patch("docker_operator.compose.resolve_config", side_effect=resolve_side_effect), \
         patch("docker_operator.compose.down", side_effect=lambda *a, **k: None):
        reconcile(settings)

    assert len(notified) == 1
    title, description, color = notified[0]
    assert "Success" in title and color == COLOR_SUCCESS
    assert "torn down" in description
    # Real service list resolved from the compose file still on disk, not just the stack name; same detail level as a deploy notification
    assert "+ web" in description
    assert "+ worker" in description
    assert "+ temp" not in description  # the stack name itself is not a service; only real services appear in the diff block
    assert "📦 Result: `2/2 containers started`" in description


def test_teardown_failure_notifies_and_keeps_state_for_retry(git_repo, tmp_path, notified, caplog):
    import subprocess, shutil
    add_stack(git_repo, "temp", compose="services:\n  web:\n    image: alpine:3.20\n  worker:\n    image: alpine:3.20\n")
    settings = make_settings(tmp_path, git_repo_url=str(git_repo), prune_removed_stacks=True,
                              notify_webhook_url="https://discord.example.com/webhook")

    with patch("docker_operator.compose.resolve_config", side_effect=_no_networks_json), \
         patch("docker_operator.compose.up", side_effect=lambda *a, **k: None):
        # First pass: "temp" deploys cleanly, its own notification is not what this test is about
        reconcile(settings)

    shutil.rmtree(git_repo / "compose" / "temp")
    subprocess.run(["git", "add", "-A"], cwd=git_repo, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "remove temp"], cwd=git_repo, check=True, capture_output=True)

    def resolve_side_effect(compose_file, env_file, project, project_dir, timeout):
        return json.dumps({"networks": {}, "services": {"web": {}, "worker": {}}})

    from docker_operator.compose import DeployError
    with caplog.at_level("ERROR", logger="docker_operator.reconcile"), \
         patch("docker_operator.compose.resolve_config", side_effect=resolve_side_effect), \
         patch("docker_operator.compose.down", side_effect=DeployError("down failed", stderr="container busy\n")):
        reconcile(settings)

    title, description, color = notified[-1]
    # Raw detail goes to the logs, same as every other failure kind; the Discord card stays on the standard shape
    assert "container busy" not in description
    # Every real service marked the same way: a `down` failure doesn't say which one blocked it
    assert "- web (teardown error)" in description
    assert "- worker (teardown error)" in description
    assert "Failed" in title
    assert color == COLOR_ERROR
    st = state_mod.load(settings.state_file)
    # Kept for retry, not silently dropped
    assert "temp" in st["stacks"]


def test_teardown_notification_falls_back_to_stack_name_when_resolve_fails(git_repo, tmp_path, deployed, notified):
    # If the compose file can't be resolved anymore (e.g. secrets no longer decryptable), fall back to the pseudo-item rather than losing the notification
    import subprocess, shutil
    add_stack(git_repo, "temp")
    settings = make_settings(tmp_path, git_repo_url=str(git_repo), prune_removed_stacks=True,
                              notify_webhook_url="https://discord.example.com/webhook")

    with patch("docker_operator.compose.resolve_config", side_effect=_no_networks_json), \
         patch("docker_operator.compose.up", side_effect=lambda *a, **k: None):
        reconcile(settings)
    notified.clear()

    shutil.rmtree(git_repo / "compose" / "temp")
    subprocess.run(["git", "add", "-A"], cwd=git_repo, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "remove temp"], cwd=git_repo, check=True, capture_output=True)

    with patch("docker_operator.compose.resolve_config", side_effect=RuntimeError("cannot decrypt secrets")), \
         patch("docker_operator.compose.down", side_effect=lambda *a, **k: None):
        # Must not raise, and must still notify about the stack itself
        reconcile(settings)

    assert len(notified) == 1
    description = notified[0][1]
    assert "+ temp" in description
    assert "📦 Result: `1/1 containers started`" in description


def test_removed_stack_service_lookup_uses_the_short_status_timeout_not_the_full_deploy_timeout(
        git_repo, tmp_path, deployed, notified):
    # This lookup is purely diagnostic (for the notification's diff block), same class of call as the `ps` snapshot --
    # a stuck daemon must not be able to block the real teardown behind a 300s+ diagnostic wait
    import subprocess, shutil
    add_stack(git_repo, "temp")
    settings = make_settings(tmp_path, git_repo_url=str(git_repo), prune_removed_stacks=True,
                              deploy_timeout_seconds=300, notify_webhook_url="https://discord.example.com/webhook")

    with patch("docker_operator.compose.resolve_config", side_effect=_no_networks_json), \
         patch("docker_operator.compose.up", side_effect=lambda *a, **k: None):
        reconcile(settings)

    shutil.rmtree(git_repo / "compose" / "temp")
    subprocess.run(["git", "add", "-A"], cwd=git_repo, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "remove temp"], cwd=git_repo, check=True, capture_output=True)

    seen_timeouts = []

    def resolve_side_effect(compose_file, env_file, project, project_dir, timeout):
        seen_timeouts.append(timeout)
        return _no_networks_json()

    with patch("docker_operator.compose.resolve_config", side_effect=resolve_side_effect), \
         patch("docker_operator.compose.down", side_effect=lambda *a, **k: None):
        reconcile(settings)

    assert seen_timeouts == [15]


# --- retry budget: DEPLOY_MAX_RETRIES / DEPLOY_RETRY_DELAY_SECONDS ---

def _counting_failure(calls: list):
    def _fail(*a, **k):
        calls.append(1)
        raise RuntimeError("boom")
    return _fail


def test_failing_stack_stops_being_attempted_once_retries_are_spent(git_repo, tmp_path):
    add_stack(git_repo, "bad")
    settings = make_settings(tmp_path, git_repo_url=str(git_repo), deploy_max_retries=2, deploy_retry_delay_seconds=0)

    calls: list = []
    with patch("docker_operator.compose.resolve_config", side_effect=_counting_failure(calls)):
        reconcile(settings)
        reconcile(settings)
        # Retry budget already spent: must not attempt again
        reconcile(settings)

    assert len(calls) == 2
    st = state_mod.load(settings.state_file)
    assert "bad" not in st["stacks"]
    assert st["retries"]["bad"]["attempts"] == 2


def test_retry_notification_only_says_giving_up_on_the_final_attempt(git_repo, tmp_path, notified):
    # A deploy-stage failure (not validate): retrying can plausibly help here, so it still graduates warn -> error
    add_stack(git_repo, "bad")
    settings = make_settings(tmp_path, git_repo_url=str(git_repo), deploy_max_retries=2, deploy_retry_delay_seconds=0)

    from docker_operator.compose import DeployError
    with patch("docker_operator.compose.resolve_config", side_effect=_no_networks_json), \
         patch("docker_operator.compose.up", side_effect=DeployError("failed", stderr="boom")):
        reconcile(settings)
        reconcile(settings)

    assert len(notified) == 2
    first_title, first_description, first_color = notified[0]
    second_title, second_description, second_color = notified[1]
    assert "- bad (retry 1/2, 0s)" in first_description and "aborted" not in first_description
    assert first_color == COLOR_WARNING
    assert "Retrying" in first_title and "Failed" not in first_title
    assert "- bad (hard error)" in second_description and "aborted" in second_description
    assert second_color == COLOR_ERROR
    assert "Failed" in second_title


def test_validate_failure_is_always_a_hard_error_never_retrying(git_repo, tmp_path, notified):
    # A bad compose file needs a git fix, not a wait; unlike a deploy failure, it must never show as "Retrying"
    add_stack(git_repo, "bad")
    settings = make_settings(tmp_path, git_repo_url=str(git_repo), deploy_max_retries=5, deploy_retry_delay_seconds=0)

    with patch("docker_operator.compose.resolve_config", side_effect=RuntimeError("invalid compose file")):
        reconcile(settings)

    assert len(notified) == 1
    title, description, color = notified[0]
    assert "Failed" in title and "Retrying" not in title
    assert color == COLOR_ERROR
    assert "attempt" not in description
    assert "- bad (invalid compose error)" in description
    assert "📦 Result: `0/1 containers started, 1 failed`" in description


def test_validate_failure_lists_every_service_found_in_the_raw_compose_file(git_repo, tmp_path, notified):
    # docker compose config itself failed, so there's no verified service list: fall back to reading the raw file
    add_stack(git_repo, "bad", compose="services:\n  app:\n    image: alpine:3.20\n  worker:\n    image: alpine:3.20\n")
    settings = make_settings(tmp_path, git_repo_url=str(git_repo))

    with patch("docker_operator.compose.resolve_config", side_effect=RuntimeError("invalid compose file")):
        reconcile(settings)

    assert len(notified) == 1
    description = notified[0][1]
    assert "- app (invalid compose error)" in description
    assert "- worker (invalid compose error)" in description
    assert "📦 Result: `0/2 containers started, 2 failed`" in description


def test_validate_failure_falls_back_to_the_stack_name_when_the_file_cant_be_scanned(git_repo, tmp_path, notified):
    # Malformed enough that even the best-effort scan finds nothing under services: and we still notify, just about the stack itself
    add_stack(git_repo, "bad", compose="not even yaml-shaped\n")
    settings = make_settings(tmp_path, git_repo_url=str(git_repo))

    with patch("docker_operator.compose.resolve_config", side_effect=RuntimeError("invalid compose file")):
        reconcile(settings)

    assert len(notified) == 1
    description = notified[0][1]
    assert "- bad (invalid compose error)" in description


def test_validate_failure_service_scan_is_not_locked_to_2_space_indentation(git_repo, tmp_path, notified):
    # The scan measures the actual indent under services: instead of assuming 2 spaces, so a 4-space compose file still lists real names
    add_stack(git_repo, "bad",
              compose="services:\n    app:\n        image: alpine:3.20\n    worker:\n        image: alpine:3.20\n")
    settings = make_settings(tmp_path, git_repo_url=str(git_repo))

    with patch("docker_operator.compose.resolve_config", side_effect=RuntimeError("invalid compose file")):
        reconcile(settings)

    assert len(notified) == 1
    description = notified[0][1]
    assert "- app (invalid compose error)" in description
    assert "- worker (invalid compose error)" in description


def test_validate_failure_scan_survives_undecodable_bytes_in_the_compose_file(git_repo, tmp_path, notified):
    # A binary/non-UTF-8 compose file must degrade to the stack-name fallback, not crash the whole reconcile pass
    import subprocess
    add_stack(git_repo, "bad")
    (git_repo / "compose" / "bad" / "compose.yaml").write_bytes(b"services:\n  \xff\xfe broken\n")
    subprocess.run(["git", "add", "-A"], cwd=git_repo, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "corrupt compose file"], cwd=git_repo, check=True, capture_output=True)
    settings = make_settings(tmp_path, git_repo_url=str(git_repo))

    with patch("docker_operator.compose.resolve_config", side_effect=RuntimeError("invalid compose file")):
        # Must not raise
        reconcile(settings)

    assert len(notified) == 1
    description = notified[0][1]
    assert "- bad (invalid compose error)" in description


def test_retry_budget_resets_once_the_stack_content_changes(git_repo, tmp_path):
    add_stack(git_repo, "bad", compose="services:\n  bad:\n    image: bad:v1\n")
    settings = make_settings(tmp_path, git_repo_url=str(git_repo), deploy_max_retries=1, deploy_retry_delay_seconds=0)

    calls: list = []
    with patch("docker_operator.compose.resolve_config", side_effect=_counting_failure(calls)):
        # Attempt 1/1, exhausted
        reconcile(settings)
        # Skipped, same failing content
        reconcile(settings)
        assert len(calls) == 1

        add_stack(git_repo, "bad", compose="services:\n  bad:\n    image: bad:v2\n")
        # Different hash: gets a fresh attempt
        reconcile(settings)

    assert len(calls) == 2


def test_retry_delay_blocks_an_immediate_second_attempt(git_repo, tmp_path, monkeypatch):
    add_stack(git_repo, "bad")
    settings = make_settings(tmp_path, git_repo_url=str(git_repo), deploy_max_retries=5,
                              deploy_retry_delay_seconds=100)

    fake_now = [1_000_000.0]
    monkeypatch.setattr("docker_operator.reconcile.time.time", lambda: fake_now[0])

    calls: list = []
    with patch("docker_operator.compose.resolve_config", side_effect=_counting_failure(calls)):
        reconcile(settings)
        assert len(calls) == 1
        # Too soon: still within the backoff delay
        reconcile(settings)
        assert len(calls) == 1
        fake_now[0] += 150
        # Delay elapsed: retries again
        reconcile(settings)
        assert len(calls) == 2


def test_force_bypasses_an_exhausted_retry_budget(git_repo, tmp_path):
    add_stack(git_repo, "bad")
    settings = make_settings(tmp_path, git_repo_url=str(git_repo), deploy_max_retries=1, deploy_retry_delay_seconds=0)

    calls: list = []
    with patch("docker_operator.compose.resolve_config", side_effect=_counting_failure(calls)):
        # Attempt 1/1, exhausted
        reconcile(settings)
        # Skipped
        reconcile(settings)
        assert len(calls) == 1
        # Explicit force overrides the budget
        reconcile(settings, force={"bad"})
        assert len(calls) == 2


def test_successful_deploy_clears_prior_retry_state(git_repo, tmp_path, deployed):
    add_stack(git_repo, "flaky", compose="services:\n  flaky:\n    image: flaky:v1\n")
    settings = make_settings(tmp_path, git_repo_url=str(git_repo), deploy_max_retries=5, deploy_retry_delay_seconds=0)

    with patch("docker_operator.compose.resolve_config", side_effect=RuntimeError("boom")):
        reconcile(settings)
    st = state_mod.load(settings.state_file)
    assert st["retries"]["flaky"]["attempts"] == 1

    add_stack(git_repo, "flaky", compose="services:\n  flaky:\n    image: flaky:v2\n")
    # deployed fixture patches resolve_config/up back to succeeding
    reconcile(settings)

    st = state_mod.load(settings.state_file)
    assert "flaky" not in st.get("retries", {})
    assert "flaky" in st["stacks"]


# --- per-container status in outcome notifications ---

def test_success_notification_includes_container_diff_block(git_repo, tmp_path, notified):
    add_stack(git_repo, "web")
    settings = make_settings(tmp_path, git_repo_url=str(git_repo))

    def resolve_side_effect(compose_file, env_file, project, project_dir, timeout):
        return json.dumps({"networks": {}, "services": {"web": {}}})

    with patch("docker_operator.compose.resolve_config", side_effect=resolve_side_effect), \
         patch("docker_operator.compose.up", side_effect=lambda *a, **k: None):
        reconcile(settings)

    assert len(notified) == 1
    title, description, color = notified[0]
    assert "(1/1 containers)" in description
    assert "```diff" in description
    assert "+ web" in description
    assert "📦 Result: `1/1 containers started`" in description
    assert color == COLOR_SUCCESS


def test_deploy_failure_diff_marks_only_the_failed_container(git_repo, tmp_path, notified):
    add_stack(git_repo, "app", compose="services:\n  web:\n    image: alpine:3.20\n  worker:\n    image: alpine:3.20\n")
    settings = make_settings(tmp_path, git_repo_url=str(git_repo), deploy_max_retries=3, deploy_retry_delay_seconds=10,
                              notify_webhook_url="https://discord.example.com/webhook")

    def resolve_side_effect(compose_file, env_file, project, project_dir, timeout):
        return json.dumps({"networks": {}, "services": {"web": {}, "worker": {}}})

    def fake_ps(compose_file, env_file, project, project_dir, timeout):
        return {"web": {"Service": "web", "State": "running"},
                "worker": {"Service": "worker", "State": "exited"}}

    from docker_operator.compose import DeployError
    with patch("docker_operator.compose.resolve_config", side_effect=resolve_side_effect), \
         patch("docker_operator.compose.up", side_effect=DeployError("failed", stderr="boom")), \
         patch("docker_operator.compose.ps", side_effect=fake_ps):
        reconcile(settings)

    assert len(notified) == 1
    title, description, color = notified[0]
    assert "+ web" in description
    assert "- worker (retry 1/3, 10s)" in description
    assert "📦 Result: `1/2 containers started, 1 retrying`" in description
    assert color == COLOR_WARNING
    assert "Retrying" in title


def test_deploy_failure_diff_shows_hard_error_after_giving_up(git_repo, tmp_path, notified):
    add_stack(git_repo, "app")
    settings = make_settings(tmp_path, git_repo_url=str(git_repo), deploy_max_retries=1, deploy_retry_delay_seconds=0,
                              notify_webhook_url="https://discord.example.com/webhook")

    def resolve_side_effect(compose_file, env_file, project, project_dir, timeout):
        return json.dumps({"networks": {}, "services": {"app": {}}})

    def fake_ps(compose_file, env_file, project, project_dir, timeout):
        return {"app": {"Service": "app", "State": "exited"}}

    from docker_operator.compose import DeployError
    with patch("docker_operator.compose.resolve_config", side_effect=resolve_side_effect), \
         patch("docker_operator.compose.up", side_effect=DeployError("failed", stderr="boom")), \
         patch("docker_operator.compose.ps", side_effect=fake_ps):
        reconcile(settings)

    assert len(notified) == 1
    title, description, color = notified[0]
    assert "- app (hard error)" in description
    assert "aborted" in description
    assert "📦 Result: `0/1 containers started, 1 failed`" in description
    assert color == COLOR_ERROR
    assert "Failed" in title


def test_deploy_failure_diff_does_not_flag_a_one_shot_job_that_exited_zero(git_repo, tmp_path, notified):
    # "migrate" is a restart:"no" one-shot job that's supposed to run once and exit 0: it must read as "+" even though the stack as a whole failed
    add_stack(git_repo, "app", compose="services:\n  migrate:\n    image: alpine:3.20\n  web:\n    image: alpine:3.20\n")
    settings = make_settings(tmp_path, git_repo_url=str(git_repo), deploy_max_retries=3, deploy_retry_delay_seconds=10,
                              notify_webhook_url="https://discord.example.com/webhook")

    def resolve_side_effect(compose_file, env_file, project, project_dir, timeout):
        return json.dumps({"networks": {}, "services": {"migrate": {}, "web": {}}})

    def fake_ps(compose_file, env_file, project, project_dir, timeout):
        return {"migrate": {"Service": "migrate", "State": "exited", "ExitCode": 0},
                "web": {"Service": "web", "State": "exited", "ExitCode": 1}}

    from docker_operator.compose import DeployError
    with patch("docker_operator.compose.resolve_config", side_effect=resolve_side_effect), \
         patch("docker_operator.compose.up", side_effect=DeployError("failed", stderr="boom")), \
         patch("docker_operator.compose.ps", side_effect=fake_ps):
        reconcile(settings)

    assert len(notified) == 1
    description = notified[0][1]
    assert "+ migrate" in description
    assert "- web (retry 1/3, 10s)" in description
    assert "📦 Result: `1/2 containers started, 1 retrying`" in description


def test_container_status_lookup_failure_is_swallowed_not_raised(git_repo, tmp_path, notified):
    add_stack(git_repo, "app")
    settings = make_settings(tmp_path, git_repo_url=str(git_repo),
                              notify_webhook_url="https://discord.example.com/webhook")

    def resolve_side_effect(compose_file, env_file, project, project_dir, timeout):
        return json.dumps({"networks": {}, "services": {"app": {}}})

    from docker_operator.compose import DeployError
    with patch("docker_operator.compose.resolve_config", side_effect=resolve_side_effect), \
         patch("docker_operator.compose.up", side_effect=DeployError("failed", stderr="boom")), \
         patch("docker_operator.compose.ps", side_effect=RuntimeError("docker not found")):
        # Must not raise even though gathering container status itself failed
        reconcile(settings)

    assert len(notified) == 1
    assert "- app" in notified[0][1]


def test_container_status_not_queried_without_a_webhook_configured(git_repo, tmp_path, notified):
    # No notify_webhook_url: nothing will ever read the container snapshot, so the extra `ps` subprocess call must not happen
    add_stack(git_repo, "app")
    settings = make_settings(tmp_path, git_repo_url=str(git_repo), notify_webhook_url=None)

    def resolve_side_effect(compose_file, env_file, project, project_dir, timeout):
        return json.dumps({"networks": {}, "services": {"app": {}}})

    from docker_operator.compose import DeployError
    with patch("docker_operator.compose.resolve_config", side_effect=resolve_side_effect), \
         patch("docker_operator.compose.up", side_effect=DeployError("failed", stderr="boom")), \
         patch("docker_operator.compose.ps") as mock_ps:
        reconcile(settings)

    mock_ps.assert_not_called()


# --- NOTIFY_WEBHOOK_URL unset must fully disable notifications: no real network call, ever; these patch the real urllib.request.urlopen (not docker_operator.reconcile.notify like the `notified` fixture does elsewhere) since notify()'s own "if not url: return" guard is what's under test, and also assert on caplog since the notification-isolation try/except would otherwise silently swallow a broken guard's crash and make assert_not_called() pass for the wrong reason ---

def _assert_no_swallowed_notify_errors(caplog) -> None:
    assert not any("failed to send" in r.message or "failed to build" in r.message for r in caplog.records)


def test_no_network_call_on_success_without_a_webhook_configured(git_repo, tmp_path, deployed, caplog):
    add_stack(git_repo, "good")
    settings = make_settings(tmp_path, git_repo_url=str(git_repo), notify_webhook_url=None)

    with caplog.at_level("ERROR", logger="docker_operator.reconcile"), \
         patch("urllib.request.urlopen") as mock_urlopen:
        reconcile(settings)

    mock_urlopen.assert_not_called()
    _assert_no_swallowed_notify_errors(caplog)


def test_no_network_call_on_deploy_failure_without_a_webhook_configured(git_repo, tmp_path, caplog):
    add_stack(git_repo, "bad")
    settings = make_settings(tmp_path, git_repo_url=str(git_repo), notify_webhook_url=None,
                              deploy_max_retries=1, deploy_retry_delay_seconds=0)

    from docker_operator.compose import DeployError
    with caplog.at_level("ERROR", logger="docker_operator.reconcile"), \
         patch("urllib.request.urlopen") as mock_urlopen, \
         patch("docker_operator.compose.resolve_config", side_effect=_no_networks_json), \
         patch("docker_operator.compose.up", side_effect=DeployError("failed", stderr="boom")):
        # deploy_max_retries=1 means this single pass already hits the "gave up" hard-error path too
        reconcile(settings)

    mock_urlopen.assert_not_called()
    _assert_no_swallowed_notify_errors(caplog)


def test_no_network_call_on_validate_failure_without_a_webhook_configured(git_repo, tmp_path, caplog):
    add_stack(git_repo, "bad")
    settings = make_settings(tmp_path, git_repo_url=str(git_repo), notify_webhook_url=None)

    with caplog.at_level("ERROR", logger="docker_operator.reconcile"), \
         patch("urllib.request.urlopen") as mock_urlopen, \
         patch("docker_operator.compose.resolve_config", side_effect=RuntimeError("invalid compose file")):
        reconcile(settings)

    mock_urlopen.assert_not_called()
    _assert_no_swallowed_notify_errors(caplog)


def test_no_network_call_on_stack_removal_without_a_webhook_configured(git_repo, tmp_path, deployed, caplog):
    import subprocess, shutil
    add_stack(git_repo, "temp")
    settings = make_settings(tmp_path, git_repo_url=str(git_repo), notify_webhook_url=None,
                              prune_removed_stacks=True)
    reconcile(settings)

    shutil.rmtree(git_repo / "compose" / "temp")
    subprocess.run(["git", "add", "-A"], cwd=git_repo, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "remove temp"], cwd=git_repo, check=True, capture_output=True)

    with caplog.at_level("ERROR", logger="docker_operator.reconcile"), \
         patch("urllib.request.urlopen") as mock_urlopen:
        reconcile(settings)

    mock_urlopen.assert_not_called()
    _assert_no_swallowed_notify_errors(caplog)


def test_no_network_call_on_teardown_failure_without_a_webhook_configured(git_repo, tmp_path, deployed, caplog):
    import subprocess, shutil
    add_stack(git_repo, "temp")
    settings = make_settings(tmp_path, git_repo_url=str(git_repo), notify_webhook_url=None,
                              prune_removed_stacks=True)
    reconcile(settings)

    shutil.rmtree(git_repo / "compose" / "temp")
    subprocess.run(["git", "add", "-A"], cwd=git_repo, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "remove temp"], cwd=git_repo, check=True, capture_output=True)

    from docker_operator.compose import DeployError
    with caplog.at_level("ERROR", logger="docker_operator.reconcile"), \
         patch("urllib.request.urlopen") as mock_urlopen, \
         patch("docker_operator.compose.down", side_effect=DeployError("down failed", stderr="container busy")):
        reconcile(settings)

    mock_urlopen.assert_not_called()
    _assert_no_swallowed_notify_errors(caplog)


def test_no_network_call_on_git_unreachable_without_a_webhook_configured(tmp_path, deployed, caplog):
    settings = make_settings(tmp_path, git_repo_url=str(tmp_path / "no-such-repo"), notify_webhook_url=None)

    with caplog.at_level("ERROR", logger="docker_operator.reconcile"), \
         patch("urllib.request.urlopen") as mock_urlopen:
        reconcile(settings)

    mock_urlopen.assert_not_called()
    _assert_no_swallowed_notify_errors(caplog)


# --- recovered notification ---

def test_recovered_notification_sent_after_a_prior_failure(git_repo, tmp_path, deployed, notified):
    add_stack(git_repo, "flaky", compose="services:\n  flaky:\n    image: flaky:v1\n")
    settings = make_settings(tmp_path, git_repo_url=str(git_repo))

    with patch("docker_operator.compose.resolve_config", side_effect=RuntimeError("boom")):
        reconcile(settings)

    add_stack(git_repo, "flaky", compose="services:\n  flaky:\n    image: flaky:v2\n")
    # deployed fixture patches resolve_config/up back to succeeding
    reconcile(settings)

    title, description, color = notified[-1]
    assert "recovered" in description
    assert color == COLOR_SUCCESS


def test_success_notification_sent_even_without_recovered_wording(git_repo, tmp_path, deployed, notified):
    add_stack(git_repo, "traefik")
    settings = make_settings(tmp_path, git_repo_url=str(git_repo))

    reconcile(settings)

    assert len(notified) == 1
    title, description, color = notified[0]
    assert "recovered" not in description
    assert "deployed successfully" in description
    assert color == COLOR_SUCCESS


def test_each_stack_gets_its_own_notification_not_a_mixed_recap(git_repo, tmp_path, notified):
    add_stack(git_repo, "good")
    add_stack(git_repo, "bad")
    settings = make_settings(tmp_path, git_repo_url=str(git_repo))

    def resolve_side_effect(compose_file, env_file, project, project_dir, timeout):
        if project == "bad":
            raise RuntimeError("invalid compose file")
        return _no_networks_json()

    with patch("docker_operator.compose.resolve_config", side_effect=resolve_side_effect), \
         patch("docker_operator.compose.up", side_effect=lambda *a, **k: None):
        reconcile(settings)

    # One notification per stack, not one mixed recap and not one per status bucket
    assert len(notified) == 2
    by_title = [(title, description, color) for title, description, color in notified]
    success_title, success_description, success_color = next(t for t in by_title if "Success" in t[0])
    failed_title, failed_description, failed_color = next(t for t in by_title if "Failed" in t[0])
    assert "good" in success_description and "bad" not in success_description
    assert success_color == COLOR_SUCCESS
    assert "bad" in failed_description and "good" not in failed_description
    assert "invalid compose error" in failed_description
    assert failed_color == COLOR_ERROR


def test_each_stack_notification_sent_as_its_own_deploy_finishes(git_repo, tmp_path, notified):
    # Notifications must go out live, per stack, not be held back until the whole pass finishes
    add_stack(git_repo, "first")
    add_stack(git_repo, "second")
    settings = make_settings(tmp_path, git_repo_url=str(git_repo), deploy_priority=["first", "second"])
    seen_at_second_deploy = []

    def fake_up(compose_file, env_file, project, project_dir, *, pull, timeout, retry_login=None):
        if project == "second":
            seen_at_second_deploy.append(len(notified))

    with patch("docker_operator.compose.resolve_config", side_effect=_no_networks_json), \
         patch("docker_operator.compose.up", side_effect=fake_up):
        reconcile(settings)

    # "first" was already notified about by the time "second" started deploying
    assert seen_at_second_deploy == [1]
    assert len(notified) == 2


# --- notification bugs must never be mistaken for deploy/teardown failures, or abort the rest of the pass ---

def test_a_broken_success_notification_does_not_get_reported_as_a_deploy_failure(git_repo, tmp_path, deployed, caplog):
    # A bug while building/sending the success notification must not flip an already-successful deploy into "failed" state
    add_stack(git_repo, "good")
    settings = make_settings(tmp_path, git_repo_url=str(git_repo))

    with caplog.at_level("ERROR", logger="docker_operator.reconcile"), \
         patch("docker_operator.compose.resolve_config", side_effect=_no_networks_json), \
         patch("docker_operator.reconcile.notify", side_effect=RuntimeError("discord is down")):
        # Must not raise even though the notification layer is completely broken
        reconcile(settings)

    assert deployed == [("up", "good")]
    st = state_mod.load(settings.state_file)
    assert "good" in st["stacks"]
    assert "good" not in st.get("retries", {})
    assert any("failed to send success notification" in r.message for r in caplog.records)


def test_a_broken_failure_notification_still_records_the_retry_state(git_repo, tmp_path, caplog):
    # A bug while building/sending the failure notification must not cost us the retry bookkeeping that already happened
    add_stack(git_repo, "bad")
    settings = make_settings(tmp_path, git_repo_url=str(git_repo), deploy_max_retries=3)

    with caplog.at_level("ERROR", logger="docker_operator.reconcile"), \
         patch("docker_operator.compose.resolve_config", side_effect=RuntimeError("invalid compose file")), \
         patch("docker_operator.reconcile.notify", side_effect=RuntimeError("discord is down")):
        # Must not raise
        reconcile(settings)

    st = state_mod.load(settings.state_file)
    assert st["retries"]["bad"]["attempts"] == 1
    assert any("failed to build/send failure notification" in r.message for r in caplog.records)


def test_a_broken_notification_does_not_block_other_stacks_in_the_same_pass(git_repo, tmp_path, deployed):
    # The whole point of catching notification bugs locally: one stack's broken notification must never cost every other stack in the pass
    add_stack(git_repo, "first")
    add_stack(git_repo, "second")
    settings = make_settings(tmp_path, git_repo_url=str(git_repo))

    with patch("docker_operator.compose.resolve_config", side_effect=_no_networks_json), \
         patch("docker_operator.reconcile.notify", side_effect=RuntimeError("discord is down")):
        reconcile(settings)

    assert ("up", "first") in deployed
    assert ("up", "second") in deployed
    st = state_mod.load(settings.state_file)
    assert "first" in st["stacks"] and "second" in st["stacks"]


def test_a_broken_removal_notification_still_records_the_teardown(git_repo, tmp_path, deployed):
    # Same isolation guarantee for the removed-stack path: a broken notification must not undo (or hide) the actual teardown
    add_stack(git_repo, "temp")
    settings = make_settings(tmp_path, git_repo_url=str(git_repo), prune_removed_stacks=True)

    with patch("docker_operator.compose.resolve_config", side_effect=_no_networks_json), \
         patch("docker_operator.compose.up", side_effect=lambda *a, **k: None):
        reconcile(settings)

    import subprocess, shutil
    shutil.rmtree(git_repo / "compose" / "temp")
    subprocess.run(["git", "add", "-A"], cwd=git_repo, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "remove temp"], cwd=git_repo, check=True, capture_output=True)

    with patch("docker_operator.compose.down", side_effect=lambda *a, **k: None), \
         patch("docker_operator.reconcile.notify", side_effect=RuntimeError("discord is down")):
        # Must not raise
        reconcile(settings)

    st = state_mod.load(settings.state_file)
    assert "temp" not in st["stacks"]


def test_no_notification_on_a_pass_with_no_changes(git_repo, tmp_path, deployed, notified):
    add_stack(git_repo, "traefik")
    settings = make_settings(tmp_path, git_repo_url=str(git_repo))
    reconcile(settings)
    assert len(notified) == 1
    notified.clear()

    # Nothing changed this time: must not notify again
    reconcile(settings)

    assert notified == []


def test_stacks_left_running_when_prune_disabled_never_notify(git_repo, tmp_path, deployed, notified):
    import subprocess, shutil
    add_stack(git_repo, "temp")
    settings = make_settings(tmp_path, git_repo_url=str(git_repo), prune_removed_stacks=False)
    reconcile(settings)
    notified.clear()

    shutil.rmtree(git_repo / "compose" / "temp")
    subprocess.run(["git", "add", "-A"], cwd=git_repo, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "remove temp"], cwd=git_repo, check=True, capture_output=True)

    # This state persists forever until PRUNE_REMOVED_STACKS is flipped or the stack comes back; must not re-notify every pass
    reconcile(settings)
    reconcile(settings)
    reconcile(settings)

    assert notified == []


# --- .paused ---

def test_paused_stack_is_skipped_even_when_its_files_changed(git_repo, tmp_path, deployed):
    import subprocess
    add_stack(git_repo, "traefik")
    settings = make_settings(tmp_path, git_repo_url=str(git_repo))
    reconcile(settings)
    deployed.clear()

    add_stack(git_repo, "traefik", compose="services:\n  traefik:\n    image: traefik:v2\n", commit=False)
    (git_repo / "compose" / "traefik" / ".paused").write_text("")
    subprocess.run(["git", "add", "-A"], cwd=git_repo, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "pause traefik"], cwd=git_repo, check=True, capture_output=True)

    reconcile(settings)

    assert deployed == []
    st = state_mod.load(settings.state_file)
    assert "traefik" in st["stacks"]


def test_paused_stack_is_not_torn_down_when_prune_enabled(git_repo, tmp_path, deployed):
    import subprocess
    add_stack(git_repo, "traefik")
    settings = make_settings(tmp_path, git_repo_url=str(git_repo), prune_removed_stacks=True)
    reconcile(settings)
    deployed.clear()

    (git_repo / "compose" / "traefik" / ".paused").write_text("")
    subprocess.run(["git", "add", "-A"], cwd=git_repo, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "pause traefik"], cwd=git_repo, check=True, capture_output=True)

    reconcile(settings)

    assert ("down", "traefik") not in deployed
    st = state_mod.load(settings.state_file)
    assert "traefik" in st["stacks"]


def test_unpausing_picks_up_changes_made_while_paused(git_repo, tmp_path, deployed):
    import subprocess
    add_stack(git_repo, "traefik")
    settings = make_settings(tmp_path, git_repo_url=str(git_repo))
    reconcile(settings)
    deployed.clear()

    add_stack(git_repo, "traefik", compose="services:\n  traefik:\n    image: traefik:v2\n", commit=False)
    (git_repo / "compose" / "traefik" / ".paused").write_text("")
    subprocess.run(["git", "add", "-A"], cwd=git_repo, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "pause traefik"], cwd=git_repo, check=True, capture_output=True)
    reconcile(settings)
    assert deployed == []

    (git_repo / "compose" / "traefik" / ".paused").unlink()
    subprocess.run(["git", "add", "-A"], cwd=git_repo, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "unpause traefik"], cwd=git_repo, check=True, capture_output=True)
    reconcile(settings)

    assert deployed == [("up", "traefik")]


def test_force_deploys_a_paused_stack(git_repo, tmp_path, deployed):
    import subprocess
    add_stack(git_repo, "traefik")
    settings = make_settings(tmp_path, git_repo_url=str(git_repo))
    reconcile(settings)
    deployed.clear()

    (git_repo / "compose" / "traefik" / ".paused").write_text("")
    subprocess.run(["git", "add", "-A"], cwd=git_repo, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "pause traefik"], cwd=git_repo, check=True, capture_output=True)

    reconcile(settings, force={"traefik"})

    assert deployed == [("up", "traefik")]


def test_force_all_deploys_paused_stacks_too(git_repo, tmp_path, deployed):
    import subprocess
    add_stack(git_repo, "traefik")
    settings = make_settings(tmp_path, git_repo_url=str(git_repo))
    reconcile(settings)
    deployed.clear()

    (git_repo / "compose" / "traefik" / ".paused").write_text("")
    subprocess.run(["git", "add", "-A"], cwd=git_repo, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "pause traefik"], cwd=git_repo, check=True, capture_output=True)

    reconcile(settings, force={"all"})

    assert deployed == [("up", "traefik")]


def test_force_on_unrelated_stack_does_not_unpause_others(git_repo, tmp_path, deployed):
    import subprocess
    add_stack(git_repo, "traefik")
    add_stack(git_repo, "forgejo")
    settings = make_settings(tmp_path, git_repo_url=str(git_repo))
    reconcile(settings)
    deployed.clear()

    (git_repo / "compose" / "traefik" / ".paused").write_text("")
    subprocess.run(["git", "add", "-A"], cwd=git_repo, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "pause traefik"], cwd=git_repo, check=True, capture_output=True)

    reconcile(settings, force={"forgejo"})

    assert ("up", "traefik") not in deployed
    assert ("up", "forgejo") in deployed


# --- .depends_on ---

def test_explicit_depends_on_orders_deploy_without_any_network_relationship(git_repo, tmp_path, deployed):
    import subprocess
    add_stack(git_repo, "db")
    add_stack(git_repo, "app", commit=False)
    (git_repo / "compose" / "app" / ".depends_on").write_text("db\n")
    subprocess.run(["git", "add", "-A"], cwd=git_repo, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "add app depending on db"], cwd=git_repo, check=True, capture_output=True)
    settings = make_settings(tmp_path, git_repo_url=str(git_repo))

    reconcile(settings)

    order = [project for action, project in deployed]
    assert order.index("db") < order.index("app")


def test_depends_on_naming_a_stack_outside_this_batch_is_ignored(git_repo, tmp_path, deployed):
    import subprocess
    add_stack(git_repo, "app", commit=False)
    (git_repo / "compose" / "app" / ".depends_on").write_text("not-a-real-stack\n")
    subprocess.run(["git", "add", "-A"], cwd=git_repo, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "add app"], cwd=git_repo, check=True, capture_output=True)
    settings = make_settings(tmp_path, git_repo_url=str(git_repo))

    reconcile(settings)

    assert deployed == [("up", "app")]


# --- validate() ---

def test_validate_returns_true_when_every_stack_is_valid(git_repo, tmp_path):
    add_stack(git_repo, "good")
    settings = make_settings(tmp_path, git_repo_url=str(git_repo))

    with patch("docker_operator.compose.resolve_config", side_effect=_no_networks_json):
        assert validate(settings) is True


def test_validate_returns_false_when_any_stack_is_invalid(git_repo, tmp_path):
    add_stack(git_repo, "good")
    add_stack(git_repo, "bad")
    settings = make_settings(tmp_path, git_repo_url=str(git_repo))

    def resolve_side_effect(compose_file, env_file, project, project_dir, timeout):
        if project == "bad":
            raise RuntimeError("invalid compose file")
        return _no_networks_json()

    with patch("docker_operator.compose.resolve_config", side_effect=resolve_side_effect):
        assert validate(settings) is False


def test_validate_never_calls_compose_up(git_repo, tmp_path):
    add_stack(git_repo, "traefik")
    settings = make_settings(tmp_path, git_repo_url=str(git_repo))

    with patch("docker_operator.compose.resolve_config", side_effect=_no_networks_json), \
         patch("docker_operator.compose.up") as mock_up:
        validate(settings)

    mock_up.assert_not_called()


def test_validate_leaves_deploy_dir_and_state_untouched(git_repo, tmp_path):
    add_stack(git_repo, "traefik")
    settings = make_settings(tmp_path, git_repo_url=str(git_repo))

    with patch("docker_operator.compose.resolve_config", side_effect=_no_networks_json):
        validate(settings)

    assert not settings.deploy_dir.exists()
    assert not settings.state_file.exists()


def test_validate_returns_false_when_repo_unreachable_with_no_cache(tmp_path):
    settings = make_settings(tmp_path, git_repo_url=str(tmp_path / "no-such-repo"))
    assert validate(settings) is False


def test_validate_uses_cached_checkout_when_remote_becomes_unreachable(git_repo, tmp_path):
    add_stack(git_repo, "traefik")
    settings = make_settings(tmp_path, git_repo_url=str(git_repo))
    with patch("docker_operator.compose.resolve_config", side_effect=_no_networks_json):
        assert validate(settings) is True

    moved_away = git_repo.parent / "moved-away"
    git_repo.rename(moved_away)
    try:
        with patch("docker_operator.compose.resolve_config", side_effect=_no_networks_json):
            assert validate(settings) is True
    finally:
        moved_away.rename(git_repo)


# --- corrupted state.json ---

def test_reconcile_recovers_from_a_malformed_state_file_instead_of_crashing(git_repo, tmp_path, deployed):
    add_stack(git_repo, "traefik")
    settings = make_settings(tmp_path, git_repo_url=str(git_repo))
    settings.state_file.parent.mkdir(parents=True, exist_ok=True)
    settings.state_file.write_text('{"stacks": ["not", "a", "dict"]}')

    # Must not raise: treated the same as a fresh install, redeploys everything
    reconcile(settings)

    assert deployed == [("up", "traefik")]
