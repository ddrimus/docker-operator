from __future__ import annotations

from docker_operator.util import exc_detail, redact


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


# --- redact ---

def test_redact_hides_url_credentials():
    cases = {
        "https://user:s3cr3t@github.com/me/stacks.git": "https://user:***@github.com/me/stacks.git",
        "https://s3cr3t@github.com/me/stacks.git": "https://***@github.com/me/stacks.git",
        "https://user:@github.com/me/stacks.git": "https://user:***@github.com/me/stacks.git",
        "fatal: unable to access 'https://oauth2:s3cr3t@gitlab.com/me/stacks.git/': 403":
            "fatal: unable to access 'https://oauth2:***@gitlab.com/me/stacks.git/': 403",
    }
    for text, expected in cases.items():
        assert redact(text) == expected


def test_redact_leaves_urls_without_credentials_alone():
    # ssh URLs never carry a password, the user there is just the login name
    for text in ("https://github.com/me/stacks.git", "git@github.com:me/stacks.git", "/repository/stacks",
                 "file:///repository/stacks", "ssh://git@github.com/me/stacks.git", "git+ssh://git@example.com/stacks.git"):
        assert redact(text) == text


def test_redact_covers_several_urls_and_an_at_sign_in_the_password():
    text = "from https://u:p@ss@a.example/x to https://t@b.example/y"
    assert redact(text) == "from https://u:***@a.example/x to https://***@b.example/y"


def test_exc_detail_hides_credentials_in_stderr():
    exc = _WithStderr("failed", "fatal: unable to access 'https://user:s3cr3t@github.com/me/stacks.git/': 403")
    assert "s3cr3t" not in exc_detail(exc)


def test_exc_detail_hides_credentials_in_the_failed_command():
    # Without stderr, str() of a CalledProcessError is the whole command, clone URL included
    import subprocess
    exc = subprocess.CalledProcessError(128, ["git", "clone", "https://user:s3cr3t@github.com/me/stacks.git"])
    assert "s3cr3t" not in exc_detail(exc)
    assert "https://user:***@github.com/me/stacks.git" in exc_detail(exc)
