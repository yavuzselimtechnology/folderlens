"""Dependency-free validation for owner-configured HTTPS origins."""

import ipaddress
import re
from urllib.parse import urlsplit


def canonical_origin(value: str) -> str:
    if not isinstance(value, str) or any(
        ord(char) <= 32 or char == "\\" for char in value
    ):
        raise ValueError("Public origin must be a valid HTTPS origin")
    parsed = urlsplit(value)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError(
            "Public origin must use HTTPS without path, credentials, query or fragment"
        )
    if ":" in parsed.hostname:
        host = "[" + str(ipaddress.IPv6Address(parsed.hostname)) + "]"
    else:
        host = parsed.hostname.encode("idna").decode("ascii").lower()
        if len(host) > 253 or any(
            not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", label)
            for label in host.rstrip(".").split(".")
        ):
            raise ValueError("Public origin has an invalid DNS hostname")
    port = parsed.port
    if port is not None and not 1 <= port <= 65535:
        raise ValueError("Public origin has an invalid port")
    return "https://" + host + (f":{port}" if port is not None and port != 443 else "")
