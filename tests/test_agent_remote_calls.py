import asyncio
import base64
import concurrent.futures
import json
import socket
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import CallToolResult, ImageContent, TextContent

from agentarm.mcp_server import create_agent
from agentarm.oauth import READ_SCOPE, WRITE_SCOPE
from agentarm.remote_calls import RETENTION_SECONDS, RemoteCalls
from agentarm.tool_history import ToolHistory
from agentarm.workspace import AgentPolicy, Workspace


def result(value="done"):
    return CallToolResult(
        content=[TextContent(type="text", text=value)],
        structuredContent={"value": value},
    )


class RemoteCatalogTests(unittest.TestCase):
    def setUp(self):
        self.catalog = RemoteCalls()
        self.futures = []
        self.addCleanup(self.finish_fixtures)

    def finish_fixtures(self):
        for future in self.futures:
            if not future.done():
                future.set_result(result())

    def submit(
        self, key="unique-request", arguments=None, owner="owner", required=None
    ):
        def schedule(call_id):
            future = concurrent.futures.Future()
            self.futures.append(future)
            return future

        required = required or {READ_SCOPE}
        return self.catalog.submit(
            owner,
            set(required),
            key,
            "write_file",
            arguments or {"path": "file.txt"},
            required,
            schedule,
        )

    def test_duplicate_requests_reuse_identity_and_reject_changed_arguments(self):
        first = self.submit(arguments={"b": 2, "a": 1})
        duplicate = self.submit(arguments={"a": 1, "b": 2})
        self.assertEqual(first["call_id"], duplicate["call_id"])
        self.assertTrue(duplicate["deduplicated"])
        self.assertEqual(len(self.futures), 1)
        with self.assertRaisesRegex(ValueError, "different"):
            self.submit(arguments={"a": 2})

    def test_owner_and_scopes_filter_status_results_and_listing(self):
        call = self.submit(required={READ_SCOPE, WRITE_SCOPE})
        for owner, scopes in [
            ("other", {READ_SCOPE, WRITE_SCOPE}),
            ("owner", {READ_SCOPE}),
        ]:
            with self.subTest(owner=owner, scopes=scopes), self.assertRaises(KeyError):
                self.catalog.get(call["call_id"], owner, scopes)
            self.assertEqual(self.catalog.list(owner, scopes, 20, 64000)["calls"], [])
        self.futures[0].set_result(result())
        returned = self.catalog.get(call["call_id"], "owner", {READ_SCOPE, WRITE_SCOPE})
        self.assertEqual(returned.structured_content["call"]["status"], "completed")

    def test_native_results_and_explicit_failures_are_preserved(self):
        first = self.submit()
        rich = CallToolResult(
            content=[ImageContent(type="image", data="aGVsbG8=", mimeType="image/png")],
            structuredContent={"image": "metadata"},
            _meta={"hint": "retained"},
        )
        self.catalog.started(first["call_id"])
        self.assertEqual(
            self.catalog.get(
                first["call_id"], "owner", {READ_SCOPE}
            ).structured_content["call"]["status"],
            "executing",
        )
        self.futures[0].set_result(rich)
        received = self.catalog.get(first["call_id"], "owner", {READ_SCOPE})
        self.assertEqual(received.content[1].data, rich.content[0].data)
        self.assertEqual(received.meta, rich.meta)
        self.assertIsNone(self.catalog.calls[first["call_id"]].future)
        second = self.submit("second-request")
        self.futures[1].set_exception(ToolError("known failure"))
        failed = self.catalog.get(second["call_id"], "owner", {READ_SCOPE})
        self.assertTrue(failed.is_error)
        self.assertEqual(failed.structured_content["call"]["status"], "failed")
        self.assertIn("known failure", failed.content[-1].text)

    def test_result_eviction_keeps_idempotent_identity_and_completed_status(self):
        size = len(result("retained").model_dump_json(by_alias=True).encode())
        with patch("agentarm.remote_calls.MAX_RETAINED_BYTES", size):
            first = self.submit()
            self.futures[0].set_result(result("retained"))
            second = self.submit("second-request")
            self.futures[1].set_result(result("retained"))
        evicted = self.catalog.get(first["call_id"], "owner", {READ_SCOPE})
        self.assertFalse(evicted.structured_content["call"]["result_available"])
        self.assertEqual(evicted.structured_content["call"]["status"], "completed")
        self.assertTrue(self.submit()["deduplicated"])
        self.assertEqual(len(self.futures), 2)
        with patch("agentarm.remote_calls.MAX_RESULT_BYTES", 1):
            oversized = self.submit("oversized-request")
            self.futures[2].set_result(result())
        self.assertFalse(
            self.catalog.get(
                oversized["call_id"], "owner", {READ_SCOPE}
            ).structured_content["call"]["result_available"]
        )
        self.assertTrue(
            self.catalog.get(
                second["call_id"], "owner", {READ_SCOPE}
            ).structured_content["call"]["result_available"]
        )

    def test_pending_catalog_quotas_and_expiry_do_not_forget_pending_calls(self):
        with patch("agentarm.remote_calls.MAX_PENDING", 1):
            first = self.submit()
            self.assertTrue(self.submit()["deduplicated"])
            with self.assertRaisesRegex(ValueError, "Pending"):
                self.submit("second-request")
        with patch(
            "agentarm.remote_calls.time.monotonic",
            return_value=time.monotonic() + RETENTION_SECONDS + 1,
        ):
            self.assertFalse(
                self.catalog.get(
                    first["call_id"], "owner", {READ_SCOPE}
                ).structured_content["call"]["finished"]
            )
        self.futures[0].set_result(result())
        with (
            patch("agentarm.remote_calls.MAX_CALLS", 1),
            self.assertRaisesRegex(ValueError, "catalog"),
        ):
            self.submit("second-request")
        with patch(
            "agentarm.remote_calls.time.monotonic",
            return_value=time.monotonic() + RETENTION_SECONDS + 1,
        ):
            with self.assertRaises(KeyError):
                self.catalog.get(first["call_id"], "owner", {READ_SCOPE})
            self.assertFalse(self.submit()["deduplicated"])

    def test_catalog_pagination_and_response_budgets(self):
        identities = [self.submit(f"request-{index}")["call_id"] for index in range(4)]
        page = self.catalog.list("owner", {READ_SCOPE}, 2, 64000)
        self.assertEqual(
            [call["call_id"] for call in page["calls"]], identities[::-1][:2]
        )
        older = self.catalog.list(
            "owner", {READ_SCOPE}, 2, 64000, page["next_before_id"]
        )
        self.assertEqual(
            [call["call_id"] for call in older["calls"]], identities[::-1][2:]
        )
        self.assertFalse(older["has_more"])
        with self.assertRaises(ValueError):
            self.catalog.list("owner", {READ_SCOPE}, 20, 10)

    def test_input_budget_and_failed_scheduling_leave_no_accepted_call(self):
        with (
            patch("agentarm.remote_calls.MAX_ARGUMENT_BYTES", 1),
            self.assertRaises(ValueError),
        ):
            self.submit()
        self.assertEqual(len(self.futures), 0)
        self.assertEqual(self.catalog.calls, {})

        def unavailable(identity):
            raise RuntimeError("No running fixture event loop")

        with self.assertRaises(RuntimeError):
            self.catalog.submit(
                "owner",
                {READ_SCOPE},
                "unscheduled",
                "read_file",
                {},
                {READ_SCOPE},
                unavailable,
            )
        self.assertEqual(self.catalog.calls, {})
        self.assertEqual(self.catalog.keys, {})

    def test_read_result_and_idempotency_remain_bound_to_original_scope_context(self):
        future = concurrent.futures.Future()
        self.futures.append(future)
        call = self.catalog.submit(
            "owner",
            {READ_SCOPE, WRITE_SCOPE},
            "scope-context",
            "get_recent_tool_calls",
            {},
            {READ_SCOPE},
            lambda identity: future,
        )
        future.set_result(result())
        with self.assertRaises(KeyError):
            self.catalog.get(call["call_id"], "owner", {READ_SCOPE})
        with self.assertRaises(PermissionError):
            self.catalog.submit(
                "owner",
                {READ_SCOPE},
                "scope-context",
                "get_recent_tool_calls",
                {},
                {READ_SCOPE},
                lambda identity: future,
            )

    def test_wait_observes_completion_without_polling_or_replaying(self):
        call = self.submit()
        timer = threading.Timer(0.05, lambda: self.futures[0].set_result(result()))
        timer.start()
        self.addCleanup(timer.join)
        received = self.catalog.get(call["call_id"], "owner", {READ_SCOPE}, 2000)
        self.assertTrue(received.structured_content["call"]["finished"])
        self.assertEqual(len(self.futures), 1)


class RemoteCallMCPTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.store = ToolHistory(self.root / "private-history", "content")
        self.addCleanup(self.store.close)
        self.server = create_agent(
            AgentPolicy(self.root, allow_write=True), history_store=self.store
        )

    async def call(self, name, arguments):
        return await self.server.call_tool(name, arguments)

    async def test_slow_write_remains_observable_and_retry_executes_only_once(self):
        entered, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)
        original = Workspace.write_file
        count = []

        def gated(workspace, *args, **kwargs):
            entered.set()
            if not release.wait(5):
                raise TimeoutError("Fixture release was not received")
            count.append(1)
            return original(workspace, *args, **kwargs)

        arguments = {
            "path": "async.txt",
            "content": "once",
            "base_sha256": None,
            "preview": False,
        }
        with patch.object(Workspace, "write_file", gated):
            first = await self.call(
                "start_tool_call",
                {
                    "tool_name": "write_file",
                    "arguments": arguments,
                    "request_id": "one-write",
                },
            )
            call_id = first.structured_content["call_id"]
            self.assertTrue(await asyncio.to_thread(entered.wait, 2))
            status = await asyncio.wait_for(
                self.call("get_tool_result", {"call_id": call_id}), timeout=1
            )
            self.assertEqual(status.structured_content["call"]["status"], "executing")
            duplicate = await asyncio.wait_for(
                self.call(
                    "start_tool_call",
                    {
                        "tool_name": "write_file",
                        "arguments": dict(arguments),
                        "request_id": "one-write",
                    },
                ),
                timeout=1,
            )
            self.assertEqual(duplicate.structured_content["call_id"], call_id)
            arguments["content"] = "mutated caller argument"
            release.set()
            finished = await self.call(
                "get_tool_result", {"call_id": call_id, "wait_ms": 2000}
            )
        self.assertEqual(finished.structured_content["call"]["status"], "completed")
        self.assertEqual((self.root / "async.txt").read_text(), "once")
        self.assertEqual(len(count), 1)
        history = await self.call("get_recent_tool_calls", {})
        history_data = history.structured_content or json.loads(history.content[0].text)
        self.assertEqual([row["tool"] for row in history_data["calls"]], ["write_file"])

    async def test_invalid_arguments_finish_as_failure_and_controls_cannot_recurse(
        self,
    ):
        with self.assertRaises(ToolError):
            await self.call(
                "start_tool_call",
                {
                    "tool_name": "start_tool_call",
                    "arguments": {},
                    "request_id": "bad-recursion",
                },
            )
        started = await self.call(
            "start_tool_call",
            {
                "tool_name": "write_file",
                "arguments": {},
                "request_id": "invalid-schema",
            },
        )
        received = await self.call(
            "get_tool_result",
            {"call_id": started.structured_content["call_id"], "wait_ms": 2000},
        )
        self.assertTrue(received.is_error)
        self.assertEqual(received.structured_content["call"]["status"], "failed")
        self.assertFalse(any(self.root.glob("*.txt")))

    async def test_dynamic_history_result_cannot_cross_into_narrower_connection_scope(
        self,
    ):
        full = SimpleNamespace(
            client_id="same-client", scopes=[READ_SCOPE, WRITE_SCOPE]
        )
        read = SimpleNamespace(client_id="same-client", scopes=[READ_SCOPE])
        auth_context = "mcp.server.auth.middleware.auth_context.get_access_token"
        with patch(auth_context, return_value=full):
            await self.call(
                "write_file",
                {
                    "path": "private-context.txt",
                    "content": "write-scope-secret",
                    "base_sha256": None,
                    "preview": False,
                },
            )
            started = await self.call(
                "start_tool_call",
                {
                    "tool_name": "get_recent_tool_calls",
                    "arguments": {},
                    "request_id": "dynamic-history",
                },
            )
            call_id = started.structured_content["call_id"]
            completed = await self.call(
                "get_tool_result", {"call_id": call_id, "wait_ms": 2000}
            )
            self.assertIn("write-scope-secret", str(completed))
        with patch(auth_context, return_value=read), self.assertRaises(ToolError):
            await self.call("get_tool_result", {"call_id": call_id})

    async def test_registry_close_drains_accepted_work(self):
        catalog = RemoteCalls()
        future = concurrent.futures.Future()
        catalog.submit(
            "owner",
            {READ_SCOPE},
            "close-request",
            "read_file",
            {},
            {READ_SCOPE},
            lambda identity: future,
        )
        asyncio.get_running_loop().call_soon(future.set_result, result())
        await catalog.close()
        self.assertTrue(catalog.closing)
        self.assertEqual(catalog.calls, {})
        self.assertEqual(catalog.result_bytes, 0)
        with self.assertRaisesRegex(ValueError, "shutting down"):
            catalog.submit(
                "owner",
                {READ_SCOPE},
                "next-request",
                "read_file",
                {},
                {READ_SCOPE},
                lambda identity: future,
            )

    async def test_background_image_result_preserves_native_content(self):
        from PIL import Image

        image_path = self.root / "fixture.png"
        Image.new("RGB", (2, 3), "green").save(image_path)
        started = await self.call(
            "start_tool_call",
            {
                "tool_name": "read_image",
                "arguments": {"path": "fixture.png"},
                "request_id": "native-image",
            },
        )
        completed = await self.call(
            "get_tool_result",
            {"call_id": started.structured_content["call_id"], "wait_ms": 2000},
        )
        self.assertEqual(completed.structured_content["call"]["status"], "completed")
        images = [item for item in completed.content if item.type == "image"]
        self.assertEqual(len(images), 1)
        self.assertEqual(base64.b64decode(images[0].data), image_path.read_bytes())

    async def test_real_http_shutdown_drains_an_accepted_active_call(self):
        import httpx2
        import uvicorn

        from folderlens import http_app

        (self.root / "shutdown.txt").write_text("read before exit")
        entered, closing, release, executed = (threading.Event() for _ in range(4))
        original_read, original_close = Workspace.read_file, RemoteCalls.close

        def gated(workspace, *args, **kwargs):
            entered.set()
            if not release.wait(5):
                raise TimeoutError("Fixture release was not received")
            response = original_read(workspace, *args, **kwargs)
            executed.set()
            return response

        async def observed_close(catalog):
            closing.set()
            await original_close(catalog)

        listener = socket.socket()
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
        token = "synthetic-remote-shutdown-test-token-123456789"
        runner = uvicorn.Server(
            uvicorn.Config(
                http_app(self.server, token),
                host="127.0.0.1",
                log_level="error",
                access_log=False,
            )
        )
        thread = threading.Thread(
            target=runner.run, kwargs={"sockets": [listener]}, daemon=True
        )
        try:
            with (
                patch.object(Workspace, "read_file", gated),
                patch.object(RemoteCalls, "close", observed_close),
            ):
                thread.start()
                deadline = time.monotonic() + 5
                while not runner.started:
                    if not thread.is_alive() or time.monotonic() > deadline:
                        self.fail("Fixture server failed to start")
                    await asyncio.sleep(0.01)
                async with httpx2.AsyncClient(trust_env=False, timeout=5) as client:
                    accepted = await client.post(
                        f"http://127.0.0.1:{port}/mcp",
                        headers={
                            "Authorization": "Bearer " + token,
                            "Accept": "application/json, text/event-stream",
                            "MCP-Protocol-Version": "2025-03-26",
                        },
                        json={
                            "jsonrpc": "2.0",
                            "id": 1,
                            "method": "tools/call",
                            "params": {
                                "name": "start_tool_call",
                                "arguments": {
                                    "tool_name": "read_file",
                                    "arguments": {"path": "shutdown.txt"},
                                    "request_id": "shutdown-active-call",
                                },
                            },
                        },
                    )
                self.assertEqual(accepted.status_code, 200, accepted.text)
                self.assertTrue(await asyncio.to_thread(entered.wait, 2))
                runner.should_exit = True
                self.assertTrue(await asyncio.to_thread(closing.wait, 2))
                self.assertTrue(thread.is_alive())
                release.set()
                await asyncio.to_thread(thread.join, 5)
                self.assertFalse(thread.is_alive())
                self.assertTrue(executed.is_set())
        finally:
            release.set()
            runner.should_exit = True
            if thread.is_alive():
                await asyncio.to_thread(thread.join, 5)
            listener.close()
            self.assertFalse(thread.is_alive(), "Fixture server leaked its thread")


if __name__ == "__main__":
    unittest.main()
