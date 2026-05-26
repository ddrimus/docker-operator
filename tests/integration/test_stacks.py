from __future__ import annotations
from pathlib import Path

from docker_operator.stacks import discover_stacks


def _write_stack(root: Path, name: str, compose: str = "services: {}\n",
                  env_config: str | None = None, env_secrets: bytes | None = None) -> Path:
    d = root / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "compose.yaml").write_text(compose)
    if env_config is not None:
        (d / ".env.config").write_text(env_config)
    if env_secrets is not None:
        (d / ".env.secrets.encrypted").write_bytes(env_secrets)
    return d


def test_discover_missing_root_returns_empty(tmp_path: Path):
    assert discover_stacks(tmp_path / "does-not-exist") == {}


def test_discover_skips_dirs_without_compose_yaml(tmp_path: Path, caplog):
    root = tmp_path / "compose"
    root.mkdir()
    (root / "not-a-stack").mkdir()
    (root / "not-a-stack" / "README.md").write_text("hi")
    stacks = discover_stacks(root)
    assert stacks == {}


def test_discover_finds_valid_stacks(tmp_path: Path):
    root = tmp_path / "compose"
    root.mkdir()
    _write_stack(root, "traefik")
    _write_stack(root, "forgejo")
    stacks = discover_stacks(root)
    assert set(stacks) == {"traefik", "forgejo"}
    assert stacks["traefik"].compose_file == root / "traefik" / "compose.yaml"


def test_discover_ignores_plain_files_in_compose_root(tmp_path: Path):
    root = tmp_path / "compose"
    root.mkdir()
    (root / "README.md").write_text("not a stack dir")
    _write_stack(root, "real-stack")
    assert set(discover_stacks(root)) == {"real-stack"}
