from __future__ import annotations
import subprocess
from pathlib import Path

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
