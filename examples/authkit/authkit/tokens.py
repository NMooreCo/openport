"""Signed session tokens (JWT, HS256)."""

import os
from datetime import datetime, timedelta, timezone

import jwt

SECRET = os.getenv("AUTHKIT_SECRET", "dev-only-secret")


def issue(user_id: str, ttl_minutes: int = 30) -> str:
    """A signed token for `user_id` that expires after `ttl_minutes`."""
    payload = {"sub": user_id, "exp": datetime.now(timezone.utc) + timedelta(minutes=ttl_minutes)}
    token = jwt.encode(payload, SECRET, algorithm="HS256")
    return token.decode("utf-8")


def verify(token: str) -> str:
    """The user ID in a valid token. Raises jwt.InvalidTokenError for bad, tampered or expired tokens."""
    payload = jwt.decode(token, SECRET)
    return payload["sub"]
