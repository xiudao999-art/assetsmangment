"""Validation for HMAC bearer tokens issued by the assets service."""
from __future__ import annotations

import hashlib
import hmac
import time


class TokenVerifier:
    """Validate the existing ``<uid>.<exp>.<signature>`` token format."""

    def __init__(self, secret: str) -> None:
        self._secret = secret.encode()

    def _sign(self, message: str) -> str:
        return hmac.new(self._secret, message.encode(), hashlib.sha256).hexdigest()

    def verify(self, token: str) -> str | None:
        try:
            user_id, expires_at, signature = token.rsplit(".", 2)
        except ValueError:
            return None
        if not hmac.compare_digest(
            signature, self._sign(f"{user_id}.{expires_at}")
        ):
            return None
        try:
            if int(expires_at) < int(time.time()):
                return None
        except ValueError:
            return None
        return user_id

