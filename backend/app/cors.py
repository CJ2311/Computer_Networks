"""Secure, environment-configurable CORS settings."""

import ipaddress
import os
import re
from urllib.parse import urlsplit


_HOSTNAME_LABEL = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?$")


def _validate_hostname(hostname: str) -> None:
    """Validate an IP address or DNS hostname."""

    try:
        ipaddress.ip_address(hostname)
        return
    except ValueError:
        pass

    if len(hostname) > 253:
        raise ValueError("CORS hostname is too long")

    labels = hostname.split(".")

    if not all(_HOSTNAME_LABEL.fullmatch(label) for label in labels):
        raise ValueError(f"Invalid CORS hostname: {hostname}")


def get_allowed_origins() -> list[str]:
    """Return explicitly configured, validated browser origins."""

    raw = os.getenv("CORS_ALLOWED_ORIGINS", "").strip()

    if not raw:
        return []

    origins: list[str] = []
    seen: set[str] = set()

    for entry in raw.split(","):
        origin = entry.strip()

        if not origin:
            raise ValueError("CORS_ALLOWED_ORIGINS contains an empty origin")

        if "*" in origin:
            raise ValueError("Wildcard CORS origins are not permitted")

        try:
            parsed = urlsplit(origin)

            if parsed.scheme not in {"http", "https"}:
                raise ValueError(f"Invalid CORS origin scheme: {origin}")

            hostname = parsed.hostname

            if not hostname:
                raise ValueError(f"CORS origin must contain a hostname: {origin}")

            if parsed.username is not None or parsed.password is not None:
                raise ValueError("CORS origins must not contain credentials")

            port = parsed.port

            if parsed.path or parsed.query or parsed.fragment:
                raise ValueError(
                    f"CORS origins must not contain paths, queries, or fragments: {origin}"
                )

            _validate_hostname(hostname)

            # IPv6 addresses must use brackets in URLs.
            try:
                is_ipv6 = ipaddress.ip_address(hostname).version == 6
            except ValueError:
                is_ipv6 = False

            authority = f"[{hostname}]" if is_ipv6 else hostname

            if port is not None:
                authority = f"{authority}:{port}"

            canonical_origin = f"{parsed.scheme}://{authority}"

            if origin != canonical_origin:
                raise ValueError(f"Noncanonical CORS origin: {origin}")

        except ValueError as exc:
            raise ValueError(f"Invalid CORS configuration: {exc}") from exc

        if origin in seen:
            raise ValueError(f"Duplicate CORS origin: {origin}")

        seen.add(origin)
        origins.append(origin)

    return origins