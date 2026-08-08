from __future__ import annotations
from pathlib import Path

from docker_operator.util import chown_recursive, exc_detail


class _WithStderr(Exception):
    def __init__(self, message: str, stderr: str) -> None:
        super().__init__(message)
        self.stderr = stderr


def test_prefers_last_line_of_stderr_over_message():
    exc = _WithStderr("command failed: docker compose up", stderr="line one\nline two\nActual reason here\n")
    assert exc_detail(exc) == "Actual reason here"


def test_falls_back_to_str_when_no_stderr_attribute():
    exc = RuntimeError("plain message")
    assert exc_detail(exc) == "plain message"


def test_falls_back_to_str_when_stderr_is_empty_string():
    exc = _WithStderr("message", stderr="")
    assert exc_detail(exc) == "message"


def test_falls_back_to_str_when_stderr_is_whitespace_only():
    exc = _WithStderr("message", stderr="   \n  \n")
    assert exc_detail(exc) == "message"


def test_falls_back_to_str_when_stderr_is_none():
    exc = _WithStderr("message", stderr=None)
    assert exc_detail(exc) == "message"


def test_single_line_stderr_used_verbatim():
    exc = _WithStderr("message", stderr="  the real reason  \n")
    assert exc_detail(exc) == "the real reason"


# --- chown_recursive ---

def test_chown_recursive_noop_when_both_unset(tmp_path: Path, monkeypatch):
    calls = []
    monkeypatch.setattr("os.chown", lambda *a, **k: calls.append(a))
    (tmp_path / "f.txt").write_text("x")
    chown_recursive(tmp_path, None, None)
    assert calls == []


def test_chown_recursive_covers_dir_and_nested_files(tmp_path: Path, monkeypatch):
    calls = []
    monkeypatch.setattr("os.chown", lambda path, uid, gid: calls.append((str(path), uid, gid)))
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "nested.txt").write_text("x")
    (tmp_path / "top.txt").write_text("x")

    chown_recursive(tmp_path, 1000, 1001)

    paths_chowned = {c[0] for c in calls}
    assert str(tmp_path) in paths_chowned
    assert str(tmp_path / "sub") in paths_chowned
    assert str(tmp_path / "sub" / "nested.txt") in paths_chowned
    assert str(tmp_path / "top.txt") in paths_chowned
    assert all(c[1:] == (1000, 1001) for c in calls)


def test_chown_recursive_leaves_unset_dimension_unchanged(tmp_path: Path, monkeypatch):
    calls = []
    monkeypatch.setattr("os.chown", lambda path, uid, gid: calls.append((uid, gid)))
    chown_recursive(tmp_path, 1000, None)
    assert calls == [(1000, -1)]
