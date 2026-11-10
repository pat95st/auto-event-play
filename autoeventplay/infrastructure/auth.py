"""Local web login: scrypt password hashes and signed session cookies."""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets

from itsdangerous import BadSignature, SignatureExpired, TimestampSigner

SESSION_COOKIE = "aep_session"
SESSION_MAX_AGE = 7 * 24 * 3600

_N, _R, _P = 2**14, 8, 1


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=_N, r=_R, p=_P, dklen=32)
    return "scrypt$%d$%d$%d$%s$%s" % (_N, _R, _P, base64.b64encode(salt).decode(), base64.b64encode(digest).decode())


def verify_password(password: str, stored: str | None) -> bool:
    if not stored:
        # constant-ish time even for unknown users
        hashlib.scrypt(b"x", salt=b"0" * 16, n=_N, r=_R, p=_P, dklen=32)
        return False
    try:
        _, n, r, p, salt_b64, digest_b64 = stored.split("$")
        salt = base64.b64decode(salt_b64)
        expected = base64.b64decode(digest_b64)
        digest = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=int(n), r=int(r), p=int(p), dklen=len(expected))
        return hmac.compare_digest(digest, expected)
    except (ValueError, TypeError):
        return False


class SessionManager:
    def __init__(self, secret_key: str):
        self._signer = TimestampSigner(secret_key, salt="aep-session")

    def issue(self, username: str) -> str:
        return self._signer.sign(username.encode("utf-8")).decode("ascii")

    def verify(self, token: str | None) -> str | None:
        if not token:
            return None
        try:
            return self._signer.unsign(token.encode("ascii"), max_age=SESSION_MAX_AGE).decode("utf-8")
        except (BadSignature, SignatureExpired, UnicodeError):
            return None
