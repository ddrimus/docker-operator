from __future__ import annotations
from pathlib import Path

from docker_operator.stacks import discover_stacks, stack_hash


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


def test_discover_missing_root_logs_a_warning(tmp_path: Path, caplog):
    with caplog.at_level("WARNING"):
        discover_stacks(tmp_path / "does-not-exist")
    assert "does not exist" in caplog.text


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


def test_hash_changes_when_compose_yaml_changes(tmp_path: Path):
    root = tmp_path / "compose"
    root.mkdir()
    d = _write_stack(root, "s", compose="services:\n  a:\n    image: alpine:3.20\n")
    stacks = discover_stacks(root)
    h1 = stack_hash(stacks["s"])
    (d / "compose.yaml").write_text("services:\n  a:\n    image: alpine:3.21\n")
    h2 = stack_hash(discover_stacks(root)["s"])
    assert h1 != h2


def test_hash_changes_when_env_config_changes(tmp_path: Path):
    root = tmp_path / "compose"
    root.mkdir()
    d = _write_stack(root, "s", env_config="FOO=bar\n")
    stacks = discover_stacks(root)
    h1 = stack_hash(stacks["s"])
    (d / ".env.config").write_text("FOO=baz\n")
    h2 = stack_hash(discover_stacks(root)["s"])
    assert h1 != h2


def test_hash_changes_when_secrets_encrypted_changes(tmp_path: Path):
    root = tmp_path / "compose"
    root.mkdir()
    d = _write_stack(root, "s", env_secrets=b"enc-v1")
    stacks = discover_stacks(root)
    h1 = stack_hash(stacks["s"])
    (d / ".env.secrets.encrypted").write_bytes(b"enc-v2")
    h2 = stack_hash(discover_stacks(root)["s"])
    assert h1 != h2


def test_hash_ignores_env_secrets_example(tmp_path: Path):
    # .env.secrets.example is documentation only, never deployed; must not trigger a redeploy when it changes
    root = tmp_path / "compose"
    root.mkdir()
    d = _write_stack(root, "s")
    (d / ".env.secrets.example").write_text("FOO=changeme\n")
    h1 = stack_hash(discover_stacks(root)["s"])
    (d / ".env.secrets.example").write_text("FOO=something-else\n")
    h2 = stack_hash(discover_stacks(root)["s"])
    assert h1 == h2


def test_hash_stable_across_repeated_calls(tmp_path: Path):
    root = tmp_path / "compose"
    root.mkdir()
    _write_stack(root, "s", env_config="FOO=bar\n")
    h1 = stack_hash(discover_stacks(root)["s"])
    h2 = stack_hash(discover_stacks(root)["s"])
    assert h1 == h2


def test_hash_differs_for_missing_vs_present_optional_files(tmp_path: Path):
    root = tmp_path / "compose"
    root.mkdir()
    _write_stack(root, "no-config")
    _write_stack(root, "with-config", env_config="")
    stacks = discover_stacks(root)
    assert stack_hash(stacks["no-config"]) != stack_hash(stacks["with-config"])


# --- Stack.paused ---

def test_stack_not_paused_by_default(tmp_path: Path):
    root = tmp_path / "compose"
    root.mkdir()
    _write_stack(root, "s")
    assert discover_stacks(root)["s"].paused is False


def test_stack_paused_when_marker_file_present(tmp_path: Path):
    root = tmp_path / "compose"
    root.mkdir()
    d = _write_stack(root, "s")
    (d / ".paused").write_text("")
    assert discover_stacks(root)["s"].paused is True


def test_paused_marker_does_not_affect_stack_hash(tmp_path: Path):
    root = tmp_path / "compose"
    root.mkdir()
    d = _write_stack(root, "s")
    h1 = stack_hash(discover_stacks(root)["s"])
    (d / ".paused").write_text("")
    h2 = stack_hash(discover_stacks(root)["s"])
    assert h1 == h2


# --- Stack.depends_on ---

def test_depends_on_empty_when_file_missing(tmp_path: Path):
    root = tmp_path / "compose"
    root.mkdir()
    _write_stack(root, "s")
    assert discover_stacks(root)["s"].depends_on == frozenset()


def test_depends_on_parses_one_name_per_line(tmp_path: Path):
    root = tmp_path / "compose"
    root.mkdir()
    d = _write_stack(root, "s")
    (d / ".depends_on").write_text("traefik\nforgejo\n")
    assert discover_stacks(root)["s"].depends_on == frozenset({"traefik", "forgejo"})


def test_depends_on_ignores_comments_and_blank_lines(tmp_path: Path):
    root = tmp_path / "compose"
    root.mkdir()
    d = _write_stack(root, "s")
    (d / ".depends_on").write_text("# deploy order notes\ntraefik\n\n  \nforgejo  # needs the proxy network too\n")
    assert discover_stacks(root)["s"].depends_on == frozenset({"traefik", "forgejo"})


def test_depends_on_does_not_affect_stack_hash(tmp_path: Path):
    root = tmp_path / "compose"
    root.mkdir()
    d = _write_stack(root, "s")
    h1 = stack_hash(discover_stacks(root)["s"])
    (d / ".depends_on").write_text("traefik\n")
    h2 = stack_hash(discover_stacks(root)["s"])
    assert h1 == h2
