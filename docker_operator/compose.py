# Wraps `docker compose` invocations with logging and structured errors
from __future__ import annotations
import json
import logging
import subprocess
from pathlib import Path
from typing import Callable

log = logging.getLogger("docker_operator.compose")

# Stderr substrings meaning the registry rejected/couldn't serve the pull rather than the image not existing: a fresh `docker login` can resolve these
_AUTH_ERROR_MARKERS = ("unauthorized", "authentication required", "access denied", "404 page not found")


def _looks_like_auth_error(stderr: str) -> bool:
    lowered = stderr.lower()
    return any(marker in lowered for marker in _AUTH_ERROR_MARKERS)


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


# Service names a `docker compose config --format json` result defines, in compose-file order, for per-container status reporting
def service_names(config_json: str) -> list[str]:
    try:
        data = json.loads(config_json)
    except json.JSONDecodeError:
        return []
    services = data.get("services") if isinstance(data, dict) else None
    return list(services) if isinstance(services, dict) else []


# Per-service `docker compose ps` rows for a project, keyed by service name; best-effort, used for per-container status in notifications
def ps(compose_file: Path, env_file: Path, project: str, project_dir: Path, timeout: int) -> dict[str, dict]:
    out = _run(_base_args(compose_file, env_file, project, project_dir) + ["ps", "-a", "--format", "json"],
                timeout, capture=True).strip()
    if not out:
        return {}
    # Compose versions differ: some print a single JSON array, others one JSON object per line
    try:
        parsed = json.loads(out)
        rows = parsed if isinstance(parsed, list) else [parsed]
    except json.JSONDecodeError:
        rows = []
        for line in out.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return {row["Service"]: row for row in rows if isinstance(row, dict) and row.get("Service")}


def up(compose_file: Path, env_file: Path, project: str, project_dir: Path, *, pull: bool, timeout: int,
       retry_login: Callable[[], None] | None = None) -> None:
    base = _base_args(compose_file, env_file, project, project_dir)
    if pull:
        try:
            _run(base + ["pull", "--quiet"], timeout)
        except DeployError as exc:
            if retry_login is None or not _looks_like_auth_error(exc.stderr):
                raise
            log.warning("stack '%s' pull looked like a registry auth failure, retrying docker login and pull once",
                        project)
            retry_login()
            _run(base + ["pull", "--quiet"], timeout)
    _run(base + ["up", "-d", "--remove-orphans"], timeout)


def down(compose_file: Path, env_file: Path, project: str, project_dir: Path, timeout: int) -> None:
    _run(_base_args(compose_file, env_file, project, project_dir) + ["down"], timeout)
