"""Bound HTTP bodies before routing, authentication queries or JSON parsing."""
import os

from starlette.datastructures import Headers
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send


DEFAULT_MAX_REQUEST_BODY_BYTES = 8 * 1024 * 1024


def get_max_request_body_bytes() -> int:
    raw = os.getenv("INVENTORY_MAX_REQUEST_BODY_BYTES", str(DEFAULT_MAX_REQUEST_BODY_BYTES)).strip()
    try:
        if not raw.isascii() or not raw.isdecimal() or int(raw) < 1:
            raise ValueError
        return int(raw)
    except ValueError:
        raise ValueError("INVENTORY_MAX_REQUEST_BODY_BYTES must be a positive integer") from None


class RequestSizeLimitMiddleware:
    def __init__(self, app: ASGIApp, max_body_bytes: int, allowed_origins=(), path_limits=None):
        if type(max_body_bytes) is not int or max_body_bytes < 1:
            raise ValueError("max_body_bytes must be a positive integer")
        self.app = app
        self.max_body_bytes = max_body_bytes
        self.allowed_origins = frozenset(allowed_origins)
        self.path_limits = dict(path_limits or {})
        if any(type(value) is not int or value < 1 for value in self.path_limits.values()):
            raise ValueError("Path body limits must be positive integers")

    async def _reject(self, scope, receive, send, status_code, detail):
        origin = Headers(scope=scope).get("origin")
        headers = {}
        if origin in self.allowed_origins:
            headers = {"Access-Control-Allow-Origin": origin, "Vary": "Origin"}
        await JSONResponse({"detail": detail}, status_code=status_code, headers=headers)(scope, receive, send)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        path = scope.get("path", "")
        root = scope.get("root_path", "")
        if root and (path == root or path.startswith(root + "/")):
            path = path[len(root):]
        limit = min(self.max_body_bytes, self.path_limits.get(path.rstrip("/"), self.max_body_bytes))
        lengths = [value.strip() for name, value in scope.get("headers", [])
                   if name.lower() == b"content-length"]
        if len(lengths) > 1 or (lengths and not lengths[0].isdigit()):
            await self._reject(scope, receive, send, 400, "Invalid Content-Length")
            return
        if lengths:
            declared = lengths[0].lstrip(b"0") or b"0"
            maximum = str(limit).encode("ascii")
            # Compare decimal strings so arbitrarily large headers cannot overflow int().
            if len(declared) > len(maximum) or (len(declared) == len(maximum) and declared > maximum):
                await self._reject(scope, receive, send, 413, "Request body too large")
                return

        body = bytearray()
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                return
            chunk = message.get("body", b"")
            if len(body) + len(chunk) > limit:
                await self._reject(scope, receive, send, 413, "Request body too large")
                return
            body.extend(chunk)
            if not message.get("more_body", False):
                break

        # One bounded buffer avoids per-chunk retention for fragmented requests.
        request_body = bytes(body)
        del body
        replayed = False

        async def replay_receive():
            nonlocal replayed
            if not replayed:
                replayed = True
                return {"type": "http.request", "body": request_body, "more_body": False}
            return await receive()

        await self.app(scope, replay_receive, send)
