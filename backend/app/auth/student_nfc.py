"""Dedicated student-write scanner credentials; no equipment/staff auth fallback."""
import base64
from dataclasses import dataclass, field
import hashlib
import hmac
import json
import os
import re
from uuid import UUID

from fastapi import HTTPException, Request
from backend.app.auth.scanners import token_digest


@dataclass(frozen=True)
class StudentScanner:
    id: UUID
    credential_digest: str = field(repr=False)


def configured_scanners():
    try:
        raw = os.environ.get("STUDENT_NFC_SCANNER_DIGESTS", "{}")
        if len(raw) > 16384:
            raise ValueError
        values = json.loads(raw)
        if not isinstance(values, dict) or len(values) > 100:
            raise ValueError
        result = {}
        for key, digest in values.items():
            identifier = UUID(key)
            if identifier in result or not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
                raise ValueError
            result[identifier] = digest
        return result
    except (ValueError, TypeError, AttributeError):
        raise HTTPException(503, "Student scanner configuration unavailable") from None


def payload_settings():
    try:
        key = os.environ.get("STUDENT_NFC_PAYLOAD_KEY", "")
        digest = token_digest(key)  # canonical independent 32-byte secret
        if digest in configured_scanners().values():
            raise ValueError
        profile = os.environ.get("STUDENT_NFC_WRITE_PROFILE", "")
        if not re.fullmatch(r"[A-Za-z0-9._-]{1,64}", profile):
            raise ValueError
        return base64.urlsafe_b64decode(key + "="), profile
    except (ValueError, HTTPException):
        raise HTTPException(503, "Approved student NFC write profile is not configured") from None


def prepare_payload(nonce, profile):
    key, current_profile = payload_settings()
    if not hmac.compare_digest(profile, current_profile):
        raise HTTPException(409, "Write profile changed; start a fresh registration attempt")
    proof = base64.urlsafe_b64encode(hmac.digest(key, f"CNINV1:{nonce}:{profile}".encode("ascii"), "sha256")).rstrip(b"=").decode("ascii")
    return f"CNINV1:{nonce}:{proof}"


def payload_digest(payload):
    return hashlib.sha256(payload.encode("ascii")).hexdigest()


def require_student_scanner(request: Request):
    headers = request.headers.getlist("authorization")
    if len(headers) != 1 or request.headers.getlist("x-dev-user"):
        raise HTTPException(401, "Student-write scanner credentials required")
    match = re.fullmatch(r"(?i:StudentNFC) ([0-9a-fA-F-]{36})\.([A-Za-z0-9_-]{43})", headers[0])
    if not match:
        raise HTTPException(401, "Student-write scanner credentials required")
    try:
        scanner = UUID(match[1])
        digest = token_digest(match[2])
    except (ValueError, HTTPException):
        raise HTTPException(401, "Student-write scanner credentials required") from None
    expected = configured_scanners().get(scanner)
    if expected is None or not hmac.compare_digest(digest, expected):
        raise HTTPException(401, "Student-write scanner credentials required")
    return StudentScanner(scanner, digest)
