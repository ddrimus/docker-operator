# Syncs the local git checkout to match the remote or bind-mounted repo
from __future__ import annotations
import logging
import os
import subprocess
from pathlib import Path

from .config import local_repo_path
from .util import exc_detail

log = logging.getLogger("docker_operator.git")


# Run a git command in repo_dir, raising on nonzero exit or timeout
def _run(args: list[str], cwd: Path | None, timeout: int, env) -> subprocess.CompletedProcess:
    log.debug("git: %s", " ".join(args))
    return subprocess.run(args, cwd=cwd, timeout=timeout, env=env,
                           capture_output=True, text=True, check=True)


# Clone if missing, else fetch and hard-reset to origin/<branch>, falling back to the last known-good checkout if the remote is unreachable
def sync_repo(repo_url: str, branch: str, repo_dir: Path, timeout: int = 120) -> tuple[str, bool]:
    env = os.environ.copy()

    if not (repo_dir / ".git").is_dir():
        repo_dir.parent.mkdir(parents=True, exist_ok=True)
        transport = "local filesystem" if local_repo_path(repo_url) is not None else "network"
        log.info("cloning %s (branch=%s, transport=%s) -> %s", repo_url, branch, transport, repo_dir)
        _run(["git", "clone", "--branch", branch, "--single-branch", repo_url, str(repo_dir)],
             None, timeout, env)
        head = _run(["git", "rev-parse", "HEAD"], repo_dir, 10, env).stdout.strip()
        log.info("repo cloned at %s", head[:12])
        return head, True

    try:
        _run(["git", "fetch", "--prune", "origin", branch], repo_dir, timeout, env)
        _run(["git", "reset", "--hard", f"origin/{branch}"], repo_dir, 30, env)
        # -fdx also clears untracked/gitignored leftovers, since discover_stacks() only checks for a compose.yaml on disk, not git tracking
        _run(["git", "clean", "-fdx"], repo_dir, 30, env)
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        head = _run(["git", "rev-parse", "HEAD"], repo_dir, 10, env).stdout.strip()
        log.warning("git remote unreachable (%s), keeping last known-good checkout at %s, will retry",
                    exc_detail(exc), head[:12])
        # synced=False tells the caller to treat this as no changes, never as a reason to touch what's already running
        return head, False

    head = _run(["git", "rev-parse", "HEAD"], repo_dir, 10, env).stdout.strip()
    log.info("repo synced at %s", head[:12])
    return head, True
