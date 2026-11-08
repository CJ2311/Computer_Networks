"""RFC 7662 boundary: IT supplies endpoint, issuer, audience and service credentials."""
import base64
import json
import os
import time
from urllib.parse import urlencode, urlsplit
from urllib.request import Request, build_opener, HTTPSHandler, HTTPRedirectHandler
import ssl

from fastapi import HTTPException


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        raise ValueError("Introspection redirects are forbidden")


def introspect(authorization: str | None) -> tuple[str, str]:
    keys = ("COLLEGE_INTROSPECTION_URL", "COLLEGE_ISSUER", "COLLEGE_AUDIENCE", "COLLEGE_CLIENT_ID", "COLLEGE_CLIENT_SECRET")
    values = [os.getenv(key, "") for key in keys]
    if not all(values):
        raise HTTPException(503, "College authentication is not configured")
    endpoint, issuer, audience, client_id, secret = values
    try:
        parsed = urlsplit(endpoint)
        if parsed.port is not None and not 1 <= parsed.port <= 65535:
            raise ValueError
    except ValueError:
        raise HTTPException(503,"College authentication configuration is invalid") from None
    if (parsed.scheme != "https" or not parsed.hostname or parsed.username is not None or parsed.password is not None
            or parsed.fragment or parsed.query or any("\n" in v or "\r" in v for v in values)):
        raise HTTPException(503, "College authentication configuration is invalid")
    parts = authorization.split(" ") if isinstance(authorization, str) else []
    if len(parts) != 2 or parts[0].lower() != "bearer" or not 1 <= len(parts[1]) <= 8192 or any(ord(c) < 33 or ord(c) > 126 for c in parts[1]):
        raise HTTPException(401, "Valid bearer token required", headers={"WWW-Authenticate": "Bearer"})
    # RFC 6749 client authentication encodes each component before joining.
    from urllib.parse import quote_plus
    credentials = base64.b64encode((quote_plus(client_id) + ":" + quote_plus(secret)).encode()).decode()
    request = Request(endpoint, data=urlencode({"token": parts[1], "token_type_hint": "access_token"}).encode(),
                      headers={"Authorization": "Basic " + credentials, "Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json"}, method="POST")
    try:
        opener = build_opener(HTTPSHandler(context=ssl.create_default_context()), NoRedirect())
        with opener.open(request, timeout=5) as response:
            if response.status != 200:
                raise ValueError("Invalid introspection status")
            raw = response.read(16385)
            if len(raw) > 16384:
                raise ValueError("Oversized introspection response")
            claims = json.loads(raw)
    except Exception:
        # Never render network errors: they may contain URLs, credentials or tokens.
        raise HTTPException(503, "College authentication provider is unavailable") from None
    if not isinstance(claims, dict):
        raise HTTPException(503, "College authentication provider returned an invalid response")
    now = time.time()
    audiences = claims.get("aud")
    audiences = [audiences] if isinstance(audiences, str) else audiences
    expiry, not_before = claims.get("exp"), claims.get("nbf", 0)
    subject = claims.get("sub")
    if (claims.get("active") is not True or claims.get("iss") != issuer
            or not isinstance(audiences, list) or audience not in audiences
            or type(expiry) not in (int, float) or not now < expiry <= 253402300799
            or type(not_before) not in (int, float) or not -62135596800 <= not_before <= now
            or not isinstance(subject, str) or not subject.strip() or len(subject) > 255
            or any(ord(c) < 32 for c in subject)
            or str(claims.get("token_type", "")).lower() != "bearer"):
        raise HTTPException(401, "Invalid college credentials", headers={"WWW-Authenticate": "Bearer"})
    required=set(os.getenv("COLLEGE_REQUIRED_SCOPES","").split())
    scope=claims.get("scope","")
    if required and (not isinstance(scope,str) or not required.issubset(set(scope.split()))):
        raise HTTPException(401,"College token lacks required API scope")
    return issuer, subject
