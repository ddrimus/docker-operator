from __future__ import annotations
import stat
import subprocess
from pathlib import Path

import pytest

from docker_operator.secrets import render_env_file
from docker_operator.stacks import Stack
from conftest import encrypt_dotenv


def _stack(root: Path, name: str) -> Stack:
    (root / name).mkdir(parents=True, exist_ok=True)
    return Stack(name=name, path=root / name)


def test_renders_config_only_when_no_secrets_file(tmp_path: Path):
    stk = _stack(tmp_path, "s")
    stk.env_config_file.write_text("FOO=bar\nBAZ=qux\n")
    dest = tmp_path / "out.env"

    render_env_file(stk, None, dest)

    content = dest.read_text()
    assert "FOO=bar" in content
    assert "BAZ=qux" in content
    assert "from .env.secrets.encrypted" not in content


def test_renders_empty_when_no_files_at_all(tmp_path: Path):
    stk = _stack(tmp_path, "s")
    dest = tmp_path / "out.env"
    render_env_file(stk, None, dest)
    assert dest.read_text() == ""


def test_dest_file_has_0600_permissions(tmp_path: Path):
    stk = _stack(tmp_path, "s")
    stk.env_config_file.write_text("FOO=bar\n")
    dest = tmp_path / "out.env"
    render_env_file(stk, None, dest)
    mode = stat.S_IMODE(dest.stat().st_mode)
    assert mode == 0o600


def test_missing_age_key_but_secrets_file_present_raises(tmp_path: Path):
    stk = _stack(tmp_path, "s")
    stk.env_secrets_file.write_bytes(b"doesnt-matter-not-reached")
    dest = tmp_path / "out.env"
    with pytest.raises(RuntimeError, match="SOPS_AGE_KEY_FILE"):
        render_env_file(stk, None, dest)


def test_real_sops_decrypt_and_config_concatenation_order(tmp_path: Path, age_key):
    key_file, pub = age_key
    stk = _stack(tmp_path, "s")
    stk.env_config_file.write_text("SHARED=from-config\nCONFIG_ONLY=yes\n")
    stk.env_secrets_file.write_bytes(
        encrypt_dotenv("SHARED=from-secret\nSECRET_ONLY=hunter2\n", key_file, pub, tmp_path)
    )
    dest = tmp_path / "out.env"

    render_env_file(stk, key_file, dest)
    content = dest.read_text()

    assert "CONFIG_ONLY=yes" in content
    assert "SECRET_ONLY=hunter2" in content
    # Secrets come after config; compose's env-file parser takes the last definition of a duplicate key, giving secrets precedence
    assert content.index("SHARED=from-secret") > content.index("SHARED=from-config")


def test_wrong_age_key_fails_to_decrypt(tmp_path: Path, age_key):
    key_file, pub = age_key
    # A second, different key: simulates the encrypted file targeting a different recipient than the one we have
    other_key_file = tmp_path / "other.key"
    proc = subprocess.run(["age-keygen", "-o", str(other_key_file)], capture_output=True, text=True, check=True)
    other_pub = next(l.split(":", 1)[1].strip() for l in proc.stderr.splitlines() if "public key" in l.lower())
    assert other_pub != pub

    stk = _stack(tmp_path, "s")
    stk.env_secrets_file.write_bytes(encrypt_dotenv("SECRET=x\n", key_file, pub, tmp_path))
    dest = tmp_path / "out.env"

    with pytest.raises(subprocess.CalledProcessError):
        render_env_file(stk, other_key_file, dest)


def test_dest_not_left_behind_on_decrypt_failure(tmp_path: Path, age_key):
    key_file, _ = age_key
    stk = _stack(tmp_path, "s")
    stk.env_secrets_file.write_bytes(b"not even valid sops output")
    dest = tmp_path / "out.env"
    with pytest.raises(subprocess.CalledProcessError):
        render_env_file(stk, key_file, dest)
    assert not dest.exists()
    assert not dest.with_name(dest.name + ".tmp").exists()


def test_secrets_only_no_config_file(tmp_path: Path, age_key):
    key_file, pub = age_key
    stk = _stack(tmp_path, "s")
    stk.env_secrets_file.write_bytes(encrypt_dotenv("ONLY=secret\n", key_file, pub, tmp_path))
    dest = tmp_path / "out.env"
    render_env_file(stk, key_file, dest)
    assert "ONLY=secret" in dest.read_text()


def test_overwrites_previous_content_of_dest(tmp_path: Path):
    stk = _stack(tmp_path, "s")
    dest = tmp_path / "out.env"
    dest.write_text("STALE=leftover-from-a-previous-render\n")
    stk.env_config_file.write_text("FRESH=value\n")
    render_env_file(stk, None, dest)
    content = dest.read_text()
    assert "FRESH=value" in content
    assert "STALE" not in content


def test_sops_timeout_propagates_and_is_not_swallowed(tmp_path: Path, age_key):
    import subprocess
    from unittest.mock import patch
    key_file, _ = age_key
    stk = _stack(tmp_path, "s")
    stk.env_secrets_file.write_bytes(b"irrelevant, subprocess is mocked")
    dest = tmp_path / "out.env"

    def _raise_timeout(*a, **k):
        raise subprocess.TimeoutExpired(cmd="sops", timeout=30)

    with patch("subprocess.run", side_effect=_raise_timeout):
        with pytest.raises(subprocess.TimeoutExpired):
            render_env_file(stk, key_file, dest)
    assert not dest.exists()


def test_write_failure_cleans_up_tmp_file(tmp_path: Path):
    from unittest.mock import patch, mock_open
    stk = _stack(tmp_path, "s")
    stk.env_config_file.write_text("FOO=bar\n")
    dest = tmp_path / "out.env"

    m = mock_open()
    m.return_value.write.side_effect = OSError("disk full")
    with patch("os.fdopen", m):
        with pytest.raises(OSError):
            render_env_file(stk, None, dest)

    assert not dest.exists()
    assert not (tmp_path / "out.env.tmp").exists()
