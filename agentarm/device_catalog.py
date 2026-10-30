"""Owner-configured device discovery with bounded authenticated MCP probes."""

from __future__ import annotations

import json
import os
import threading
import time
from dataclasses import dataclass

import anyio
import httpx2
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from mcp.types import CallToolResult, ImageContent, TextContent

from .device_config import ROOT_PREVIEW_CHARS, DeviceConfig
from .oauth import EXEC_SCOPE, WRITE_SCOPE
from .remote_calls import MAX_RESULT_BYTES, fingerprint

PROBE_SECONDS = 3
REFRESH_SECONDS = 12
PROBE_RESPONSE_BYTES = 64 * 1024
STATUS_TTL = 60


class BoundedResponse(httpx2.AsyncByteStream):
    def __init__(self, source, limit):
        self.source, self.limit = source, limit

    async def __aiter__(self):
        used = 0
        async for chunk in self.source:
            used += len(chunk)
            if used > self.limit:
                raise ValueError("Device response exceeds byte budget")
            yield chunk

    async def aclose(self):
        await self.source.aclose()


def response_guard(limit):
    async def guard(response):
        if response.status_code in {401, 403}:
            response.raise_for_status()
        if 300 <= response.status_code < 400:
            raise ValueError("Device endpoint redirects are not permitted")
        if (
            response.headers.get("content-encoding", "identity").casefold()
            != "identity"
        ):
            raise ValueError("Device response compression is not permitted")
        declared = response.headers.get("content-length")
        if declared is not None and (not declared.isdecimal() or int(declared) > limit):
            raise ValueError("Device response length exceeds byte budget")
        response.stream = BoundedResponse(response.stream, limit)

    return guard


@dataclass
class DeviceState:
    status: str = "not_checked"
    checked_at: float | None = None
    checked_monotonic: float | None = None
    name: str | None = None
    allow_write: bool = False
    allow_exec: bool = False
    error_type: str | None = None


def error_status(error):
    grouped = getattr(error, "exceptions", ())
    if isinstance(grouped, tuple) and grouped:
        for nested in grouped:
            status, kind = error_status(nested)
            if status == "authentication_error":
                return status, kind
        return error_status(grouped[0])
    if isinstance(error, httpx2.HTTPStatusError) and error.response.status_code in {
        401,
        403,
    }:
        return "authentication_error", type(error).__name__
    if isinstance(error, (TimeoutError, httpx2.TransportError, OSError)):
        return "unreachable", type(error).__name__
    return "protocol_error", type(error).__name__


class DeviceCatalog:
    def __init__(self, config: DeviceConfig):
        self.config = config
        self.profiles = {profile.id: profile for profile in config.profiles}
        self.states = {identity: DeviceState() for identity in self.profiles}
        self.lock = threading.RLock()
        self._tokens = {}
        for profile in config.profiles:
            token = os.environ.get(profile.token_env, "")
            if 32 <= len(token) <= 4096 and all(
                33 <= ord(char) <= 126 for char in token
            ):
                self._tokens[profile.id] = token
            else:
                self.states[profile.id] = DeviceState(
                    status="credential_missing" if not token else "credential_invalid"
                )

    async def probe(self, identity: str, seconds: float):
        profile = self.profiles[identity]
        token = self._tokens.get(identity)
        if token is None:
            return
        state = DeviceState(checked_at=time.time(), checked_monotonic=time.monotonic())
        try:
            with anyio.fail_after(seconds):
                async with (
                    httpx2.AsyncClient(
                        headers={
                            "Authorization": "Bearer " + token,
                            "Accept-Encoding": "identity",
                        },
                        trust_env=False,
                        follow_redirects=False,
                        timeout=seconds,
                        event_hooks={
                            "response": [response_guard(PROBE_RESPONSE_BYTES)]
                        },
                    ) as client,
                    streamable_http_client(
                        profile.url,
                        http_client=client,
                        terminate_on_close=False,
                        max_sse_event_size=PROBE_RESPONSE_BYTES,
                    ) as streams,
                    ClientSession(streams[0], streams[1]) as session,
                ):
                    await session.initialize()
                    result = await session.call_tool("get_config", {})
                    if result.is_error:
                        state.status = (
                            "authentication_error"
                            if result.meta and "mcp/www_authenticate" in result.meta
                            else "protocol_error"
                        )
                    else:
                        data = result.structured_content or json.loads(
                            result.content[0].text
                        )
                        if not isinstance(data, dict) or any(
                            type(data.get(key)) is not bool
                            for key in ("allow_write", "allow_exec")
                        ):
                            raise ValueError(
                                "Device get_config response is incompatible"
                            )
                        if data.get("device_id") != identity:
                            state.status = "identity_mismatch"
                        elif data.get("root") != profile.root:
                            state.status = "root_mismatch"
                        else:
                            remote_scopes = data.get("oauth_scopes")
                            if remote_scopes is not None and (
                                not isinstance(remote_scopes, list)
                                or any(
                                    not isinstance(scope, str)
                                    for scope in remote_scopes
                                )
                            ):
                                raise ValueError("Device scopes are incompatible")
                            state.status = "online"
                            name = data.get("device", identity)
                            state.name = (
                                name[:255] if isinstance(name, str) else identity
                            )
                            state.allow_write = data["allow_write"] and (
                                remote_scopes is None or WRITE_SCOPE in remote_scopes
                            )
                            state.allow_exec = data["allow_exec"] and (
                                remote_scopes is None or EXEC_SCOPE in remote_scopes
                            )
        except Exception as error:  # noqa: BLE001 -- discovery failures are explicit redacted status, not actions.
            state.status, state.error_type = error_status(error)
        with self.lock:
            self.states[identity] = state

    async def refresh(self, identity: str | None = None) -> dict:
        if identity is not None and identity not in self.profiles:
            raise ValueError("Unknown configured device ID")
        targets = [identity] if identity is not None else list(self.profiles)
        deadline = time.monotonic() + REFRESH_SECONDS
        remaining_ids = []
        for index, selected in enumerate(targets):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                remaining_ids = targets[index:]
                break
            await self.probe(selected, min(PROBE_SECONDS, remaining))
        return {"refresh_complete": not remaining_ids, "unrefreshed_ids": remaining_ids}

    async def call(self, identity, tool_name, arguments, required, authorize, seconds):
        """Forward once, after fresh identity/root and caller authorization checks.

        A missing response after forwarding leaves the side effect uncertain.
        Never retries the POST or returns peer authentication metadata as hub metadata.
        """
        profile = self.profiles[identity]
        fingerprint(tool_name, arguments)
        token = self._tokens.get(identity)
        if token is None:
            raise PermissionError("Configured device credential is unavailable")
        forwarded = False
        try:
            with anyio.fail_after(seconds):
                async with (
                    httpx2.AsyncClient(
                        headers={
                            "Authorization": "Bearer " + token,
                            "Accept-Encoding": "identity",
                        },
                        trust_env=False,
                        follow_redirects=False,
                        timeout=seconds,
                        event_hooks={
                            "response": [response_guard(PROBE_RESPONSE_BYTES)]
                        },
                    ) as client,
                    streamable_http_client(
                        profile.url,
                        http_client=client,
                        terminate_on_close=False,
                        max_sse_event_size=MAX_RESULT_BYTES,
                    ) as streams,
                    ClientSession(streams[0], streams[1]) as session,
                ):
                    await session.initialize()
                    config = await session.call_tool("get_config", {})
                    if config.is_error:
                        raise PermissionError("Device configuration was denied")
                    data = config.structured_content or json.loads(
                        config.content[0].text
                    )
                    if not isinstance(data, dict) or (
                        data.get("device_id") != identity
                        or data.get("root") != profile.root
                    ):
                        raise PermissionError(
                            "Device identity or root no longer matches"
                        )
                    remote_scopes = data.get("oauth_scopes")
                    if any(
                        type(data.get(key)) is not bool
                        for key in ("allow_write", "allow_exec")
                    ) or (
                        remote_scopes is not None
                        and (
                            not isinstance(remote_scopes, list)
                            or any(
                                not isinstance(scope, str) for scope in remote_scopes
                            )
                        )
                    ):
                        raise ValueError("Device capability response is incompatible")
                    for scope, field in (
                        (WRITE_SCOPE, "allow_write"),
                        (EXEC_SCOPE, "allow_exec"),
                    ):
                        if scope in required and (
                            data.get(field) is not True
                            or not getattr(profile, field)
                            or (
                                remote_scopes is not None and scope not in remote_scopes
                            )
                        ):
                            raise PermissionError(
                                "Device permission ceiling denies this tool"
                            )
                    await authorize()
                    client.event_hooks["response"] = [response_guard(MAX_RESULT_BYTES)]
                    forwarded = True
                    response = await session.call_tool(tool_name, arguments)
                    if any(
                        not isinstance(item, (TextContent, ImageContent))
                        for item in response.content
                    ):
                        raise ValueError("Unsupported peer content type")
                    return CallToolResult(
                        content=response.content,
                        structuredContent=response.structured_content,
                        isError=response.is_error,
                    )
        except Exception as error:  # noqa: BLE001 -- redact transport errors and explicitly preserve uncertain outcomes.
            status, kind = error_status(error)
            detail = {
                "device_id": identity,
                "status": "outcome_unknown" if forwarded else "not_forwarded",
                "connection_status": status,
                "error_type": kind,
                "retry_original_tool": False,
                "message": "Inspect actual device state before retrying"
                if forwarded
                else "No target action was sent",
            }
            return CallToolResult(
                content=[TextContent(type="text", text=json.dumps(detail))],
                structuredContent=detail,
                isError=True,
            )

    def metadata(self, may_write: bool, may_exec: bool) -> list[dict]:
        with self.lock:
            result = []
            for identity, profile in self.profiles.items():
                state = self.states[identity]
                stale = (
                    state.checked_monotonic is None
                    or time.monotonic() - state.checked_monotonic > STATUS_TTL
                )
                verified = state.status == "online" and not stale
                result.append(
                    {
                        "id": identity,
                        "name": state.name or identity,
                        "status": state.status,
                        "url": profile.url,
                        "root": profile.root[:ROOT_PREVIEW_CHARS],
                        "root_truncated": len(profile.root) > ROOT_PREVIEW_CHARS,
                        "checked_at": state.checked_at,
                        "stale": stale,
                        "error_type": state.error_type,
                        "allow_write": verified
                        and may_write
                        and profile.allow_write
                        and state.allow_write,
                        "allow_exec": verified
                        and may_exec
                        and profile.allow_exec
                        and state.allow_exec,
                        "configured_write_ceiling": profile.allow_write,
                        "configured_exec_ceiling": profile.allow_exec,
                        "scope": "configured peer; status is a bounded connectivity snapshot",
                    }
                )
            return result

    def close(self):
        with self.lock:
            self._tokens.clear()
            self.states = {
                identity: DeviceState(status="closed") for identity in self.profiles
            }
