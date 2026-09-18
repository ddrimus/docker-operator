from __future__ import annotations
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from docker_operator.reconcile import reconcile
from conftest import add_stack, make_settings, encrypt_dotenv

pytestmark = pytest.mark.e2e


def _is_running(container_name: str) -> bool:
    proc = subprocess.run(
        ["docker", "inspect", "--format", "{{.State.Running}}", container_name],
        capture_output=True, text=True,
    )
    return proc.returncode == 0 and proc.stdout.strip() == "true"


# Container ID changes across a real `docker compose up` recreate, but not across a plain stop/start, which distinguishes the two
def _container_id(container_name: str) -> str:
    return subprocess.run(["docker", "inspect", "--format", "{{.Id}}", container_name],
                           capture_output=True, text=True, check=True).stdout.strip()


def _marker(container_name: str) -> str:
    return subprocess.run(["docker", "exec", container_name, "printenv", "MARKER"],
                           capture_output=True, text=True, check=True).stdout.strip()


# Printed (not logged) so it shows up in pytest's own captured-output-on-failure, no LOG_LEVEL wiring needed to see it
def _log(message: str) -> None:
    print(f"[trial] {message}")


def test_stack_actually_runs_via_real_docker(git_repo: Path, tmp_path: Path, real_docker_cleanup):
    # Reuse tmp_path's unique name so every real docker resource this test touches can't collide with a leftover from a previous run
    name = f"e2e-solo-{tmp_path.name}"
    add_stack(git_repo, name, compose=f"""\
services:
  {name}:
    image: alpine:3.20
    container_name: {name}
    command: ["sleep", "3600"]
""")
    settings = make_settings(tmp_path, git_repo_url=str(git_repo), pull_images=False,
                              deploy_timeout_seconds=120)

    reconcile(settings)
    real_docker_cleanup(settings.deploy_dir / name, name)

    assert (settings.deploy_dir / name / "compose.yaml").is_file()
    assert _is_running(name)


def test_network_dependency_ordering_with_real_docker(git_repo: Path, tmp_path: Path, real_docker_cleanup):
    # Real docker compose, not a mocked resolve_config; wrong topo-ordering would fail the dependent stack's `up` with "network not found"
    suffix = tmp_path.name
    owner = f"e2e-owner-{suffix}"
    dependent = f"e2e-dependent-{suffix}"
    net = f"e2e-net-{suffix}"

    add_stack(git_repo, owner, compose=f"""\
services:
  {owner}:
    image: alpine:3.20
    container_name: {owner}
    command: ["sleep", "3600"]
    networks: ["shared"]
networks:
  shared:
    name: {net}
    driver: bridge
""")
    add_stack(git_repo, dependent, compose=f"""\
services:
  {dependent}:
    image: alpine:3.20
    container_name: {dependent}
    command: ["sleep", "3600"]
    networks: ["shared"]
networks:
  shared:
    name: {net}
    external: true
""")
    settings = make_settings(tmp_path, git_repo_url=str(git_repo), pull_images=False,
                              deploy_timeout_seconds=120)

    reconcile(settings)
    real_docker_cleanup(settings.deploy_dir / dependent, dependent)
    real_docker_cleanup(settings.deploy_dir / owner, owner)

    assert _is_running(owner)
    assert _is_running(dependent)


def test_secret_actually_reaches_the_running_container(git_repo: Path, tmp_path: Path,
                                                         age_key, real_docker_cleanup):
    key_file, pub = age_key
    name = f"e2e-secret-{tmp_path.name}"
    add_stack(git_repo, name,
              compose=f"""\
services:
  {name}:
    image: alpine:3.20
    container_name: {name}
    command: ["sleep", "3600"]
    environment:
      SECRET_VALUE: "${{SECRET_VALUE}}"
""",
              env_secrets_encrypted=encrypt_dotenv("SECRET_VALUE=hunter2\n", key_file, pub, tmp_path))
    settings = make_settings(tmp_path, git_repo_url=str(git_repo), pull_images=False,
                              deploy_timeout_seconds=120, sops_age_key_file=key_file)

    reconcile(settings)
    real_docker_cleanup(settings.deploy_dir / name, name)

    assert _is_running(name)
    proc = subprocess.run(["docker", "exec", name, "printenv", "SECRET_VALUE"],
                           capture_output=True, text=True, check=True)
    assert proc.stdout.strip() == "hunter2"


def test_removed_stack_actually_torn_down_by_real_docker(git_repo: Path, tmp_path: Path, real_docker_cleanup):
    import shutil as _shutil
    name = f"e2e-removed-{tmp_path.name}"
    add_stack(git_repo, name, compose=f"""\
services:
  {name}:
    image: alpine:3.20
    container_name: {name}
    command: ["sleep", "3600"]
""")
    settings = make_settings(tmp_path, git_repo_url=str(git_repo), pull_images=False,
                              deploy_timeout_seconds=120, prune_removed_stacks=True)

    reconcile(settings)
    assert _is_running(name)
    # Registered for cleanup regardless: a no-op if the removal below already tore it down, a safety net if the test fails before that
    real_docker_cleanup(settings.deploy_dir / name, name)

    _shutil.rmtree(git_repo / "compose" / name)
    subprocess.run(["git", "add", "-A"], cwd=git_repo, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "remove stack"], cwd=git_repo, check=True, capture_output=True)

    reconcile(settings)

    assert not _is_running(name)


def test_broken_deploy_reports_real_per_container_status(git_repo: Path, tmp_path: Path, real_docker_cleanup):
    # compose.ps() only runs when notify_webhook_url is set, so the realistic-session test's broken-image step (below) never actually exercises it --
    # this is the one test in the suite that does, against a real docker daemon instead of the mocked `ps` used everywhere else
    name = f"e2e-ps-{tmp_path.name}"
    add_stack(git_repo, name, compose=f"""\
services:
  {name}:
    image: alpine:e2e-ps-tag-does-not-exist
    pull_policy: never
    container_name: {name}
    command: ["sleep", "3600"]
""")
    settings = make_settings(tmp_path, git_repo_url=str(git_repo), pull_images=False, deploy_timeout_seconds=120,
                              deploy_max_retries=1, deploy_retry_delay_seconds=0,
                              notify_webhook_url="https://discord.example.com/webhook")
    real_docker_cleanup(settings.deploy_dir / name, name)

    calls = []
    with patch("docker_operator.reconcile.notify", side_effect=lambda *a: calls.append(a)):
        reconcile(settings)

    assert len(calls) == 1
    _, _, description, _ = calls[0]
    assert f"- {name} (hard error)" in description


# One realistic homelab session end to end: everything a user could do to a running stack by hand, in order
def test_realistic_user_session_covers_stop_start_pause_force_and_removal(git_repo: Path, tmp_path: Path,
                                                                            real_docker_cleanup):
    import shutil
    from docker_operator import state as state_mod
    name = f"e2e-trial-{tmp_path.name}"
    settings = make_settings(tmp_path, git_repo_url=str(git_repo), pull_images=False,
                              deploy_timeout_seconds=120, prune_removed_stacks=True)

    def compose_with_marker(marker: str) -> str:
        return f"""\
services:
  {name}:
    image: alpine:3.20
    container_name: {name}
    command: ["sleep", "3600"]
    environment:
      MARKER: "{marker}"
"""

    _log("1/11 fresh deploy")
    # Registered for cleanup now, a no-op by the time this test tears the stack down itself at the end
    add_stack(git_repo, name, compose=compose_with_marker("v1"))
    reconcile(settings)
    real_docker_cleanup(settings.deploy_dir / name, name)
    assert _is_running(name)
    assert _marker(name) == "v1"
    _log("1/11 OK: running, MARKER=v1")

    _log("2/11 manual `docker stop`, then reconcile with no git change")
    # The operator must never treat "not running" as something to fix on its own
    subprocess.run(["docker", "stop", name], check=True, capture_output=True)
    assert not _is_running(name)
    reconcile(settings)
    assert not _is_running(name)
    _log("2/11 OK: left stopped")

    _log("3/11 manual `docker start`")
    # Plain docker commands work normally against an operator-deployed stack
    subprocess.run(["docker", "start", name], check=True, capture_output=True)
    assert _is_running(name)
    _log("3/11 OK: running again")

    _log("4/11 manual `docker restart`, then reconcile with no git change")
    # Same container the whole time: restart isn't a recreate, and the operator still has no reason to touch it
    id_before_restart = _container_id(name)
    subprocess.run(["docker", "restart", name], check=True, capture_output=True)
    assert _is_running(name)
    assert _container_id(name) == id_before_restart
    reconcile(settings)
    assert _container_id(name) == id_before_restart
    _log("4/11 OK: same container throughout")

    _log("5/11 real content change in git")
    id_before_change = _container_id(name)
    add_stack(git_repo, name, compose=compose_with_marker("v2"))
    reconcile(settings)
    assert _marker(name) == "v2"
    assert _container_id(name) != id_before_change
    _log("5/11 OK: picked up, container recreated, MARKER=v2")

    _log("6/11 pause, then change again in the same commit")
    add_stack(git_repo, name, compose=compose_with_marker("v3"), commit=False)
    (git_repo / "compose" / name / ".paused").write_text("")
    subprocess.run(["git", "add", "-A"], cwd=git_repo, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "pause and change"], cwd=git_repo, check=True, capture_output=True)
    reconcile(settings)
    assert _marker(name) == "v2"
    _log("6/11 OK: frozen at MARKER=v2 while paused")

    _log("7/11 unpause")
    (git_repo / "compose" / name / ".paused").unlink()
    subprocess.run(["git", "add", "-A"], cwd=git_repo, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "unpause"], cwd=git_repo, check=True, capture_output=True)
    reconcile(settings)
    assert _marker(name) == "v3"
    _log("7/11 OK: accumulated change applied, MARKER=v3")

    _log("8/11 manual teardown, then --force to recover")
    # A manual `docker compose down -v --rmi all` leaves nothing for the operator to notice on its own
    subprocess.run(["docker", "rm", "-f", name], check=True, capture_output=True)
    assert not _is_running(name)
    reconcile(settings)
    assert not _is_running(name)
    reconcile(settings, force={name})
    assert _is_running(name)
    assert _marker(name) == "v3"
    _log("8/11 OK: stayed gone until forced, then came back")

    _log("9/11 a broken image reference fails at deploy, not silently")
    # pull_policy: never keeps this failure local and deterministic; no network/registry flakiness in CI
    add_stack(git_repo, name, compose=f"""\
services:
  {name}:
    image: alpine:e2e-trial-tag-does-not-exist
    pull_policy: never
    container_name: {name}
    command: ["sleep", "3600"]
    environment:
      MARKER: "v4"
""")
    reconcile(settings)
    st = state_mod.load(settings.state_file)
    assert st["retries"][name]["attempts"] == 1
    _log("9/11 OK: deploy failed as expected, retry recorded")

    _log("10/11 fixing the image and reconciling recovers it")
    add_stack(git_repo, name, compose=compose_with_marker("v4"))
    reconcile(settings)
    assert _is_running(name)
    assert _marker(name) == "v4"
    st = state_mod.load(settings.state_file)
    assert name not in st.get("retries", {})
    _log("10/11 OK: fixed image deployed, retry state cleared")

    _log("11/11 removing the stack from the repo entirely")
    shutil.rmtree(git_repo / "compose" / name)
    subprocess.run(["git", "add", "-A"], cwd=git_repo, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "remove stack"], cwd=git_repo, check=True, capture_output=True)
    reconcile(settings)
    assert not _is_running(name)
    assert not (settings.deploy_dir / name).exists()
    _log("11/11 OK: torn down and cleaned up")
