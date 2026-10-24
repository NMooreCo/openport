import time

import jwt
import pytest

from authkit import tokens


def test_round_trip():
    token = tokens.issue("user-42")
    assert isinstance(token, str)
    assert tokens.verify(token) == "user-42"


def test_tampered_token_is_rejected():
    token = tokens.issue("user-42")
    header, payload, signature = token.split(".")
    tampered = ".".join([header, payload, signature[:-2] + ("AA" if signature[-2:] != "AA" else "BB")])
    with pytest.raises(jwt.InvalidTokenError):
        tokens.verify(tampered)


def test_expired_token_is_rejected():
    token = tokens.issue("user-42", ttl_minutes=-1)
    with pytest.raises(jwt.ExpiredSignatureError):
        tokens.verify(token)


def test_wrong_secret_is_rejected(monkeypatch):
    token = tokens.issue("user-42")
    monkeypatch.setattr(tokens, "SECRET", "another-secret")
    with pytest.raises(jwt.InvalidTokenError):
        tokens.verify(token)
