from __future__ import annotations
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
# --import-mode=importlib (see pyproject.toml) doesn't auto-add this dir, so add it explicitly for `from conftest import ...` to resolve
sys.path.insert(0, str(Path(__file__).resolve().parent))

from docker_operator import compose  # noqa: E402
from docker_operator.config import Settings  # noqa: E402

# Skip collecting tests/e2e/ entirely when docker isn't on PATH, and keep a single shared conftest.py so `from conftest import ...` stays unambiguous
if shutil.which("docker") is None:
    collect_ignore_glob = ["e2e/test_*.py"]


def _git(args: list[str], cwd: Path) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True)


# Real local git repo (real `git` binary, no mocking) with an empty compose/ dir, usable as a GIT_REPO_URL local-path source
@pytest.fixture
def git_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "source-repo"
    repo.mkdir()
    _git(["init", "--initial-branch=main"], repo)
    _git(["config", "user.email", "test@example.com"], repo)
    _git(["config", "user.name", "test"], repo)
    (repo / "compose").mkdir()
    (repo / "compose" / ".gitkeep").write_text("")
    _git(["add", "-A"], repo)
    _git(["commit", "-m", "initial"], repo)
    return repo


# Write a stack's files into repo/compose/<name>/ and commit them
def add_stack(repo: Path, name: str, *, compose: str = None, env_config: str = None,
              env_secrets_encrypted: bytes = None, commit: bool = True) -> None:
    stack_dir = repo / "compose" / name
    stack_dir.mkdir(parents=True, exist_ok=True)
    if compose is None:
        compose = f"services:\n  {name}:\n    image: alpine:3.20\n"
    (stack_dir / "compose.yaml").write_text(compose)
    if env_config is not None:
        (stack_dir / ".env.config").write_text(env_config)
    if env_secrets_encrypted is not None:
        (stack_dir / ".env.secrets.encrypted").write_bytes(env_secrets_encrypted)
    if commit:
        _git(["add", "-A"], repo)
        _git(["commit", "-m", f"add/update stack {name}"], repo)


# Generate a real age keypair via age-keygen
@pytest.fixture
def age_key(tmp_path: Path) -> tuple[Path, str]:
    key_file = tmp_path / "age.key"
    proc = subprocess.run(["age-keygen", "-o", str(key_file)], capture_output=True, text=True, check=True)
    pub = None
    for line in (proc.stderr or "").splitlines():
        if "public key:" in line.lower():
            pub = line.split(":", 1)[1].strip()
    assert pub, f"could not parse age public key from age-keygen output: {proc.stderr!r}"
    return key_file, pub


# Encrypt dotenv text via a real sops+age round trip, for realistic .env.secrets.encrypted fixtures
def encrypt_dotenv(plaintext: str, age_key_file: Path, age_public_key: str, tmp_path: Path) -> bytes:
    plain = tmp_path / f"plain-{abs(hash(plaintext))}.env"
    plain.write_text(plaintext)
    proc = subprocess.run(
        ["sops", "--input-type", "dotenv", "--output-type", "dotenv", "--encrypt",
         "--age", age_public_key, str(plain)],
        capture_output=True, text=True, check=True,
    )
    return proc.stdout.encode()


def make_settings(tmp_path: Path, *, git_repo_url: str, sops_age_key_file: Path | None = None,
                   **overrides) -> Settings:
    defaults = dict(
        webhook_secret="test-secret",
        git_repo_url=git_repo_url,
        git_branch="main",
        data_dir=tmp_path / "data",
        deploy_dir=tmp_path / "deploy",
        deploy_uid=None,
        deploy_gid=None,
        compose_subdir="compose",
        sops_age_key_file=sops_age_key_file,
        listen_host="127.0.0.1",
        listen_port=0,
        webhook_path="/webhook",
        pull_images=False,
        prune_removed_stacks=False,
        poll_interval_seconds=0,
        deploy_timeout_seconds=30,
        deploy_max_retries=3,
        deploy_retry_delay_seconds=0,
        deploy_priority=(),
        notify_webhook_url=None,
        registry_host=None,
        registry_username=None,
        registry_password=None,
        log_level="INFO",
        log_dir=tmp_path / "logs",
    )
    defaults.update(overrides)
    return Settings(**defaults)


# Everything below is e2e-only; everything above must keep working with no docker daemon present

# Track (deploy_path, project) pairs an e2e test deployed for real, and tear each down after the test regardless of pass/fail
@pytest.fixture
def real_docker_cleanup():
    registered: list[tuple[Path, str]] = []

    def register(deploy_path: Path, project: str) -> None:
        registered.append((deploy_path, project))

    yield register

    for deploy_path, project in registered:
        compose_file = deploy_path / "compose.yaml"
        env_file = deploy_path / ".env"
        if not compose_file.is_file():
            continue
        try:
            compose.down(compose_file, env_file, project, deploy_path, timeout=60)
        except Exception as exc:
            print(f"WARNING: e2e cleanup failed for project {project!r}: {exc}")
