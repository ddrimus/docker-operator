# Discovers deployable stacks and hashes their tracked files for change detection
from __future__ import annotations
import hashlib
import logging
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger("docker_operator.stacks")

COMPOSE_FILE = "compose.yaml"
ENV_CONFIG_FILE = ".env.config"
ENV_SECRETS_FILE = ".env.secrets.encrypted"
TRACKED_FILES = (COMPOSE_FILE, ENV_CONFIG_FILE, ENV_SECRETS_FILE)

# Like TRACKED_FILES, these must be committed to git: git clean -fdx in sync_repo() wipes anything untracked
PAUSE_FILE = ".paused"
DEPENDS_ON_FILE = ".depends_on"


# A discovered stack's name and directory, exposing paths to its tracked files
@dataclass(frozen=True)
class Stack:
    name: str
    path: Path

    @property
    def compose_file(self) -> Path:
        return self.path / COMPOSE_FILE

    @property
    def env_config_file(self) -> Path:
        return self.path / ENV_CONFIG_FILE

    @property
    def env_secrets_file(self) -> Path:
        return self.path / ENV_SECRETS_FILE

    @property
    def paused(self) -> bool:
        return (self.path / PAUSE_FILE).is_file()

    # Names of other stacks this one must deploy after, beyond what's already implied by Docker network ownership
    @property
    def depends_on(self) -> frozenset[str]:
        f = self.path / DEPENDS_ON_FILE
        if not f.is_file():
            return frozenset()
        lines = (line.partition("#")[0].strip() for line in f.read_text().splitlines())
        return frozenset(name for name in lines if name)


# Find stack directories under compose_root that contain a compose.yaml
def discover_stacks(compose_root: Path) -> dict[str, Stack]:
    stacks: dict[str, Stack] = {}
    if not compose_root.is_dir():
        # Could be a genuinely empty repo, or COMPOSE_SUBDIR misconfigured; either way, worth surfacing rather than silently discovering nothing forever
        log.warning("compose root %s does not exist, no stacks discovered", compose_root)
        return stacks
    for entry in sorted(compose_root.iterdir()):
        if not entry.is_dir():
            continue
        if not (entry / COMPOSE_FILE).is_file():
            log.warning("skipping '%s': no %s", entry.name, COMPOSE_FILE)
            continue
        stacks[entry.name] = Stack(name=entry.name, path=entry)
    return stacks


# Hash the files that affect deployment output (.env.secrets.example is excluded, it's documentation only)
def stack_hash(stack: Stack) -> str:
    h = hashlib.sha256()
    for fname in TRACKED_FILES:
        f = stack.path / fname
        h.update(fname.encode() + b"\0")
        h.update(f.read_bytes() if f.is_file() else b"<missing>")
        h.update(b"\0")
    return h.hexdigest()
