"""Validation for HMAC bearer tokens issued by the assets service."""
from __future__ import annotations

import hashlib
import hmac
import time


class TokenVerifier:
    """Validate current nonce tokens and legacy three-part access tokens."""

    def __init__(self, secret: str) -> None:
        self._secret = secret.encode()

    def _sign(self, message: str) -> str:
        return hmac.new(self._secret, message.encode(), hashlib.sha256).hexdigest()

    def verify(self, token: str) -> str | None:
        try:
            parts = token.rsplit(".", 3)
            if len(parts) == 4:
                user_id, expires_at, nonce, signature = parts
                message = f"{user_id}.{expires_at}.{nonce}"
            elif len(parts) == 3:
                user_id, expires_at, signature = parts
                message = f"{user_id}.{expires_at}"
            else:
                return None
        except ValueError:
            return None
        if not hmac.compare_digest(
            signature, self._sign(message)
        ):
            return None
        try:
            if int(expires_at) < int(time.time()):
                return None
        except ValueError:
            return None
        return user_id

