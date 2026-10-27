"""Short-lived signed links, for the upload page and file downloads.

A link carries what it grants (`purpose`, `subject`) and when it expires, signed
with HMAC-SHA256. It can be handed to a person in chat: it grants that one thing,
for a few minutes, and nothing else — the API key is never exposed.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import time


def sign(secret: str, purpose: str, subject: str, minutes: int) -> tuple[str, int]:
    """A token for `purpose` on `subject`, and its expiry (unix seconds)."""
    expires = int(time.time()) + minutes * 60
    return _token(secret, purpose, subject, expires), expires


def verify(secret: str, purpose: str, subject: str, token: str) -> bool:
    try:
        expires_text, _ = token.split(".", 1)
        expires = int(expires_text)
    except ValueError:
        return False
    if expires < time.time():
        return False
    return hmac.compare_digest(token, _token(secret, purpose, subject, expires))


def _token(secret: str, purpose: str, subject: str, expires: int) -> str:
    message = f"{purpose}\n{subject}\n{expires}".encode()
    digest = hmac.new(secret.encode(), message, hashlib.sha256).digest()
    return f"{expires}.{base64.urlsafe_b64encode(digest).decode().rstrip('=')}"
