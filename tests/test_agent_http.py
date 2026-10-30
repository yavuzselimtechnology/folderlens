import asyncio
import json
import socket
import tempfile
import threading
import time
import unittest
from pathlib import Path

import httpx2
import uvicorn
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

from agentarm.mcp_server import create_agent
from agentarm.workspace import AgentPolicy
from folderlens import http_app


class HTTPWireTests(unittest.IsolatedAsyncioTestCase):
    async def test_real_http_sdk_client_reads_writes_and_shuts_down_cleanly(self):
        with tempfile.TemporaryDirectory() as directory:
            server = create_agent(AgentPolicy(Path(directory), allow_write=True))
            app = http_app(server, "synthetic-integration-token-123456789")
            listener = socket.socket()
            listener.bind(("127.0.0.1", 0))
            port = listener.getsockname()[1]
            config = uvicorn.Config(
                app, host="127.0.0.1", log_level="error", access_log=False
            )
            runner = uvicorn.Server(config)
            thread = threading.Thread(
                target=runner.run, kwargs={"sockets": [listener]}, daemon=True
            )
            thread.start()
            try:
                deadline = time.monotonic() + 10
                while not runner.started:
                    if not thread.is_alive() or time.monotonic() > deadline:
                        self.fail("HTTP test server failed to start")
                    await asyncio.sleep(0.02)
                async with (
                    httpx2.AsyncClient(
                        headers={
                            "Authorization": "Bearer synthetic-integration-token-123456789"
                        },
                        timeout=10,
                        trust_env=False,
                    ) as http_client,
                    streamable_http_client(
                        f"http://127.0.0.1:{port}/mcp", http_client=http_client
                    ) as streams,
                    ClientSession(streams[0], streams[1]) as client,
                ):
                    await client.initialize()
                    tools = await client.list_tools()
                    self.assertIn("undo_change", {tool.name for tool in tools.tools})
                    written = await client.call_tool(
                        "write_file",
                        {
                            "path": "http.txt",
                            "content": "over real HTTP",
                            "base_sha256": None,
                            "preview": False,
                        },
                    )
                    self.assertFalse(written.is_error, str(written))
                    started = await client.call_tool(
                        "start_tool_call",
                        {
                            "tool_name": "write_file",
                            "request_id": "http-async-write",
                            "arguments": {
                                "path": "async-http.txt",
                                "content": "over async HTTP",
                                "base_sha256": None,
                                "preview": False,
                            },
                        },
                    )
                    self.assertFalse(started.is_error, str(started))
                    completed = await client.call_tool(
                        "get_tool_result",
                        {
                            "call_id": started.structured_content["call_id"],
                            "wait_ms": 2000,
                        },
                    )
                    self.assertEqual(
                        completed.structured_content["call"]["status"], "completed"
                    )
                    self.assertEqual(
                        (Path(directory) / "async-http.txt").read_text(),
                        "over async HTTP",
                    )
                    content = await client.call_tool("read_file", {"path": "http.txt"})
                    self.assertIn("over real HTTP", content.content[0].text)
                    result = await client.call_tool(
                        "read_file", {"path": "../outside.txt"}
                    )
                    self.assertTrue(result.is_error)
                    await client.call_tool(
                        "set_config_value",
                        {"key": "allow_write", "value": False},
                    )
                    result = await client.call_tool(
                        "write_file",
                        {
                            "path": "blocked.txt",
                            "content": "must not write",
                            "base_sha256": None,
                            "preview": False,
                        },
                    )
                    self.assertTrue(result.is_error)
                    self.assertFalse((Path(directory) / "blocked.txt").exists())
                    resource = await client.read_resource("folderlens://guide")
                    self.assertIn("SHA-256", resource.contents[0].text)
                    result = await client.call_tool("get_config", {})
                    effective = result.structured_content or json.loads(
                        result.content[0].text
                    )
                    self.assertFalse(effective["allow_write"])
            finally:
                runner.should_exit = True
                await asyncio.to_thread(thread.join, 5)
                listener.close()
                self.assertFalse(
                    thread.is_alive(), "HTTP test server leaked its thread"
                )
