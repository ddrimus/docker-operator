# Discovers deployable stacks under the compose root
from __future__ import annotations
import logging
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger("docker_operator.stacks")

COMPOSE_FILE = "compose.yaml"
ENV_CONFIG_FILE = ".env.config"
ENV_SECRETS_FILE = ".env.secrets.encrypted"


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


# Find stack directories under compose_root that contain a compose.yaml
def discover_stacks(compose_root: Path) -> dict[str, Stack]:
    stacks: dict[str, Stack] = {}
    if not compose_root.is_dir():
        return stacks
    for entry in sorted(compose_root.iterdir()):
        if not entry.is_dir():
            continue
        if not (entry / COMPOSE_FILE).is_file():
            log.warning("skipping '%s': no %s", entry.name, COMPOSE_FILE)
            continue
        stacks[entry.name] = Stack(name=entry.name, path=entry)
    return stacks
