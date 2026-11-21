"""Dedicated high-entropy device/capture credentials; never staff OAuth fallback."""
import base64
from dataclasses import dataclass, field
import hashlib
import hmac
import re
from uuid import UUID

from fastapi import HTTPException, Request


@dataclass(frozen=True)
class ScannerCredential:
    scanner_id: UUID
    digest: str = field(repr=False)


def token_digest(token: str) -> str:
    if not isinstance(token, str) or not re.fullmatch(r"[A-Za-z0-9_-]{43}", token):
        raise HTTPException(401, "Valid scanner or capture credentials required")
    decoded = base64.urlsafe_b64decode(token + "=")
    canonical = base64.urlsafe_b64encode(decoded).rstrip(b"=").decode("ascii")
    if len(decoded) != 32 or not hmac.compare_digest(canonical, token):
        raise HTTPException(401, "Valid scanner or capture credentials required")
    return hashlib.sha256(token.encode("ascii")).hexdigest()


def require_scanner(request: Request) -> ScannerCredential:
    headers = request.headers.getlist("authorization")
    if len(headers) != 1 or request.headers.getlist("x-dev-user"):
        raise HTTPException(401, "Valid scanner credentials required")
    match = re.fullmatch(r"(?i:Scanner) ([0-9a-fA-F-]{36})\.([A-Za-z0-9_-]{43})", headers[0])
    if not match:
        raise HTTPException(401, "Valid scanner credentials required")
    try:
        scanner_id = UUID(match[1])
    except ValueError:
        raise HTTPException(401, "Valid scanner credentials required") from None
    return ScannerCredential(scanner_id, token_digest(match[2]))


def require_capture_secret(request: Request) -> str:
    values = request.headers.getlist("x-scan-session-token")
    if len(values) != 1:
        raise HTTPException(401, "Valid capture credentials required")
    return token_digest(values[0])
