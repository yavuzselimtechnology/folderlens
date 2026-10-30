"""Request-scoped HTTP shutdown acknowledgement, without sleeps or process signals."""

from __future__ import annotations

import contextvars
import threading
from dataclasses import dataclass


@dataclass
class ShutdownTicket:
    controller: ShutdownControl
    requested: bool = False


CURRENT_RESPONSE = contextvars.ContextVar("folderlens_shutdown_response", default=None)


class ShutdownControl:
    def __init__(self):
        self.lock = threading.RLock()
        self.stop = None
        self.requested = False
        self.signaled = False

    def bind(self, stop):
        if not callable(stop):
            raise TypeError("Shutdown target must be callable")
        with self.lock:
            if self.stop is not None:
                raise ValueError("Shutdown control is already bound to a server")
            self.stop = stop

    @property
    def available(self) -> bool:
        with self.lock:
            return self.stop is not None and not self.signaled

    def request(self) -> dict:
        ticket = CURRENT_RESPONSE.get()
        with self.lock:
            if self.stop is None or ticket is None or ticket.controller is not self:
                raise ValueError(
                    "Remote shutdown requires the active HTTP shutdown boundary"
                )
            previous = self.requested
            self.requested = True
            # AnyIO copies context variables into its worker, but this ticket is
            # the same mutable object observed by the HTTP response sender.
            ticket.requested = True
            return {
                "shutdown_requested": True,
                "already_requested": previous,
                "stop_after_response": True,
                "accepted_calls": "accepted background calls drain; in-flight direct operations finish",
                "owned_terminal_sessions_stop": True,
                "restart": "The owner must restart FolderLens locally to restore access",
            }

    def response_sent(self, ticket: ShutdownTicket):
        with self.lock:
            if not ticket.requested or self.signaled:
                return
            self.signaled = True
            try:
                self.stop()
            except BaseException:
                self.signaled = False
                raise

    def wrap(self, app):
        async def boundary(scope, receive, send):
            if scope["type"] != "http":
                return await app(scope, receive, send)
            ticket = ShutdownTicket(self)
            context_token = CURRENT_RESPONSE.set(ticket)

            async def acknowledged_send(message):
                await send(message)
                if message["type"] == "http.response.body" and not message.get(
                    "more_body", False
                ):
                    self.response_sent(ticket)

            try:
                return await app(scope, receive, acknowledged_send)
            finally:
                CURRENT_RESPONSE.reset(context_token)

        return boundary
