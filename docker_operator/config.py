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


# Immutable snapshot of all operator configuration loaded from environment variables
@dataclass(frozen=True)
class Settings:
    webhook_secret: str
    git_repo_url: str
    git_branch: str
    data_dir: Path
    deploy_dir: Path
    compose_subdir: str
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
        log_level=_env("LOG_LEVEL", "INFO"),
    )
