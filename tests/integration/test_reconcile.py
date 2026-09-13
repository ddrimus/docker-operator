from __future__ import annotations
import json
from unittest.mock import patch

import pytest

from docker_operator import state as state_mod
from docker_operator.reconcile import reconcile, validate
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

    def fake_up(compose_file, env_file, project, project_dir, *, pull, timeout):
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

    def fake_up(compose_file, env_file, project, project_dir, *, pull, timeout):
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


def test_unreachable_git_with_no_previous_checkout_notifies_and_returns_cleanly(tmp_path, deployed):
    settings = make_settings(tmp_path, git_repo_url=str(tmp_path / "no-such-repo"))
    notified = []
    with patch("docker_operator.reconcile.notify", side_effect=lambda url, msg: notified.append(msg)):
        # Must not raise
        reconcile(settings)
    assert deployed == []
    assert len(notified) == 1
    assert "cannot reach" in notified[0]


def test_reconcile_lock_is_released_after_call_allows_second_call(git_repo, tmp_path, deployed):
    add_stack(git_repo, "traefik")
    settings = make_settings(tmp_path, git_repo_url=str(git_repo))
    # If the flock leaked, this would hang forever
    reconcile(settings)
    reconcile(settings)
    # Reaching here at all proves the lock was released
    assert True


def test_deploy_failure_notification_includes_stderr_detail(git_repo, tmp_path):
    add_stack(git_repo, "traefik")
    settings = make_settings(tmp_path, git_repo_url=str(git_repo))
    from docker_operator.compose import DeployError

    notified = []
    with patch("docker_operator.compose.resolve_config", side_effect=_no_networks_json), \
         patch("docker_operator.compose.up",
               side_effect=DeployError("command failed: ...", stderr="port 80 already allocated\n")), \
         patch("docker_operator.reconcile.notify", side_effect=lambda url, msg: notified.append(msg)):
        reconcile(settings)

    assert len(notified) == 1
    assert "port 80 already allocated" in notified[0]


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


def test_teardown_skipped_when_nothing_promoted_to_disk_yet(git_repo, tmp_path, deployed):
    # A stack tracked in state whose compose.yaml was never promoted (deleted out-of-band) must just stop being tracked, not attempt `compose down`
    import subprocess, shutil
    add_stack(git_repo, "ghost")
    settings = make_settings(tmp_path, git_repo_url=str(git_repo), prune_removed_stacks=True)
    reconcile(settings)
    deployed.clear()

    (settings.deploy_dir / "ghost" / "compose.yaml").unlink()

    shutil.rmtree(git_repo / "compose" / "ghost")
    subprocess.run(["git", "add", "-A"], cwd=git_repo, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "remove ghost"], cwd=git_repo, check=True, capture_output=True)

    reconcile(settings)

    assert ("down", "ghost") not in deployed
    st = state_mod.load(settings.state_file)
    assert "ghost" not in st["stacks"]


def test_teardown_failure_notifies_and_keeps_state_for_retry(git_repo, tmp_path):
    import subprocess, shutil
    add_stack(git_repo, "temp")
    settings = make_settings(tmp_path, git_repo_url=str(git_repo), prune_removed_stacks=True)

    with patch("docker_operator.compose.resolve_config", side_effect=_no_networks_json), \
         patch("docker_operator.compose.up", side_effect=lambda *a, **k: None):
        reconcile(settings)

    shutil.rmtree(git_repo / "compose" / "temp")
    subprocess.run(["git", "add", "-A"], cwd=git_repo, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "remove temp"], cwd=git_repo, check=True, capture_output=True)

    notified = []
    from docker_operator.compose import DeployError
    with patch("docker_operator.compose.down", side_effect=DeployError("down failed", stderr="container busy\n")), \
         patch("docker_operator.reconcile.notify", side_effect=lambda url, msg: notified.append(msg)):
        reconcile(settings)

    assert len(notified) == 1
    assert "container busy" in notified[0]
    st = state_mod.load(settings.state_file)
    # Kept for retry, not silently dropped
    assert "temp" in st["stacks"]


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


def test_retry_notification_only_says_giving_up_on_the_final_attempt(git_repo, tmp_path):
    add_stack(git_repo, "bad")
    settings = make_settings(tmp_path, git_repo_url=str(git_repo), deploy_max_retries=2, deploy_retry_delay_seconds=0)

    notified: list = []
    with patch("docker_operator.compose.resolve_config", side_effect=RuntimeError("boom")), \
         patch("docker_operator.reconcile.notify", side_effect=lambda url, msg: notified.append(msg)):
        reconcile(settings)
        reconcile(settings)

    assert "attempt 1/2" in notified[0] and "giving up" not in notified[0]
    assert "giving up" in notified[1]


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


# --- recovered notification ---

def test_recovered_notification_sent_after_a_prior_failure(git_repo, tmp_path, deployed):
    add_stack(git_repo, "flaky", compose="services:\n  flaky:\n    image: flaky:v1\n")
    settings = make_settings(tmp_path, git_repo_url=str(git_repo))

    with patch("docker_operator.compose.resolve_config", side_effect=RuntimeError("boom")):
        reconcile(settings)

    add_stack(git_repo, "flaky", compose="services:\n  flaky:\n    image: flaky:v2\n")
    notified = []
    with patch("docker_operator.reconcile.notify", side_effect=lambda url, msg: notified.append(msg)):
        # deployed fixture patches resolve_config/up back to succeeding
        reconcile(settings)

    assert len(notified) == 1
    assert "recovered" in notified[0]


def test_no_recovered_notification_on_an_ordinary_first_deploy(git_repo, tmp_path, deployed):
    add_stack(git_repo, "traefik")
    settings = make_settings(tmp_path, git_repo_url=str(git_repo))

    notified = []
    with patch("docker_operator.reconcile.notify", side_effect=lambda url, msg: notified.append(msg)):
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
