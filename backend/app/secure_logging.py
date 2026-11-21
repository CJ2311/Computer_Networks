"""Allowlisted request metadata and redacted Uvicorn diagnostics."""
from collections import deque
import logging
from pathlib import Path
import re
import time
import traceback

from starlette.types import ASGIApp, Receive, Scope, Send


class SuppressRequestDetails(logging.Filter):
    inventory_safe_filter = True

    def filter(self, record):
        # Both protocol access records and ASGI trace scopes contain request URLs.
        return False


def _label(value):
    return re.sub(r"[^A-Za-z0-9_.<>-]", "_", value)[:80]


class SafeServerErrors(logging.Filter):
    inventory_safe_filter = True

    def filter(self, record):
        if record.levelno < logging.INFO:
            return False  # Protocol debug/trace logging may contain headers.
        if record.exc_info:
            kind, _, tb = record.exc_info
            locations = deque(maxlen=12)
            for frame, line in traceback.walk_tb(tb):
                locations.append(f"{_label(Path(frame.f_code.co_filename).name)}:{line}:{_label(frame.f_code.co_name)}")
            # No exception values, chains, source text or frame locals are formatted.
            record.msg = "ASGI exception type=%s locations=%s"
            record.args = (_label(kind.__name__) if kind else "Unknown", ",".join(locations))
        elif record.levelno >= logging.ERROR:
            record.msg = "ASGI server error (details withheld)"
            record.args = ()
        elif isinstance(record.msg, str) and "WebSocket" in record.msg and record.args:
            return False  # Uvicorn also logs WebSocket URLs on uvicorn.error.
        record.exc_info = None
        record.exc_text = None
        record.stack_info = None
        record.__dict__.pop("color_message", None)
        return True


def install_secure_logging():
    """Install before serving; repeated application construction is harmless."""
    for name, filter_type in (("uvicorn.access", SuppressRequestDetails),
                              ("uvicorn.asgi", SuppressRequestDetails),
                              ("uvicorn.error", SafeServerErrors)):
        logger = logging.getLogger(name)
        if not any(getattr(item, "inventory_safe_filter", False) for item in logger.filters):
            logger.addFilter(filter_type())


class SafeAccessLogMiddleware:
    def __init__(self, app: ASGIApp):
        self.app = app
        # Covers common programmatic Uvicorn configuration after importing main.
        install_secure_logging()

    async def __call__(self, scope: Scope, receive: Receive, send: Send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        started = time.monotonic()
        status = 500
        failed = False

        async def record_status(message):
            nonlocal status
            if message["type"] == "http.response.start":
                status = message["status"]
            await send(message)

        try:
            await self.app(scope, receive, record_status)
        except BaseException:
            failed = True
            raise
        finally:
            # Use only registered route templates, never path/query/raw_path,
            # headers, body, response contents or client/proxy identifiers.
            route = getattr(scope.get("route"), "path", None) or "<unmatched>"
            method = scope.get("method")
            if method not in {"GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "TRACE", "CONNECT"}:
                method = "OTHER"
            logging.getLogger("uvicorn.error").info(
                "HTTP request method=%s route=%s status=%d duration_ms=%.2f failed=%s",
                method, route, status, (time.monotonic() - started) * 1000, failed,
            )
