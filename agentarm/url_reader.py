"""Owner-allowlisted HTTPS reads with IP pinning, no cookies/proxies, bounded cache."""

from __future__ import annotations

import http.client
import ipaddress
import json
import socket
import ssl
import threading
import time
from collections import OrderedDict
from urllib.parse import quote, urljoin, urlsplit

from .workspace import Workspace, digest


def normalize_host(host: str) -> str:
    if not host or any(char in host for char in "/\\@:*?#% \t\r\n"):
        raise ValueError(
            "Allow URL hosts by exact DNS name, without scheme, port or wildcard"
        )
    normalized = host.rstrip(".").encode("idna").decode("ascii").lower()
    if (
        not normalized
        or len(normalized) > 253
        or any(not part or len(part) > 63 for part in normalized.split("."))
    ):
        raise ValueError("Invalid allowed URL hostname")
    return normalized


class URLReader:
    def __init__(
        self,
        workspace: Workspace,
        max_bytes: int = 1024 * 1024,
        max_cached: int = 4,
        cache_ttl: float = 60,
    ):
        self.workspace = workspace
        self.allowed_hosts = frozenset(
            normalize_host(host) for host in workspace.policy.url_hosts
        )
        self.max_bytes = max_bytes
        self.max_cached, self.cache_ttl = max_cached, cache_ttl
        self.cache: OrderedDict[str, tuple[float, dict]] = OrderedDict()

    def validate_url(self, url: str):
        if len(url) > 8192 or any(ord(char) < 32 or ord(char) == 127 for char in url):
            raise ValueError("Invalid URL")
        parsed = urlsplit(url)
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.port not in {None, 443}
            or parsed.fragment
        ):
            raise ValueError("Use HTTPS on port 443 without credentials or fragments")
        host = normalize_host(parsed.hostname)
        if host not in self.allowed_hosts:
            raise PermissionError(
                "URL hostname was not enabled locally with --allow-url-host"
            )
        return parsed, host

    def addresses(self, host: str) -> list[str]:
        addresses = list(
            dict.fromkeys(
                item[4][0]
                for item in socket.getaddrinfo(
                    host, 443, type=socket.SOCK_STREAM, proto=socket.IPPROTO_TCP
                )
            )
        )
        if not addresses:
            raise ValueError("URL host has no address")
        for address in addresses:
            parsed = ipaddress.ip_address(address)
            if not parsed.is_global or parsed.is_multicast or parsed.is_reserved:
                raise PermissionError(
                    "Private, reserved and multicast URL addresses are excluded"
                )
        return addresses

    def connection(self, host: str, deadline: float | None = None):
        addresses = self.addresses(host)
        connection = http.client.HTTPSConnection(
            host, 443, timeout=10, context=ssl.create_default_context()
        )

        def connect(address, timeout=10, source_address=None):
            # Keep the original TLS SNI/certificate hostname while connecting only
            # to already-validated IPs. A second DNS lookup cannot rebind to LAN.
            last_error = None
            for target in addresses:
                remaining = (
                    timeout
                    if deadline is None
                    else min(timeout, deadline - time.monotonic())
                )
                if remaining <= 0:
                    raise TimeoutError("URL connection exceeded the total time budget")
                try:
                    return socket.create_connection(
                        (target, 443), remaining, source_address
                    )
                except OSError as exc:
                    last_error = exc
            raise OSError("Could not connect to the validated URL host") from last_error

        connection._create_connection = connect
        return connection

    def fetch(self, url: str) -> dict:
        current = url
        deadline = time.monotonic() + 30
        for hop in range(4):
            parsed, host = self.validate_url(current)
            if time.monotonic() >= deadline:
                raise TimeoutError("URL read exceeded the total time budget")
            connection = self.connection(host, deadline)
            interrupted = threading.Event()
            active_socket = [None]
            response = None

            def abort(conn=connection, event=interrupted, active=active_socket):
                event.set()
                sock = active[0] or conn.sock
                if sock is not None:
                    try:
                        sock.shutdown(socket.SHUT_RDWR)
                    except OSError:
                        pass
                conn.close()

            timer = threading.Timer(max(0, deadline - time.monotonic()), abort)
            timer.daemon = True
            timer.start()
            try:
                connection.connect()
                active_socket[0] = connection.sock
                if interrupted.is_set():
                    raise TimeoutError("URL connection exceeded the total time budget")
                # If the deadline closes the socket, never auto-reconnect behind it.
                connection.auto_open = False
                request_path = quote(parsed.path or "/", safe="/%:@!$&'()*+,;=-._~")
                if parsed.query:
                    request_path += "?" + quote(
                        parsed.query, safe="/%:@!$&'()*+,;=?-._~"
                    )
                connection.request(
                    "GET",
                    request_path,
                    headers={
                        "User-Agent": "FolderLens-Agent/2",
                        "Accept-Encoding": "identity",
                        "Accept": "text/*, application/json, application/xml, application/pdf",
                    },
                )
                response = connection.getresponse()
                if response.status in {301, 302, 303, 307, 308}:
                    location = response.getheader("Location")
                    if not location or hop == 3:
                        raise ValueError(
                            "Missing redirect target or redirect limit reached"
                        )
                    current = urljoin(current, location)
                    continue
                if response.status != 200:
                    raise ValueError(f"URL returned HTTP {response.status}")
                if (
                    response.getheader("Content-Encoding", "identity").lower()
                    != "identity"
                ):
                    raise ValueError("Compressed URL responses are excluded")
                declared = response.getheader("Content-Length")
                if declared and (
                    not declared.isdigit() or int(declared) > self.max_bytes
                ):
                    raise ValueError("URL response exceeds the 1 MiB read budget")
                data = response.read(self.max_bytes + 1)
                if len(data) > self.max_bytes:
                    raise ValueError("URL response exceeds the 1 MiB read budget")
                if interrupted.is_set():
                    raise TimeoutError("URL read exceeded the total time budget")
                content_type = (
                    response.getheader("Content-Type", "")
                    .split(";", 1)[0]
                    .strip()
                    .lower()
                )
                if content_type == "application/pdf":
                    from .snapshots import _extract_document

                    document = _extract_document(1, "remote.pdf", data)
                    if document["kind"] == "extraction_error":
                        raise ValueError("Remote PDF could not be extracted")
                    content = json.dumps(document, ensure_ascii=False)
                    representation = "extracted_document_json"
                elif content_type.startswith("text/") or content_type in {
                    "application/json",
                    "application/xml",
                    "application/xhtml+xml",
                    "application/javascript",
                }:
                    charset = response.headers.get_content_charset() or "utf-8"
                    try:
                        content = data.decode(charset, errors="strict")
                    except (UnicodeError, LookupError) as exc:
                        raise ValueError(
                            "URL text charset is unsupported or invalid"
                        ) from exc
                    representation = "text_source"
                else:
                    raise ValueError("URL response must be text, JSON, XML or PDF")
                if len(content) > self.max_bytes:
                    raise ValueError("Normalized URL content exceeds cache budget")
                return {
                    "url": current,
                    "sha256": digest(data),
                    "source_bytes": len(data),
                    "content_type": content_type,
                    "representation": representation,
                    "content": content,
                }
            except http.client.HTTPException as exc:
                raise ValueError("HTTPS response failed protocol validation") from exc
            finally:
                timer.cancel()
                if response is not None:
                    response.close()
                connection.close()
        raise ValueError("Redirect limit reached")

    def read(
        self,
        url: str,
        offset: int = 0,
        length: int = 16000,
        expected_sha256: str | None = None,
        refresh: bool = False,
    ) -> dict:
        self.validate_url(url)
        if offset < 0 or not 1 <= length <= self.workspace.policy.max_output_chars:
            raise ValueError("Invalid character window")
        now = time.monotonic()
        for key, (created, _) in list(self.cache.items()):
            if now - created > self.cache_ttl:
                del self.cache[key]
        cached = self.cache.get(url)
        if refresh or cached is None:
            document = self.fetch(url)
            self.cache[url] = (time.monotonic(), document)
            self.cache.move_to_end(url)
            while len(self.cache) > self.max_cached:
                self.cache.popitem(last=False)
        else:
            document = cached[1]
        if expected_sha256 is not None and expected_sha256 != document["sha256"]:
            raise ValueError(
                "URL content changed; restart pagination using the new SHA-256"
            )
        content = document["content"]
        end = min(offset + length, len(content))
        return {
            **document,
            "content": content[offset:end],
            "total_chars": len(content),
            "offset": offset,
            "next_offset": end if end < len(content) else None,
            "cache_ttl_seconds": self.cache_ttl,
        }
