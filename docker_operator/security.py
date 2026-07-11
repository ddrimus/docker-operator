# Verifies webhook signatures and tokens from Forgejo, Gitea, Gogs, GitHub and GitLab
from __future__ import annotations
import hmac
import hashlib


# Constant-time HMAC-SHA256 check of a webhook signature header, tolerating an optional 'sha256=' prefix
def verify_signature(secret: str, body: bytes, signature_header: str | None) -> bool:
    if not signature_header:
        return False
    expected = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    provided = signature_header.strip()
    if provided.lower().startswith("sha256="):
        provided = provided[7:]
    try:
        return hmac.compare_digest(expected, provided)
    except Exception:
        return False


# Constant-time check of a plain secret header, GitLab sends the secret itself instead of signing the body
def verify_token(secret: str, token_header: str | None) -> bool:
    if not token_header:
        return False
    return hmac.compare_digest(secret.encode(), token_header.strip().encode())
