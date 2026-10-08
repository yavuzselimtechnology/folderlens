from __future__ import annotations

import asyncio
import unittest

from agentarm.relay import EphemeralRelay, RelayError


class RelayTests(unittest.IsolatedAsyncioTestCase):
    async def test_expired_request_is_not_delivered(self) -> None:
        relay = EphemeralRelay(request_timeout=0.01)
        share_id = "TemporaryWorkspace123"
        token = "bridge-capability"
        relay.register(share_id, token)
        with self.assertRaises(RelayError):
            await relay.request(share_id, {"title": "Expired"})
        self.assertEqual(
            await relay.poll(share_id, token, timeout=0.01), {"command": None}
        )

    async def test_cancelled_request_is_skipped_before_live_request(self) -> None:
        relay = EphemeralRelay(request_timeout=1)
        share_id = "TemporaryWorkspace123"
        token = "bridge-capability"
        relay.register(share_id, token)
        cancelled = asyncio.create_task(relay.request(share_id, {"title": "Cancelled"}))
        await asyncio.sleep(0)
        cancelled.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await cancelled
        live = asyncio.create_task(relay.request(share_id, {"title": "Live"}))
        command = (await relay.poll(share_id, token, timeout=0.1))["command"]
        self.assertEqual(command["payload"]["title"], "Live")
        await relay.respond(share_id, token, command["id"], {"state": "applied"})
        self.assertEqual(await live, {"state": "applied"})

    async def test_inflight_requests_share_the_queue_capacity(self) -> None:
        relay = EphemeralRelay(request_timeout=1)
        share_id = "TemporaryWorkspace123"
        token = "bridge-capability"
        relay.register(share_id, token)
        capacity = relay._bridges[share_id].commands.maxsize
        requests = []
        try:
            for _ in range(capacity):
                requests.append(asyncio.create_task(relay.request(share_id, {})))
                self.assertIsNotNone(
                    (await relay.poll(share_id, token, timeout=0.1))["command"]
                )
            with self.assertRaisesRegex(RelayError, "busy"):
                await relay.request(share_id, {})
        finally:
            for request in requests:
                request.cancel()
            await asyncio.gather(*requests, return_exceptions=True)

    async def test_change_round_trip_is_ephemeral(self) -> None:
        relay = EphemeralRelay(request_timeout=1, idle_ttl=2)
        share_id = "TemporaryWorkspace123"
        token = "bridge-capability"
        relay.register(share_id, token)

        request = asyncio.create_task(
            relay.request(
                share_id,
                {
                    "format": "agentarm.changes.v1",
                    "title": "Test",
                    "operations": [],
                },
            )
        )
        polled = await relay.poll(share_id, token, timeout=0.1)
        command = polled["command"]
        self.assertEqual(command["operation"], "apply_changes")
        await relay.respond(
            share_id,
            token,
            command["id"],
            {"state": "applied"},
        )
        self.assertEqual(await request, {"state": "applied"})

        await relay.disconnect(share_id, token)
        self.assertFalse(relay.is_live(share_id))
        with self.assertRaises(RelayError):
            await relay.poll(share_id, token, timeout=0.01)


if __name__ == "__main__":
    unittest.main()
