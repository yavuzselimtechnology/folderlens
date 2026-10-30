import asyncio
import json
import os
import socket
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import anyio
import httpx2
from mcp.server.mcpserver.exceptions import ToolError

from agentarm.mcp_server import create_agent
from agentarm.shutdown import ShutdownControl
from agentarm.workspace import AgentPolicy


class ShutdownBoundaryTests(unittest.IsolatedAsyncioTestCase):
    async def test_stop_follows_final_response_send_from_worker_context(self):
        controller = ShutdownControl()
        events = []
        controller.bind(lambda: events.append("stop"))

        async def app(scope, receive, send):
            requested = await anyio.to_thread.run_sync(controller.request)
            self.assertTrue(requested["shutdown_requested"])
            await send({"type": "http.response.start", "status": 200})
            await send(
                {"type": "http.response.body", "body": b"first", "more_body": True}
            )
            self.assertNotIn("stop", events)
            await send({"type": "http.response.body", "body": b"last"})

        async def send(message):
            events.append(message["type"])

        await controller.wrap(app)({"type": "http"}, None, send)
        self.assertEqual(
            events,
            ["http.response.start", "http.response.body", "http.response.body", "stop"],
        )
        self.assertTrue(controller.signaled)
        self.assertFalse(controller.available)
        with self.assertRaises(ValueError):
            controller.request()

    async def test_failed_send_does_not_signal_and_retry_signals_once(self):
        controller = ShutdownControl()
        stopped = []
        controller.bind(lambda: stopped.append(True))

        async def app(scope, receive, send):
            controller.request()
            await send({"type": "http.response.body", "body": b"ack"})

        async def failed_send(message):
            raise ConnectionError("Synthetic connection failure")

        async def sent(message):
            return None

        with self.assertRaises(ConnectionError):
            await controller.wrap(app)({"type": "http"}, None, failed_send)
        self.assertFalse(controller.signaled)
        await controller.wrap(app)({"type": "http"}, None, sent)
        await controller.wrap(app)({"type": "http"}, None, sent)
        self.assertEqual(stopped, [True])

    async def test_unrelated_responses_and_other_controller_cannot_signal(self):
        controller, other = ShutdownControl(), ShutdownControl()
        stopped = []
        controller.bind(lambda: stopped.append(True))
        other.bind(lambda: stopped.append(False))

        async def no_request(scope, receive, send):
            await send({"type": "http.response.body", "body": b"ordinary"})

        async def cross_controller(scope, receive, send):
            with self.assertRaises(ValueError):
                controller.request()

        async def sent(message):
            return None

        await controller.wrap(no_request)({"type": "http"}, None, sent)
        await other.wrap(cross_controller)({"type": "http"}, None, sent)
        self.assertEqual(stopped, [])

    async def test_non_http_lifecycle_is_forwarded_without_a_shutdown_ticket(self):
        controller = ShutdownControl()
        controller.bind(lambda: None)
        received = []

        async def app(scope, receive, send):
            received.append(scope["type"])
            with self.assertRaises(ValueError):
                controller.request()

        await controller.wrap(app)({"type": "lifespan"}, None, None)
        self.assertEqual(received, ["lifespan"])


class ShutdownToolTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)

    async def test_stdio_and_disabled_exec_cannot_shutdown_or_enqueue_shutdown(self):
        for enabled in (False, True):
            server = create_agent(
                AgentPolicy(Path(self.temp.name), allow_exec=enabled), "test-device"
            )
            with self.subTest(exec=enabled), self.assertRaises(ToolError):
                await server.call_tool("shutdown", {"device_id": "test-device"})
            with self.assertRaises(ToolError):
                await server.call_tool(
                    "start_tool_call",
                    {
                        "tool_name": "shutdown",
                        "arguments": {"device_id": "test-device"},
                        "request_id": "cannot-background-shutdown",
                    },
                )

    async def test_binding_is_explicit_and_cannot_silently_switch_servers(self):
        controller = ShutdownControl()
        self.assertFalse(controller.available)
        with self.assertRaises(TypeError):
            controller.bind(None)
        controller.bind(lambda: None)
        with self.assertRaises(ValueError):
            controller.bind(lambda: None)


class ShutdownCLITests(unittest.IsolatedAsyncioTestCase):
    async def test_real_cli_http_reply_arrives_before_clean_exit(self):
        with tempfile.TemporaryDirectory() as directory:
            listener = socket.socket()
            listener.bind(("127.0.0.1", 0))
            port = listener.getsockname()[1]
            listener.close()
            token = "synthetic-shutdown-cli-token-123456789"
            environment = {
                **os.environ,
                "FOLDERLENS_TOKEN": token,
                "FOLDERLENS_BOOTSTRAPPED": "1",
            }
            process = await asyncio.create_subprocess_exec(
                sys.executable,
                str(Path(__file__).resolve().parents[1] / "folderlens.py"),
                "--root",
                directory,
                "--transport",
                "http",
                "--port",
                str(port),
                "--name",
                "shutdown-fixture",
                "--allow-exec",
                env=environment,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
            )
            try:
                async with httpx2.AsyncClient(
                    trust_env=False,
                    timeout=2,
                    headers={
                        "Authorization": "Bearer " + token,
                        "Accept": "application/json, text/event-stream",
                        "MCP-Protocol-Version": "2025-03-26",
                    },
                ) as client:

                    async def call(name, arguments):
                        return await client.post(
                            f"http://127.0.0.1:{port}/mcp",
                            json={
                                "jsonrpc": "2.0",
                                "id": 1,
                                "method": "tools/call",
                                "params": {"name": name, "arguments": arguments},
                            },
                        )

                    deadline = asyncio.get_running_loop().time() + 10
                    while True:
                        try:
                            response = await call("get_config", {})
                            break
                        except (httpx2.ConnectError, httpx2.ConnectTimeout):
                            if (
                                process.returncode is not None
                                or asyncio.get_running_loop().time() > deadline
                            ):
                                self.fail("Fixture CLI did not start its HTTP endpoint")
                            await asyncio.sleep(0.05)
                    self.assertEqual(response.status_code, 200, response.text)
                    config_result = response.json()["result"]
                    config = config_result.get("structuredContent") or json.loads(
                        config_result["content"][0]["text"]
                    )
                    self.assertTrue(config["remote_shutdown_available"])
                    mismatch = await call("shutdown", {"device_id": "different-device"})
                    self.assertTrue(mismatch.json()["result"]["isError"])
                    self.assertIsNone(process.returncode)
                    reply = await call("shutdown", {"device_id": "shutdown-fixture"})
                    self.assertEqual(reply.status_code, 200, reply.text)
                    result = reply.json()["result"]
                    payload = result.get("structuredContent") or json.loads(
                        result["content"][0]["text"]
                    )
                    self.assertTrue(payload["shutdown_requested"])
                    self.assertEqual(await asyncio.wait_for(process.wait(), 5), 0)
                async with httpx2.AsyncClient(
                    trust_env=False, timeout=1
                ) as disconnected:
                    with self.assertRaises(
                        (httpx2.ConnectError, httpx2.ConnectTimeout)
                    ):
                        await disconnected.post(f"http://127.0.0.1:{port}/mcp")
            finally:
                if process.returncode is None:
                    process.terminate()
                    await asyncio.wait_for(process.wait(), 5)
                await asyncio.wait_for(process.communicate(), 5)
