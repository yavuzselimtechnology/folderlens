"""HTTP boundary around the SDK's OAuth endpoints and authenticated MCP transport."""

from __future__ import annotations

import asyncio
import base64
import binascii
import json
import time
from collections import deque
from urllib.parse import parse_qs, parse_qsl, unquote, urlencode, urlsplit, urlunsplit

from mcp.server.auth.routes import build_metadata
from starlette.responses import JSONResponse

from .oauth import OwnerOAuthProvider


class OAuthBoundary:
    def __init__(self, app, provider: OwnerOAuthProvider):
        self.app, self.provider = app, provider
        self.hosts = {
            "localhost",
            "127.0.0.1",
            "::1",
            urlsplit(provider.origin).hostname,
        }
        self.auth_times: deque[float] = deque(maxlen=120)
        settings = provider.settings
        self.metadata = build_metadata(
            settings.issuer_url,
            settings.service_documentation_url,
            settings.client_registration_options,
            settings.revocation_options,
        )
        self.metadata.authorization_response_iss_parameter_supported = True
        self.metadata.client_id_metadata_document_supported = False
        self.metadata.token_endpoint_auth_methods_supported = [
            "none",
            "client_secret_post",
            "client_secret_basic",
        ]
        self.metadata.revocation_endpoint_auth_methods_supported = (
            self.metadata.token_endpoint_auth_methods_supported
        )

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        headers = dict(scope.get("headers", []))
        try:
            host = urlsplit("//" + headers.get(b"host", b"").decode("ascii")).hostname
        except (UnicodeError, ValueError):
            host = None
        path = scope.get("path", "")

        async def safe_send(message):
            if message["type"] == "http.response.start":
                result_headers = list(message.get("headers", []))
                if path == "/authorize" and 300 <= message["status"] < 400:
                    updated = []
                    for key, value in result_headers:
                        if key.lower() == b"location":
                            location = urlsplit(value.decode("latin-1"))
                            query = [
                                (key, value)
                                for key, value in parse_qsl(
                                    location.query, keep_blank_values=True
                                )
                                if key != "iss"
                            ]
                            query.append(("iss", self.provider.issuer))
                            value = urlunsplit(
                                location._replace(query=urlencode(query))
                            ).encode("latin-1")
                        updated.append((key, value))
                    result_headers = updated
                result_headers.extend(
                    [
                        (b"cache-control", b"no-store"),
                        (b"pragma", b"no-cache"),
                        (b"referrer-policy", b"no-referrer"),
                        (b"x-content-type-options", b"nosniff"),
                        (
                            b"content-security-policy",
                            b"default-src 'none'; style-src 'unsafe-inline'; form-action 'self'; frame-ancestors 'none'; base-uri 'none'",
                        ),
                    ]
                )
                message = {**message, "headers": result_headers}
            await send(message)

        async def respond(content, status, extra_headers=None):
            await JSONResponse(content, status_code=status, headers=extra_headers)(
                scope, receive, safe_send
            )

        if host not in self.hosts:
            return await respond({"error": "Invalid Host"}, 421)
        if len(scope.get("query_string", b"")) > 16384:
            return await respond({"error": "Query too large"}, 414)
        is_metadata = path in {
            "/.well-known/oauth-authorization-server",
            "/.well-known/oauth-protected-resource",
        }
        metadata_headers = {
            "Access-Control-Allow-Origin": "*",
            "Access-Control-Allow-Methods": "GET, OPTIONS",
        }
        if is_metadata and scope["method"] == "OPTIONS":
            return await respond({}, 200, metadata_headers)
        if is_metadata and scope["method"] != "GET":
            return await respond(
                {"error": "Method not allowed"}, 405, {"Allow": "GET, OPTIONS"}
            )
        if path == "/.well-known/oauth-authorization-server":
            return await respond(
                self.metadata.model_dump(mode="json", exclude_none=True),
                200,
                metadata_headers,
            )
        if path == "/.well-known/oauth-protected-resource":
            return await respond(
                {
                    "resource": self.provider.resource,
                    "authorization_servers": [self.provider.issuer],
                    "scopes_supported": self.provider.scopes,
                },
                200,
                metadata_headers,
            )
        is_auth = path in {
            "/authorize",
            "/register",
            "/token",
            "/revoke",
        } or path.startswith("/consent/")
        if is_auth:
            now = time.monotonic()
            while self.auth_times and self.auth_times[0] < now - 60:
                self.auth_times.popleft()
            if len(self.auth_times) >= self.auth_times.maxlen:
                return await respond(
                    {"error": "Authorization rate limit reached; retry later"}, 429
                )
            self.auth_times.append(now)
        if (is_auth or path == "/mcp") and scope["method"] == "POST":
            body_limit = 65536 if is_auth else 1024 * 1024

            async def read_body():
                body = bytearray()
                while True:
                    message = await receive()
                    if message["type"] == "http.disconnect":
                        raise ValueError("Request disconnected")
                    body.extend(message.get("body", b""))
                    if len(body) > body_limit:
                        raise ValueError("Request exceeds its body budget")
                    if not message.get("more_body", False):
                        return bytes(body)

            try:
                body = await asyncio.wait_for(read_body(), timeout=10)
            except TimeoutError:
                return await respond({"error": "Request body timeout"}, 408)
            except ValueError:
                return await respond({"error": "Invalid or oversized request"}, 413)
            if path in {"/token", "/revoke"}:
                try:
                    params = parse_qs(
                        body.decode("utf-8"), keep_blank_values=True, max_num_fields=32
                    )
                except (UnicodeError, ValueError):
                    return await respond({"error": "invalid_request"}, 400)
                if any(len(values) != 1 for values in params.values()):
                    return await respond(
                        {
                            "error": "invalid_request",
                            "error_description": "Duplicate OAuth parameters",
                        },
                        400,
                    )
                body_changed = False
                authorization = headers.get(b"authorization", b"")
                if authorization.lower().startswith(b"basic "):
                    try:
                        credentials = base64.b64decode(
                            authorization.split(b" ", 1)[1], validate=True
                        ).decode("utf-8")
                        basic_id, _ = credentials.split(":", 1)
                    except (UnicodeError, ValueError, binascii.Error):
                        return await respond({"error": "invalid_client"}, 401)
                    if "client_id" not in params:
                        params["client_id"] = [unquote(basic_id)]
                        body_changed = True
                    # The SDK authenticates and compares the secret itself. Supply
                    # the RFC 6749 Basic client ID even when it was not duplicated in the form.
                    headers[b"authorization"] = (
                        b"Basic " + authorization.split(b" ", 1)[1]
                    )
                if path == "/revoke" and "client_secret" not in params:
                    # SDK 2.3's revocation model requires this nullable body field
                    # even for public/Basic clients. An empty placeholder satisfies
                    # the shape; ClientAuthenticator still verifies the real method/secret.
                    params["client_secret"] = [""]
                    body_changed = True
                if body_changed:
                    body = urlencode(params, doseq=True).encode("utf-8")
                    if len(body) > 65536:
                        return await respond({"error": "invalid_request"}, 413)
                if body_changed or authorization.lower().startswith(b"basic "):
                    headers[b"content-length"] = str(len(body)).encode("ascii")
                    scope = {**scope, "headers": list(headers.items())}
                if path == "/token" and params.get("resource") != [
                    self.provider.resource
                ]:
                    return await respond(
                        {
                            "error": "invalid_target",
                            "error_description": "resource must match the advertised MCP resource",
                        },
                        400,
                    )
            if path == "/mcp":
                try:
                    message = json.loads(body)
                except (ValueError, UnicodeError):
                    message = None
                params = message.get("params") if isinstance(message, dict) else None
                name = params.get("name") if isinstance(params, dict) else None
                if isinstance(name, str) and message.get("method") == "tools/call":
                    required = self.provider.tool_scopes.get(name)
                    authorization = headers.get(b"authorization", b"")
                    if required and authorization.lower().startswith(b"bearer "):
                        try:
                            token = await self.provider.load_access_token(
                                authorization.split(b" ", 1)[1].decode("utf-8")
                            )
                        except UnicodeError:
                            token = None
                        if token is not None and not set(required).issubset(
                            token.scopes
                        ):
                            challenge = (
                                f'Bearer resource_metadata="{self.provider.origin}/.well-known/oauth-protected-resource", '
                                f'error="insufficient_scope", scope="{" ".join(required)}"'
                            )
                            return await respond(
                                {"error": "insufficient_scope"},
                                403,
                                {"WWW-Authenticate": challenge},
                            )
            consumed = False

            async def replay():
                nonlocal consumed
                if not consumed:
                    consumed = True
                    return {"type": "http.request", "body": body, "more_body": False}
                return await receive()

            return await self.app(scope, replay, safe_send)
        return await self.app(scope, receive, safe_send)
