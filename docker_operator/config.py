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


def _env_optional_int(name: str) -> int | None:
    val = os.environ.get(name)
    if val is None or val == "":
        return None
    try:
        return int(val)
    except ValueError:
        print(f"FATAL: {name} must be an integer, got {val!r}", file=sys.stderr)
        sys.exit(1)


def _env_list(name: str) -> tuple[str, ...]:
    val = os.environ.get(name, "")
    return tuple(s.strip() for s in val.split(",") if s.strip())


# Return the local filesystem path if GIT_REPO_URL is a bind-mounted repo rather than a network URL, so a missing mount fails fast at startup
def local_repo_path(url: str) -> Path | None:
    if url.startswith("file://"):
        return Path(url[len("file://"):])
    if url.startswith(("/", "./", "../")):
        return Path(url)
    return None


# Immutable snapshot of all operator configuration loaded from environment variables
@dataclass(frozen=True)
class Settings:
    webhook_secret: str
    git_repo_url: str
    git_branch: str
    data_dir: Path
    deploy_dir: Path
    deploy_uid: int | None
    deploy_gid: int | None
    compose_subdir: str
    sops_age_key_file: Path | None
    listen_host: str
    listen_port: int
    webhook_path: str
    pull_images: bool
    prune_removed_stacks: bool
    poll_interval_seconds: int
    deploy_timeout_seconds: int
    deploy_max_retries: int
    deploy_retry_delay_seconds: int
    deploy_priority: tuple[str, ...]
    notify_webhook_url: str | None
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

    sops_key_raw = os.environ.get("SOPS_AGE_KEY_FILE") or ""
    sops_key: Path | None = None
    if sops_key_raw:
        sops_key = Path(sops_key_raw)
        if not sops_key.is_file():
            print(f"FATAL: SOPS_AGE_KEY_FILE {sops_key} does not exist", file=sys.stderr)
            sys.exit(1)

    git_repo_url = _env("GIT_REPO_URL", required=True)
    local_repo = local_repo_path(git_repo_url)
    if local_repo is not None and not (local_repo / "HEAD").exists() and not (local_repo / ".git" / "HEAD").exists():
        print(f"FATAL: GIT_REPO_URL {git_repo_url!r} looks like a local path but no git repo "
              f"(no HEAD file) was found there, check the bind mount", file=sys.stderr)
        sys.exit(1)

    return Settings(
        webhook_secret=_env("WEBHOOK_SECRET", required=True),
        git_repo_url=git_repo_url,
        git_branch=_env("GIT_BRANCH", "main"),
        data_dir=data_dir,
        deploy_dir=deploy_dir,
        deploy_uid=_env_optional_int("DEPLOY_UID"),
        deploy_gid=_env_optional_int("DEPLOY_GID"),
        compose_subdir=_env("COMPOSE_SUBDIR", "compose"),
        sops_age_key_file=sops_key,
        listen_host=_env("LISTEN_HOST", "0.0.0.0"),
        listen_port=_env_int("LISTEN_PORT", 8080),
        webhook_path=_env("WEBHOOK_PATH", "/webhook"),
        pull_images=_env_bool("PULL_IMAGES", True),
        prune_removed_stacks=_env_bool("PRUNE_REMOVED_STACKS", False),
        poll_interval_seconds=_env_int("POLL_INTERVAL_SECONDS", 300),
        deploy_timeout_seconds=_env_int("DEPLOY_TIMEOUT_SECONDS", 300),
        deploy_max_retries=_env_int("DEPLOY_MAX_RETRIES", 3),
        deploy_retry_delay_seconds=_env_int("DEPLOY_RETRY_DELAY_SECONDS", 60),
        deploy_priority=_env_list("DEPLOY_PRIORITY"),
        notify_webhook_url=os.environ.get("NOTIFY_WEBHOOK_URL") or None,
        log_level=_env("LOG_LEVEL", "INFO"),
    )
