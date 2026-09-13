# Wraps `docker compose` invocations with logging and structured errors
from __future__ import annotations
import logging
import subprocess
from pathlib import Path

log = logging.getLogger("docker_operator.compose")


# Raised when a docker compose invocation fails or times out
class DeployError(RuntimeError):
    def __init__(self, message: str, stderr: str = "") -> None:
        super().__init__(message)
        self.stderr = stderr


# Run a docker compose command, raising DeployError on nonzero exit or timeout
def _run(args: list[str], timeout: int, capture: bool = False) -> str:
    # Raw invocation is DEBUG-only noise once things are working; reconcile.py's own before/after lines carry the INFO-level story
    log.debug("+ %s", " ".join(args))
    try:
        proc = subprocess.run(args, timeout=timeout, capture_output=True, text=True)
    except subprocess.TimeoutExpired as exc:
        raise DeployError(f"command timed out after {timeout}s: {' '.join(args)}",
                           stderr=(exc.stderr or "") if isinstance(exc.stderr, str) else "") from exc
    if proc.returncode != 0:
        log.error("command failed (%s): %s\n%s", proc.returncode, " ".join(args), proc.stderr.strip())
        raise DeployError(f"command failed: {' '.join(args)}", stderr=proc.stderr)
    if proc.stdout.strip() and not capture:
        log.debug(proc.stdout.strip())
    return proc.stdout


def _base_args(compose_file: Path, env_file: Path, project: str, project_dir: Path) -> list[str]:
    return [
        "docker", "compose",
        "-f", str(compose_file),
        "--env-file", str(env_file),
        "--project-directory", str(project_dir),
        "-p", project,
    ]


# Validate the stack and return its fully resolved config as JSON, also used to discover network ownership for deploy ordering
def resolve_config(compose_file: Path, env_file: Path, project: str, project_dir: Path, timeout: int) -> str:
    return _run(_base_args(compose_file, env_file, project, project_dir) + ["config", "--format", "json"],
                timeout, capture=True)


def up(compose_file: Path, env_file: Path, project: str, project_dir: Path, *, pull: bool, timeout: int) -> None:
    base = _base_args(compose_file, env_file, project, project_dir)
    if pull:
        _run(base + ["pull", "--quiet"], timeout)
    _run(base + ["up", "-d", "--remove-orphans"], timeout)


def down(compose_file: Path, env_file: Path, project: str, project_dir: Path, timeout: int) -> None:
    _run(_base_args(compose_file, env_file, project, project_dir) + ["down"], timeout)
