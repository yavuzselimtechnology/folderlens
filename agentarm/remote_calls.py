"""Bounded, in-memory call identities; never retries side-effecting operations."""

from __future__ import annotations

import asyncio
import concurrent.futures
import contextvars
import hashlib
import json
import secrets
import threading
import time
from dataclasses import dataclass, field

from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import CallToolResult, TextContent

MAX_CALLS = 256
MAX_PENDING = 4
RETENTION_SECONDS = 3600
MAX_ARGUMENT_BYTES = 32 * 1024 * 1024
MAX_RESULT_BYTES = 8 * 1024 * 1024
MAX_RETAINED_BYTES = 16 * 1024 * 1024
CURRENT_CALL = contextvars.ContextVar("folderlens_remote_call", default=None)


def fingerprint(tool: str, arguments: dict) -> str:
    hashed = hashlib.sha256(tool.encode("utf-8") + b"\0")
    size = 0
    for chunk in json.JSONEncoder(sort_keys=True, ensure_ascii=True).iterencode(
        arguments
    ):
        size += len(chunk)
        if size > MAX_ARGUMENT_BYTES:
            raise ValueError("Remote call arguments exceed 32 MiB JSON budget")
        hashed.update(chunk.encode("ascii"))
    return hashed.hexdigest()


@dataclass
class RemoteCall:
    call_id: str
    owner: str
    request_id: str
    fingerprint: str
    tool: str
    scopes: frozenset[str]
    created: float
    done: threading.Event = field(default_factory=threading.Event)
    future: concurrent.futures.Future | None = None
    completed: float | None = None
    status: str = "pending"
    result: CallToolResult | None = None
    result_bytes: int = 0
    unavailable: str | None = None


class RemoteCalls:
    def __init__(self):
        self.lock = threading.RLock()
        self.calls: dict[str, RemoteCall] = {}
        self.keys: dict[tuple[str, str], str] = {}
        self.result_bytes = 0
        self.closing = False

    def _cleanup(self):
        now = time.monotonic()
        for call in list(self.calls.values()):
            if call.completed is not None and now - call.completed >= RETENTION_SECONDS:
                self.result_bytes -= call.result_bytes
                del self.calls[call.call_id]
                del self.keys[call.owner, call.request_id]

    @staticmethod
    def _metadata(call: RemoteCall) -> dict:
        return {
            "call_id": call.call_id,
            "tool": call.tool,
            "status": call.status,
            "age_seconds": round(time.monotonic() - call.created, 3),
            "finished": call.completed is not None,
            "result_available": call.result is not None,
            "result_unavailable_reason": call.unavailable,
            "required_scopes": sorted(call.scopes),
            "retry_original_tool": False,
        }

    def submit(
        self,
        owner: str,
        scopes: set[str],
        request_id: str,
        tool: str,
        arguments: dict,
        required: set[str],
        schedule,
    ) -> dict:
        if not 1 <= len(request_id) <= 128 or any(
            ord(char) < 33 or ord(char) > 126 for char in request_id
        ):
            raise ValueError(
                "Use a unique 1..128 printable ASCII request_id without spaces"
            )
        if not required.issubset(scopes):
            raise PermissionError(
                "Background tool requires additional owner-approved scopes"
            )
        identity = fingerprint(tool, arguments)
        with self.lock:
            self._cleanup()
            if self.closing:
                raise ValueError(
                    "Server is shutting down; new remote calls are disabled"
                )
            previous = self.keys.get((owner, request_id))
            if previous is not None:
                call = self.calls[previous]
                if not call.scopes.issubset(scopes):
                    raise PermissionError(
                        "request_id belongs to a broader authorization context"
                    )
                if call.fingerprint != identity:
                    raise ValueError(
                        "request_id already belongs to different tool arguments"
                    )
                return {**self._metadata(call), "deduplicated": True}
            if len(self.calls) >= MAX_CALLS:
                raise ValueError(
                    "Remote call catalog is full; wait for completed call retention expiry"
                )
            if (
                sum(call.completed is None for call in self.calls.values())
                >= MAX_PENDING
            ):
                raise ValueError(
                    "Pending remote call budget reached; inspect existing call IDs"
                )
            call = RemoteCall(
                secrets.token_urlsafe(24),
                owner,
                request_id,
                identity,
                tool,
                # Outputs such as tool history depend on the whole connection's
                # permissions, beyond the target's minimum invocation scope.
                frozenset(scopes),
                time.monotonic(),
            )
            self.calls[call.call_id] = call
            self.keys[owner, request_id] = call.call_id
            try:
                call.future = schedule(call.call_id)
            except BaseException:
                del self.calls[call.call_id]
                del self.keys[owner, request_id]
                raise
            call.future.add_done_callback(
                lambda future: self._finish(call.call_id, future)
            )
            return {**self._metadata(call), "deduplicated": False}

    def started(self, call_id: str):
        with self.lock:
            if call_id in self.calls:
                self.calls[call_id].status = "executing"

    def _finish(self, call_id: str, future):
        try:
            result = future.result()
            if not isinstance(result, CallToolResult):
                raise TypeError(
                    "Background tool returned unsupported input-required result"
                )
        except Exception as exc:  # noqa: BLE001 -- future failures become explicit failed call results.
            message = (
                str(exc)[:1000] if isinstance(exc, ToolError) else type(exc).__name__
            )
            result = CallToolResult(
                content=[TextContent(type="text", text=message)], isError=True
            )
        try:
            encoded_size = len(result.model_dump_json(by_alias=True).encode("utf-8"))
        except (ValueError, TypeError):
            encoded_size = None
        with self.lock:
            call = self.calls[call_id]
            call.completed = time.monotonic()
            call.status = "failed" if result.is_error else "completed"
            call.done.set()
            # Drop the future too: its result would otherwise retain an evicted payload.
            call.future = None
            if encoded_size is None:
                call.unavailable = "result_serialization_failed"
                return
            if encoded_size > MAX_RESULT_BYTES:
                call.unavailable = "result_exceeds_8_MiB_retention_budget"
                return
            for older in self.calls.values():
                if self.result_bytes + encoded_size <= MAX_RETAINED_BYTES:
                    break
                if older.result is not None:
                    self.result_bytes -= older.result_bytes
                    older.result = None
                    older.result_bytes = 0
                    older.unavailable = "aggregate_result_budget_eviction"
            call.result = result
            call.result_bytes = encoded_size
            self.result_bytes += encoded_size

    def _authorized(self, call_id: str, owner: str, scopes: set[str]) -> RemoteCall:
        self._cleanup()
        call = self.calls.get(call_id)
        if call is None or call.owner != owner or not call.scopes.issubset(scopes):
            raise KeyError("Unknown or inaccessible remote call")
        return call

    def get(
        self, call_id: str, owner: str, scopes: set[str], wait_ms: int = 0
    ) -> CallToolResult:
        if type(wait_ms) is not int or not 0 <= wait_ms <= 30000:
            raise ValueError("Use wait_ms 0..30000")
        with self.lock:
            call = self._authorized(call_id, owner, scopes)
            done = call.done
        if wait_ms:
            done.wait(timeout=wait_ms / 1000)
        with self.lock:
            call = self._authorized(call_id, owner, scopes)
            metadata = self._metadata(call)
            result = call.result
            # A historical authentication failure is not a challenge against the
            # current (possibly newly authorized) polling request.
            result_meta = result.meta if result else None
            live_meta = {
                key: value
                for key, value in (result_meta or {}).items()
                if key != "mcp/www_authenticate"
            }
            return CallToolResult(
                content=[
                    TextContent(type="text", text=json.dumps(metadata)),
                    *(result.content if result else []),
                ],
                structuredContent={
                    "call": metadata,
                    "result": result.structured_content if result else None,
                    "result_meta": result_meta,
                },
                isError=result.is_error if result else False,
                _meta=live_meta or None,
            )

    def list(
        self,
        owner: str,
        scopes: set[str],
        limit: int,
        budget: int,
        before_id: str | None = None,
    ) -> dict:
        if type(limit) is not int or not 1 <= limit <= MAX_CALLS:
            raise ValueError("Use limit 1..256")
        with self.lock:
            self._cleanup()
            eligible = [
                call
                for call in reversed(self.calls.values())
                if call.owner == owner and call.scopes.issubset(scopes)
            ]
            if before_id is not None:
                self._authorized(before_id, owner, scopes)
                eligible = eligible[
                    next(
                        index
                        for index, call in enumerate(eligible)
                        if call.call_id == before_id
                    )
                    + 1 :
                ]

            def response(records):
                more = len(records) < len(eligible)
                return {
                    "calls": records,
                    "has_more": more,
                    "next_before_id": records[-1]["call_id"]
                    if more and records
                    else None,
                }

            selected = []
            for call in eligible[:limit]:
                candidate = response([*selected, self._metadata(call)])
                if len(json.dumps(candidate)) > budget:
                    if not selected:
                        raise ValueError(
                            "One remote catalog record exceeds output budget"
                        )
                    break
                selected = candidate["calls"]
            result = response(selected)
            if len(json.dumps(result)) > budget:
                raise ValueError("Remote catalog metadata exceeds output budget")
            return result

    async def close(self):
        with self.lock:
            self.closing = True
            futures = [
                call.future for call in self.calls.values() if call.future is not None
            ]
        # Do not cancel an operation that may already have changed a file. Existing
        # tool/helper timeouts govern completion; shutdown drains accepted calls.
        if futures:
            await asyncio.gather(
                *(asyncio.wrap_future(future) for future in futures),
                return_exceptions=True,
            )
        with self.lock:
            self.calls.clear()
            self.keys.clear()
            self.result_bytes = 0
