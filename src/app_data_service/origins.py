"""Normalize a fixed browser origin without assuming a particular application."""

from urllib.parse import urlsplit


def browser_origin(value):
    if not isinstance(value, str):
        raise ValueError("A fixed HTTPS browser origin is required")
    parsed = urlsplit(value)
    if (
        parsed.scheme != "https"
        and not (parsed.scheme == "http" and parsed.hostname in {"localhost", "127.0.0.1", "::1"})
    ) or (
        not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("A fixed HTTPS browser origin is required")
    host = parsed.hostname.lower()
    host = f"[{host}]" if ":" in host else host
    port = parsed.port
    suffix = f":{port}" if port and port != (443 if parsed.scheme == "https" else 80) else ""
    return f"{parsed.scheme}://{host}{suffix}"
