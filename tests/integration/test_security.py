from __future__ import annotations
import hashlib
import hmac

from docker_operator.security import verify_signature

SECRET = "s3cr3t"
BODY = b'{"ref":"refs/heads/main"}'


def _hex_sig(secret: str, body: bytes) -> str:
    return hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def test_valid_forgejo_style_hex_signature_accepted():
    assert verify_signature(SECRET, BODY, _hex_sig(SECRET, BODY)) is True


def test_valid_github_style_prefixed_signature_accepted():
    assert verify_signature(SECRET, BODY, "sha256=" + _hex_sig(SECRET, BODY)) is True


def test_prefix_is_case_insensitive():
    assert verify_signature(SECRET, BODY, "SHA256=" + _hex_sig(SECRET, BODY)) is True


def test_wrong_secret_rejected():
    assert verify_signature(SECRET, BODY, _hex_sig("wrong-secret", BODY)) is False


def test_tampered_body_rejected():
    sig = _hex_sig(SECRET, BODY)
    assert verify_signature(SECRET, b'{"ref":"refs/heads/evil"}', sig) is False


def test_missing_header_rejected():
    assert verify_signature(SECRET, BODY, None) is False


def test_empty_header_rejected():
    assert verify_signature(SECRET, BODY, "") is False


def test_garbage_header_does_not_raise():
    # hmac.compare_digest must not raise on malformed/short input: a crashed handler thread is worse than a rejected request
    assert verify_signature(SECRET, BODY, "not-hex-at-all!!") is False
    assert verify_signature(SECRET, BODY, "sha256=") is False
    assert verify_signature(SECRET, BODY, "🤖" * 20) is False


def test_whitespace_around_header_is_stripped():
    assert verify_signature(SECRET, BODY, "  " + _hex_sig(SECRET, BODY) + "  ") is True
