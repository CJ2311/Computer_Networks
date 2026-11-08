"""Opt-in opaque bearer credentials; only SHA-256 digests live in configuration.

Tokens are provisioned out of band, at least 32 random bytes (base64url). This
adapter is deliberately not a password/login, token issuance or OIDC service.
"""
import hashlib
import hmac
import json
import os
import re

from fastapi import HTTPException


def token_mode() -> bool:
    mode = os.getenv("AUTH_MODE", "").strip()
    if mode not in {"", "opaque_token", "college_introspection"}:
        raise HTTPException(503, "Authentication configuration is unavailable")
    return mode == "opaque_token"


def token_identity(authorization: str | None) -> str:
    try:
        mapping = json.loads(os.environ["AUTH_TOKEN_SHA256_MAP"])
        if (not isinstance(mapping, dict) or not mapping or any(
            not isinstance(key, str) or not re.fullmatch(r"[0-9a-f]{64}", key)
            or not isinstance(value, str) or not value.strip() or len(value) > 50
            or value != value.strip() or "\x00" in value for key, value in mapping.items())):
            raise ValueError
    except (KeyError, ValueError, TypeError):
        raise HTTPException(503, "Authentication configuration is unavailable") from None
    parts = authorization.split(" ") if isinstance(authorization, str) else []
    if len(parts) != 2 or parts[0].lower() != "bearer" or not re.fullmatch(r"[A-Za-z0-9_-]{43,256}", parts[1]):
        raise HTTPException(401, "Valid bearer token required", headers={"WWW-Authenticate": "Bearer"})
    digest = hashlib.sha256(parts[1].encode("ascii")).hexdigest()
    for expected, college_id in mapping.items():
        if hmac.compare_digest(digest, expected):
            return college_id
    raise HTTPException(401, "Invalid credentials", headers={"WWW-Authenticate": "Bearer"})
