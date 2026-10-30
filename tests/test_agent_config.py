import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from mcp.server.mcpserver.exceptions import ToolError

from agentarm.mcp_server import create_agent
from agentarm.workspace import AgentPolicy, Workspace
from folderlens import client_config, main, parser


class ConfigTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.server = create_agent(
            AgentPolicy(
                self.root,
                allow_write=True,
                allow_exec=True,
                url_hosts=("example.com", "www.python.org"),
            ),
            "workstation",
        )

    async def call(self, name, args):
        result = await self.server.call_tool(name, args)
        return result.structured_content or json.loads(result.content[0].text)

    async def test_permissions_only_narrow_and_report_effective_state(self):
        await self.call("set_config_value", {"key": "allow_write", "value": False})
        config = await self.call("get_config", {})
        self.assertFalse(config["allow_write"])
        with self.assertRaises(ToolError):
            await self.call("set_config_value", {"key": "allow_write", "value": True})
        with self.assertRaises(ToolError):
            await self.call(
                "write_file",
                {
                    "path": "x.txt",
                    "content": "not written",
                    "base_sha256": None,
                    "preview": False,
                },
            )
        await self.call(
            "set_config_value", {"key": "url_hosts", "value": ["www.python.org"]}
        )
        config = await self.call("get_config", {})
        self.assertEqual(config["allowed_url_hosts"], ["www.python.org"])
        with self.assertRaises(ToolError):
            await self.call(
                "set_config_value", {"key": "url_hosts", "value": ["example.com"]}
            )
        await self.call("set_config_value", {"key": "max_scan_files", "value": 100})
        with self.assertRaises(ToolError):
            await self.call("set_config_value", {"key": "max_scan_files", "value": 101})
        with self.assertRaises(ToolError):
            await self.call("set_config_value", {"key": "root", "value": []})

    async def test_revoking_exec_stops_existing_owned_session_and_new_commands(self):
        started = await self.call(
            "start_process",
            {"command": [sys.executable, "-c", "import time; time.sleep(60)"]},
        )
        await self.call("set_config_value", {"key": "allow_exec", "value": False})
        result = await self.call(
            "read_process_output", {"session_id": started["session_id"]}
        )
        self.assertEqual(result["status"], "completed")
        self.assertIsNotNone(result["exit_code"])
        with self.assertRaises(ToolError):
            await self.call(
                "start_process",
                {"command": [sys.executable, "-c", "print('disabled')"]},
            )

    async def test_device_alias_and_current_connection_scope(self):
        devices = await self.call("list_devices", {})
        self.assertEqual(devices["devices"][0]["id"], "workstation")
        self.assertEqual(devices["devices"][0]["root"], str(self.root.resolve()))
        self.assertIn("current MCP server", devices["scope"])

    async def test_identity_reports_real_process_owner_without_trusting_environment(
        self,
    ):
        import psutil

        actual = psutil.Process(os.getpid()).username()
        with patch.dict(
            os.environ,
            {
                "USERNAME": "untrusted-environment-name",
                "USER": "untrusted-environment-name",
            },
        ):
            identity = await self.call("who_am_i", {})
        self.assertEqual(identity["os_process_owner"]["account"], actual)
        self.assertIsNone(identity["os_process_owner"]["error_type"])
        self.assertEqual(identity["server_pid"], os.getpid())
        self.assertEqual(identity["device"]["id"], "workstation")
        self.assertIsNone(identity["connection"]["client_id"])
        self.assertEqual(
            identity["effective_permissions"],
            {"read": True, "write": True, "exec": True},
        )

    async def test_identity_intersects_local_policy_and_current_token_without_returning_token(
        self,
    ):
        from agentarm.oauth import READ_SCOPE

        token = SimpleNamespace(
            client_id="approved-client",
            scopes=[READ_SCOPE],
            token="raw-secret-token-must-not-appear",
        )
        with patch(
            "mcp.server.auth.middleware.auth_context.get_access_token",
            return_value=token,
        ):
            identity = await self.call("who_am_i", {})
        self.assertEqual(identity["connection"]["client_id"], "approved-client")
        self.assertEqual(
            identity["effective_permissions"],
            {"read": True, "write": False, "exec": False},
        )
        self.assertNotIn(token.token, json.dumps(identity))
        await self.call("set_config_value", {"key": "allow_exec", "value": False})
        self.assertFalse(
            (await self.call("who_am_i", {}))["effective_permissions"]["exec"]
        )

    async def test_identity_owner_lookup_failure_is_explicit(self):
        import psutil

        with patch(
            "psutil.Process.username", side_effect=psutil.AccessDenied(os.getpid())
        ):
            identity = await self.call("who_am_i", {})
        self.assertEqual(
            identity["os_process_owner"],
            {"account": None, "error_type": "AccessDenied"},
        )


class ConfigLauncherTests(unittest.TestCase):
    def test_named_config_and_url_arguments(self):
        args = parser().parse_args(
            [
                "--name",
                "work-laptop",
                "--root",
                str(Path.cwd()),
                "--allow-url-host",
                "example.com",
                "--print-config",
                "json",
            ]
        )
        config = json.loads(client_config(args))["mcpServers"]["work-laptop"]
        self.assertIn("--name", config["args"])
        self.assertIn("example.com", config["args"])

    def test_http_doctor_does_not_print_success_before_auth_validation(self):
        from unittest.mock import patch

        out = io.StringIO()
        with patch.dict("os.environ", {"FOLDERLENS_TOKEN": ""}), redirect_stdout(out):
            self.assertEqual(main(["--doctor", "--transport", "http"]), 2)
        self.assertNotIn('"ok": true', out.getvalue())

    def test_reducing_file_budget_cannot_break_existing_undo(self):
        with tempfile.TemporaryDirectory() as directory:
            workspace = Workspace(AgentPolicy(Path(directory), allow_write=True))
            created = workspace.write_file("large.txt", "large", None)
            with self.assertRaisesRegex(ValueError, "undo"):
                workspace.reduce_config("max_file_bytes", 1)
            workspace.undo_change(created["undo_id"])
            self.assertEqual(workspace.reduce_config("max_file_bytes", 1)["value"], 1)
