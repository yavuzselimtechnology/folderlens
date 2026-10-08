from __future__ import annotations

import asyncio
import hashlib
import hmac
import time
from dataclasses import dataclass, field
from typing import Any
from uuid import uuid4

from .paths import validate_share_id


class RelayError(ValueError):
    pass


@dataclass(slots=True)
class Bridge:
    token_hash: str
    last_seen: float
    commands: asyncio.Queue[dict[str, Any]] = field(
        default_factory=lambda: asyncio.Queue(maxsize=16)
    )
    pending: dict[str, asyncio.Future[Any]] = field(default_factory=dict)


class EphemeralRelay:
    """Route bounded change requests to a live browser without persistence."""

    def __init__(self, *, request_timeout: float = 90.0, idle_ttl: float = 45.0):
        self.request_timeout = request_timeout
        self.idle_ttl = idle_ttl
        self._bridges: dict[str, Bridge] = {}

    def register(self, share_id: str, token: str) -> None:
        workspace = validate_share_id(share_id)
        previous = self._bridges.pop(workspace, None)
        if previous:
            _cancel_bridge(previous, "Local bridge replaced")
        self._bridges[workspace] = Bridge(
            token_hash=_hash(token),
            last_seen=time.monotonic(),
        )

    async def disconnect(self, share_id: str, token: str) -> None:
        workspace = validate_share_id(share_id)
        bridge = self._authorized(workspace, token)
        self._bridges.pop(workspace, None)
        _cancel_bridge(bridge, "Local bridge disconnected")

    async def poll(
        self, share_id: str, token: str, timeout: float = 25.0
    ) -> dict[str, Any]:
        bridge = self._authorized(validate_share_id(share_id), token)
        bridge.last_seen = time.monotonic()
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return {"command": None}
            try:
                command = await asyncio.wait_for(
                    bridge.commands.get(), timeout=remaining
                )
            except TimeoutError:
                return {"command": None}
            future = bridge.pending.get(command["id"])
            expires_at = command.pop("expires_at")
            # A timed-out/cancelled caller must never cause a later local write.
            if future is None or future.done() or time.monotonic() >= expires_at:
                continue
            bridge.last_seen = time.monotonic()
            return {"command": command}

    async def respond(
        self,
        share_id: str,
        token: str,
        request_id: str,
        result: Any = None,
        error: str | None = None,
    ) -> None:
        bridge = self._authorized(validate_share_id(share_id), token)
        bridge.last_seen = time.monotonic()
        future = bridge.pending.get(request_id)
        if not future or future.done():
            raise RelayError("Change request expired or was already answered")
        if error:
            future.set_exception(RelayError(error[:1000]))
        else:
            future.set_result(result)

    async def request(self, share_id: str, payload: dict[str, Any]) -> Any:
        workspace = validate_share_id(share_id)
        bridge = self._live_bridge(workspace)
        if len(bridge.pending) >= bridge.commands.maxsize:
            raise RelayError("Local bridge is busy; retry shortly")
        request_id = str(uuid4())
        future = asyncio.get_running_loop().create_future()
        bridge.pending[request_id] = future
        command = {
            "id": request_id,
            "operation": "apply_changes",
            "payload": payload,
            "expires_at": time.monotonic() + self.request_timeout,
        }
        try:
            bridge.commands.put_nowait(command)
        except asyncio.QueueFull as exc:
            bridge.pending.pop(request_id, None)
            raise RelayError("Local bridge is busy; retry shortly") from exc
        try:
            return await asyncio.wait_for(future, timeout=self.request_timeout)
        except TimeoutError as exc:
            raise RelayError(
                "The local folder did not answer. Keep its AgentArm tab open."
            ) from exc
        finally:
            bridge.pending.pop(request_id, None)

    def is_live(self, share_id: str) -> bool:
        try:
            bridge = self._bridges.get(validate_share_id(share_id))
        except ValueError:
            return False
        return bool(bridge and time.monotonic() - bridge.last_seen <= self.idle_ttl)

    def forget(self, share_id: str) -> None:
        bridge = self._bridges.pop(share_id, None)
        if bridge:
            _cancel_bridge(bridge, "Workspace expired")

    def cleanup(self) -> None:
        cutoff = time.monotonic() - max(120.0, self.idle_ttl * 4)
        for share_id, bridge in list(self._bridges.items()):
            if bridge.last_seen < cutoff:
                self.forget(share_id)

    def _authorized(self, share_id: str, token: str) -> Bridge:
        bridge = self._bridges.get(share_id)
        if not bridge or not hmac.compare_digest(bridge.token_hash, _hash(token)):
            raise RelayError("Invalid or expired local bridge")
        return bridge

    def _live_bridge(self, share_id: str) -> Bridge:
        bridge = self._bridges.get(share_id)
        if not bridge or time.monotonic() - bridge.last_seen > self.idle_ttl:
            raise RelayError(
                "The workspace is offline. Open AgentArm on the folder owner's device."
            )
        return bridge


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _cancel_bridge(bridge: Bridge, message: str) -> None:
    for future in bridge.pending.values():
        if not future.done():
            future.set_exception(RelayError(message))
    bridge.pending.clear()
