from __future__ import annotations
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


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
