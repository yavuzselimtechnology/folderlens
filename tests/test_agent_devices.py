import asyncio
import hashlib
import io
import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from contextlib import redirect_stderr
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx2
import uvicorn
from mcp.server.mcpserver.exceptions import ToolError
from mcp.shared.auth import OAuthClientInformationFull

from agentarm.device_catalog import BoundedResponse, DeviceCatalog, response_guard
from agentarm.device_config import MAX_CONFIG_BYTES, load_devices
from agentarm.mcp_server import create_agent
from agentarm.oauth import (
    EXEC_SCOPE,
    READ_SCOPE,
    WRITE_SCOPE,
    OwnerOAuthProvider,
    policy_binding,
)
from agentarm.tool_history import ToolHistory
from agentarm.workspace import AgentPolicy
from folderlens import http_app

TOKEN_ENV = "FOLDERLENS_PEER_FIXTURE_TOKEN"
TOKEN = "synthetic-device-fixture-bearer-token-123456789"


class DeviceConfigTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.path = self.root / "devices.json"

    def row(self, **changes):
        return {
            "id": "peer",
            "url": "https://peer.example/mcp",
            "root": str(self.root),
            "token_env": TOKEN_ENV,
            **changes,
        }

    def write(self, rows):
        self.path.write_text(
            json.dumps({"version": 1, "devices": rows}), encoding="utf-8-sig"
        )
        return load_devices(self.path, "hub")

    def test_canonical_binding_is_stable_and_changes_with_authority(self):
        first = self.write([self.row(), self.row(id="other")])
        reordered = self.write([self.row(id="other"), self.row()])
        self.assertEqual(first.binding, reordered.binding)
        self.assertNotEqual(
            first.binding,
            self.write([self.row(allow_exec=True)]).binding,
        )
        empty = AgentPolicy(self.root)
        legacy = json.dumps(
            {"root": str(empty.root), "write": False, "exec": False, "hosts": []},
            sort_keys=True,
        )
        self.assertEqual(
            policy_binding(empty), hashlib.sha256(legacy.encode()).hexdigest()
        )
        self.assertNotEqual(
            policy_binding(empty), policy_binding(replace(empty, device_config=first))
        )

    def test_credentials_unknown_fields_duplicate_keys_and_unsafe_endpoints_are_rejected(
        self,
    ):
        cases = [
            self.row(token="raw-secret"),
            self.row(token_env="PATH"),
            self.row(root="relative"),
            self.row(id="hub"),
            self.row(allow_write="false"),
            *[
                self.row(url=url)
                for url in (
                    "http://192.168.1.2/mcp",
                    "http://localhost/mcp",
                    "https://u:p@peer.example/mcp",
                    "https://peer.example/mcp?token=x",
                    "https://peer.example/other",
                )
            ],
        ]
        for row in cases:
            with self.subTest(row=row), self.assertRaises(ValueError):
                self.write([row])
        with self.assertRaises(ValueError):
            self.write([self.row(), self.row()])
        self.path.write_text('{"version":1,"version":1,"devices":[]}', encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "duplicate"):
            load_devices(self.path, "hub")
        self.path.write_bytes(b" " * (MAX_CONFIG_BYTES + 1))
        with self.assertRaisesRegex(ValueError, "64 KiB"):
            load_devices(self.path, "hub")

    def test_stdio_config_with_profiles_runs_under_python_without_site_packages(self):
        self.write([self.row()])
        response = subprocess.run(
            [
                sys.executable,
                "-S",
                str(Path(__file__).resolve().parents[1] / "folderlens.py"),
                "--root",
                str(self.root),
                "--name",
                "hub",
                "--devices",
                str(self.path),
                "--print-config",
                "json",
            ],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        self.assertEqual(response.returncode, 0, response.stderr)
        self.assertIn(
            str(self.path), json.loads(response.stdout)["mcpServers"]["hub"]["args"]
        )
        self.assertFalse((self.root / ".folderlens").exists())

    def test_owner_terminal_discloses_peer_authority_without_credential_values(self):
        config = self.write([self.row(allow_write=True, allow_exec=False)])
        provider = OwnerOAuthProvider(
            AgentPolicy(
                self.root, allow_write=True, allow_exec=True, device_config=config
            ),
            "https://hub.example",
            ["https://client.example/callback"],
            self.root / "private-auth",
        )
        self.addCleanup(provider.close)
        output = io.StringIO()
        with redirect_stderr(output):
            provider.notify_owner(
                "fixture-request",
                "fixture-pin",
                OAuthClientInformationFull(
                    client_id="client", client_name="Test client"
                ),
                [READ_SCOPE, WRITE_SCOPE, EXEC_SCOPE],
            )
        text = output.getvalue()
        self.assertIn("Authorized peer:", text)
        self.assertIn('"write": true', text)
        self.assertIn('"exec": false', text)
        self.assertNotIn(TOKEN, text)


class DeviceCatalogBoundsTests(unittest.IsolatedAsyncioTestCase):
    async def test_body_headers_and_streams_enforce_bounds_without_redirects(self):
        request = httpx2.Request("GET", "https://peer.example/mcp")
        for response in (
            httpx2.Response(
                302, headers={"Location": "https://other.example"}, request=request
            ),
            httpx2.Response(200, headers={"Content-Encoding": "gzip"}, request=request),
            httpx2.Response(200, headers={"Content-Length": "9"}, request=request),
        ):
            with self.assertRaises(ValueError):
                await response_guard(4)(response)

        class Source(httpx2.AsyncByteStream):
            closed = False

            async def __aiter__(self):
                yield b"ab"
                yield b"cdef"

            async def aclose(self):
                self.closed = True

        source = Source()
        bounded = BoundedResponse(source, 4)
        with self.assertRaises(ValueError):
            _ = [chunk async for chunk in bounded]
        await bounded.aclose()
        self.assertTrue(source.closed)

    async def test_overall_refresh_reports_unvisited_peers_without_background_polling(
        self,
    ):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "devices.json"
            path.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "devices": [
                            {
                                "id": identity,
                                "url": "https://peer.example/mcp",
                                "root": directory,
                                "token_env": TOKEN_ENV,
                            }
                            for identity in ("one", "two", "three")
                        ],
                    }
                )
            )
            catalog = DeviceCatalog(load_devices(path, "hub"))
            clock = [0]
            visited = []

            async def probe(identity, seconds):
                visited.append(identity)
                clock[0] += 13

            with (
                patch.object(catalog, "probe", probe),
                patch(
                    "agentarm.device_catalog.time",
                    SimpleNamespace(monotonic=lambda: clock[0]),
                ),
            ):
                response = await catalog.refresh()
            self.assertEqual(visited, ["one"])
            self.assertEqual(
                response,
                {"refresh_complete": False, "unrefreshed_ids": ["two", "three"]},
            )


class DeviceCatalogWireTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.peer_root = self.root / "peer"
        self.peer_root.mkdir()
        self.listener = socket.socket()
        self.listener.bind(("127.0.0.1", 0))
        self.port = self.listener.getsockname()[1]
        self.runner = uvicorn.Server(
            uvicorn.Config(
                http_app(
                    create_agent(
                        AgentPolicy(self.peer_root, allow_write=True, allow_exec=True),
                        "peer",
                    ),
                    TOKEN,
                ),
                host="127.0.0.1",
                access_log=False,
                log_level="error",
            )
        )
        self.thread = threading.Thread(
            target=self.runner.run, kwargs={"sockets": [self.listener]}, daemon=True
        )
        self.thread.start()
        self.addAsyncCleanup(self.stop_peer)
        deadline = time.monotonic() + 5
        while not self.runner.started:
            if not self.thread.is_alive() or time.monotonic() > deadline:
                self.fail("Fixture peer did not start")
            await asyncio.sleep(0.01)

    async def stop_peer(self):
        self.runner.should_exit = True
        await asyncio.to_thread(self.thread.join, 5)
        self.listener.close()
        self.assertFalse(self.thread.is_alive(), "Peer fixture leaked a server thread")

    def catalog(self, token=TOKEN, **changes):
        path = self.root / "devices.json"
        path.write_text(
            json.dumps(
                {
                    "version": 1,
                    "devices": [
                        {
                            "id": "peer",
                            "url": f"http://127.0.0.1:{self.port}/mcp",
                            "root": str(self.peer_root),
                            "token_env": TOKEN_ENV,
                            "allow_write": True,
                            "allow_exec": False,
                            **changes,
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )
        with patch.dict(os.environ, {TOKEN_ENV: token}):
            catalog = DeviceCatalog(load_devices(path, "hub"))
        self.addCleanup(catalog.close)
        return catalog

    async def test_real_authenticated_probe_pins_identity_root_and_permission_ceilings(
        self,
    ):
        catalog = self.catalog()
        self.assertEqual(catalog.metadata(True, True)[0]["status"], "not_checked")
        response = await catalog.refresh("peer")
        self.assertTrue(response["refresh_complete"])
        record = catalog.metadata(True, True)[0]
        self.assertEqual(record["status"], "online")
        self.assertTrue(record["allow_write"])
        self.assertFalse(record["allow_exec"])
        self.assertFalse(catalog.metadata(False, False)[0]["allow_write"])
        self.assertNotIn(TOKEN, json.dumps(record))
        self.assertNotIn(TOKEN_ENV, json.dumps(record))
        with patch("agentarm.device_catalog.STATUS_TTL", 0):
            stale = catalog.metadata(True, True)[0]
        self.assertTrue(stale["stale"])
        self.assertFalse(stale["allow_write"])

    async def test_authentication_and_identity_or_root_mismatches_are_explicit(self):
        for token, changes, expected in [
            ("incorrect-device-token-value-123456789", {}, "authentication_error"),
            (TOKEN, {"id": "wrong"}, "identity_mismatch"),
            (TOKEN, {"root": str(self.root)}, "root_mismatch"),
            ("", {}, "credential_missing"),
        ]:
            catalog = self.catalog(token, **changes)
            await catalog.refresh()
            record = catalog.metadata(True, True)[0]
            self.assertEqual(record["status"], expected)
            self.assertFalse(record["allow_write"])
            self.assertFalse(record["allow_exec"])

    async def test_hub_catalog_is_paged_protected_and_requires_matching_policy(self):
        catalog = self.catalog()
        with self.assertRaises(ValueError):
            create_agent(AgentPolicy(self.root), "hub", device_catalog=catalog)
        hub = create_agent(
            AgentPolicy(self.root, allow_write=True, device_config=catalog.config),
            "hub",
            device_catalog=catalog,
        )

        async def call(name, args):
            response = await hub.call_tool(name, args)
            return response.structured_content or json.loads(response.content[0].text)

        first = await call("list_devices", {"limit": 1})
        self.assertEqual(first["devices"][0]["id"], "hub")
        self.assertEqual(first["next_offset"], 1)
        last = await call("list_devices", {"offset": first["next_offset"]})
        self.assertEqual(last["devices"][0]["id"], "peer")
        self.assertIsNone(last["next_offset"])
        refreshed = await call("list_devices", {"refresh": True})
        self.assertEqual(refreshed["devices"][1]["status"], "online")
        with self.assertRaises(ToolError):
            await call("read_file", {"path": "devices.json"})
        with self.assertRaises(ToolError):
            await call("set_config_value", {"key": "device_config", "value": False})

    def hub(self, catalog, **changes):
        return create_agent(
            AgentPolicy(
                self.root,
                allow_write=True,
                allow_exec=True,
                device_config=catalog.config,
            ),
            "hub",
            device_catalog=catalog,
            **changes,
        )

    async def route(self, hub, tool="read_file", **arguments):
        return await hub.call_tool(
            "call_device",
            {
                "device_id": "peer",
                "tool_name": tool,
                "arguments": arguments,
            },
        )

    async def test_routed_writes_preview_hash_conflicts_and_native_images(self):
        from PIL import Image

        catalog = self.catalog()
        store = ToolHistory(self.root / "private-history", "content")
        self.addCleanup(store.close)
        hub = self.hub(catalog, history_store=store)
        args = {"path": "routed.txt", "content": "one write", "base_sha256": None}
        preview = await self.route(hub, "write_file", **args)
        self.assertFalse(preview.is_error)
        self.assertFalse((self.peer_root / "routed.txt").exists())
        written = await self.route(hub, "write_file", **args, preview=False)
        self.assertFalse(written.is_error)
        self.assertEqual((self.peer_root / "routed.txt").read_text(), "one write")
        self.assertFalse((self.root / "routed.txt").exists())
        read = await self.route(hub, path="routed.txt")
        self.assertFalse(read.is_error)
        self.assertIn("one write", str(read.content))
        conflict = await self.route(hub, "write_file", **args, preview=False)
        self.assertTrue(conflict.is_error)
        self.assertEqual((self.peer_root / "routed.txt").read_text(), "one write")
        Image.new("RGB", (2, 2), "green").save(self.peer_root / "fixture.png")
        image = await self.route(hub, "read_image", path="fixture.png")
        self.assertFalse(image.is_error)
        self.assertTrue(any(item.type == "image" for item in image.content))
        history = await hub.call_tool("get_recent_tool_calls", {})
        page = history.structured_content or json.loads(history.content[0].text)
        self.assertEqual(page["calls"], [])

    async def test_routing_rejects_missing_hub_scopes_and_profile_ceilings(self):
        catalog = self.catalog()
        hub = self.hub(catalog)
        write = {
            "path": "denied.txt",
            "content": "denied",
            "base_sha256": None,
            "preview": False,
        }
        with (
            patch(
                "mcp.server.auth.middleware.auth_context.get_access_token",
                return_value=SimpleNamespace(
                    client_id="read-client", scopes=[READ_SCOPE]
                ),
            ),
            self.assertRaises(ToolError),
        ):
            await self.route(hub, "write_file", **write)
        for name in (
            "start_process",
            "call_device",
            "start_tool_call",
            "get_recent_tool_calls",
            "shutdown",
            "not_registered",
        ):
            with self.subTest(tool=name), self.assertRaises(ToolError):
                await self.route(hub, name)
        self.assertFalse((self.peer_root / "denied.txt").exists())
        await hub.call_tool("set_config_value", {"key": "allow_write", "value": False})
        with self.assertRaises(ToolError):
            await self.route(hub, "write_file", **write)

    async def test_routing_rechecks_root_instead_of_trusting_cached_online_state(self):
        catalog = self.catalog(root=str(self.root))
        catalog.states["peer"].status = "online"
        response = await self.route(
            self.hub(catalog),
            "write_file",
            path="denied.txt",
            content="denied",
            base_sha256=None,
            preview=False,
        )
        self.assertTrue(response.is_error)
        self.assertEqual(response.structured_content["status"], "not_forwarded")
        self.assertFalse((self.peer_root / "denied.txt").exists())

    async def test_lost_response_reports_uncertainty_without_replaying_write(self):
        from mcp import ClientSession

        catalog = self.catalog()
        original = ClientSession.call_tool
        writes = []

        async def lose_after_write(session, name, arguments=None, **kwargs):
            result = await original(session, name, arguments, **kwargs)
            if name == "write_file":
                writes.append(name)
                raise TimeoutError("synthetic private transport detail")
            return result

        with patch.object(ClientSession, "call_tool", lose_after_write):
            response = await self.route(
                self.hub(catalog),
                "write_file",
                path="uncertain.txt",
                content="once",
                base_sha256=None,
                preview=False,
            )
        self.assertEqual(writes, ["write_file"])
        self.assertEqual((self.peer_root / "uncertain.txt").read_text(), "once")
        self.assertTrue(response.is_error)
        self.assertEqual(response.structured_content["status"], "outcome_unknown")
        self.assertFalse(response.structured_content["retry_original_tool"])
        self.assertNotIn("synthetic private", str(response))

    async def test_oauth_revocation_during_peer_verification_prevents_forwarding(self):
        catalog = self.catalog()
        policy = AgentPolicy(
            self.root, allow_write=True, allow_exec=True, device_config=catalog.config
        )
        provider = OwnerOAuthProvider(
            policy,
            "https://hub.example",
            ["https://client.example/callback"],
            self.root / "auth-state",
        )
        self.addCleanup(provider.close)
        hub = create_agent(
            policy, "hub", device_catalog=catalog, oauth_provider=provider
        )
        token = SimpleNamespace(
            token="synthetic-test-token",
            client_id="owner-client",
            scopes=[READ_SCOPE, WRITE_SCOPE, EXEC_SCOPE],
        )
        with (
            patch(
                "mcp.server.auth.middleware.auth_context.get_access_token",
                return_value=token,
            ),
            patch.object(
                provider,
                "load_access_token",
                AsyncMock(side_effect=[token, token, None]),
            ) as reload_token,
        ):
            response = await self.route(
                hub,
                "write_file",
                path="revoked.txt",
                content="denied",
                base_sha256=None,
                preview=False,
            )
        self.assertEqual(reload_token.await_count, 3)
        self.assertTrue(response.is_error)
        self.assertEqual(response.structured_content["status"], "not_forwarded")
        self.assertFalse((self.peer_root / "revoked.txt").exists())

    async def test_async_routed_write_keeps_idempotency_and_original_scope_binding(
        self,
    ):
        catalog = self.catalog()
        hub = self.hub(catalog)
        arguments = {
            "device_id": "peer",
            "tool_name": "write_file",
            "arguments": {
                "path": "async-peer.txt",
                "content": "once",
                "base_sha256": None,
                "preview": False,
            },
        }
        request = {
            "tool_name": "call_device",
            "arguments": arguments,
            "request_id": "peer-write-once",
        }
        full = SimpleNamespace(
            client_id="same-client", scopes=[READ_SCOPE, WRITE_SCOPE]
        )
        with patch(
            "mcp.server.auth.middleware.auth_context.get_access_token",
            return_value=full,
        ):
            first = await hub.call_tool("start_tool_call", request)
            repeated = await hub.call_tool("start_tool_call", request)
            call_id = first.structured_content["call_id"]
            self.assertEqual(call_id, repeated.structured_content["call_id"])
            result = await hub.call_tool(
                "get_tool_result", {"call_id": call_id, "wait_ms": 5000}
            )
            self.assertFalse(result.is_error)
            self.assertEqual(result.structured_content["call"]["status"], "completed")
        self.assertEqual((self.peer_root / "async-peer.txt").read_text(), "once")
        with (
            patch(
                "mcp.server.auth.middleware.auth_context.get_access_token",
                return_value=SimpleNamespace(
                    client_id="same-client", scopes=[READ_SCOPE]
                ),
            ),
            self.assertRaises(ToolError),
        ):
            await hub.call_tool("get_tool_result", {"call_id": call_id})

    async def test_routed_exec_uses_peer_sessions_and_scrubs_peer_credentials(self):
        catalog = self.catalog(allow_exec=True)
        hub = self.hub(catalog)
        with patch.dict(os.environ, {TOKEN_ENV: TOKEN}):
            response = await self.route(
                hub,
                "start_process",
                command=[
                    sys.executable,
                    "-I",
                    "-S",
                    "-c",
                    "import os; print('peer-exec'); print(any(k.startswith('FOLDERLENS_PEER_') for k in os.environ))",
                ],
            )
        self.assertFalse(response.is_error)
        started = response.structured_content or json.loads(response.content[0].text)
        received = await self.route(
            hub, "read_process_output", session_id=started["session_id"], wait_ms=5000
        )
        self.assertFalse(received.is_error)
        output = received.structured_content or json.loads(received.content[0].text)
        self.assertIn("peer-exec", output["output"])
        self.assertIn("False", output["output"])
        await self.route(hub, "force_terminate", session_id=started["session_id"])
