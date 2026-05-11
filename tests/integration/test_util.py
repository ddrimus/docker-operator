from __future__ import annotations

from docker_operator.util import exc_detail


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
