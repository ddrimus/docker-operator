# Loads and validates operator configuration from environment variables
from __future__ import annotations
import os
import sys
from dataclasses import dataclass
from pathlib import Path


def _env(name: str, default: str | None = None, required: bool = False) -> str:
    val = os.environ.get(name, default)
    if required and not val:
        print(f"FATAL: missing required environment variable {name}", file=sys.stderr)
        sys.exit(1)
    return val or ""


def _env_bool(name: str, default: bool) -> bool:
    val = os.environ.get(name)
    if val is None:
        return default
    return val.strip().lower() in ("1", "true", "yes", "on")


def _env_int(name: str, default: int) -> int:
    val = os.environ.get(name)
    if val is None or val == "":
        return default
    try:
        return int(val)
    except ValueError:
        print(f"FATAL: {name} must be an integer, got {val!r}", file=sys.stderr)
        sys.exit(1)


# Immutable snapshot of all operator configuration loaded from environment variables
@dataclass(frozen=True)
class Settings:
    webhook_secret: str
    git_repo_url: str
    git_branch: str
    data_dir: Path
    deploy_dir: Path
    compose_subdir: str
    listen_host: str
    listen_port: int
    webhook_path: str
    pull_images: bool
    prune_removed_stacks: bool
    poll_interval_seconds: int
    deploy_timeout_seconds: int
    log_level: str

    @property
    def repo_dir(self) -> Path:
        return self.data_dir / "repo"

    @property
    def state_file(self) -> Path:
        return self.data_dir / "state.json"

    @property
    def compose_root(self) -> Path:
        return self.repo_dir / self.compose_subdir


# Read and validate all environment variables into a Settings instance
def load_settings() -> Settings:
    data_dir = Path(_env("DATA_DIR", "/data"))
    deploy_dir_raw = _env("DEPLOY_DIR", "")
    deploy_dir = Path(deploy_dir_raw) if deploy_dir_raw else (data_dir / "deploy")

    git_repo_url = _env("GIT_REPO_URL", required=True)

    return Settings(
        webhook_secret=_env("WEBHOOK_SECRET", required=True),
        git_repo_url=git_repo_url,
        git_branch=_env("GIT_BRANCH", "main"),
        data_dir=data_dir,
        deploy_dir=deploy_dir,
        compose_subdir=_env("COMPOSE_SUBDIR", "compose"),
        listen_host=_env("LISTEN_HOST", "0.0.0.0"),
        listen_port=_env_int("LISTEN_PORT", 8080),
        webhook_path=_env("WEBHOOK_PATH", "/webhook"),
        pull_images=_env_bool("PULL_IMAGES", True),
        prune_removed_stacks=_env_bool("PRUNE_REMOVED_STACKS", False),
        poll_interval_seconds=_env_int("POLL_INTERVAL_SECONDS", 300),
        deploy_timeout_seconds=_env_int("DEPLOY_TIMEOUT_SECONDS", 300),
        log_level=_env("LOG_LEVEL", "INFO"),
    )
