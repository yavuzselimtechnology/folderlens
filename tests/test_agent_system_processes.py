import asyncio
import json
import os
import signal
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import psutil

from agentarm.mcp_server import create_agent
from agentarm.processes import Processes
from agentarm.system_processes import SystemProcesses
from agentarm.workspace import AgentPolicy, Workspace

CAN_KILL = os.name == "nt" or (
    sys.platform == "linux"
    and hasattr(os, "pidfd_open")
    and hasattr(signal, "pidfd_send_signal")
)


class SystemProcessTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.workspace = Workspace(AgentPolicy(self.root, allow_exec=True))
        self.owned = Processes(self.workspace)
        self.addCleanup(self.owned.close)
        self.system = SystemProcesses(self.workspace, self.owned)

    def child(self):
        process = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(60)"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

        def cleanup():
            if process.poll() is None:
                process.kill()
            process.wait(timeout=3)

        self.addCleanup(cleanup)
        return process

    def test_native_observation_identity_cpu_memory_and_no_command_line(self):
        child = self.child()
        first = self.system.list(pid=child.pid)["processes"][0]
        self.assertEqual(first["pid"], child.pid)
        self.assertTrue(first["process_key"])
        self.assertGreater(first["rss_bytes"], 0)
        self.assertIsNone(first["cpu_percent"])
        second = self.system.list(pid=child.pid)["processes"][0]
        self.assertEqual(second["process_key"], first["process_key"])
        self.assertGreaterEqual(second["cpu_percent"], 0)
        self.assertGreater(second["cpu_interval_seconds"], 0)
        self.assertNotIn("cmdline", second)
        self.assertNotIn("time.sleep", json.dumps(second))

    def test_cpu_calculation_identity_reset_expiry_and_bounded_samples(self):
        rows = [
            {"process_key": "a", "cpu_seconds": 1},
            {"process_key": "a", "cpu_seconds": 3},
            {"process_key": "b", "cpu_seconds": 4},
            {"process_key": "b", "cpu_seconds": 5},
        ]
        with (
            patch("agentarm.system_processes._snapshot", side_effect=rows),
            patch(
                "agentarm.system_processes.time.perf_counter",
                side_effect=[10, 12, 13, 74],
            ),
        ):
            self.assertIsNone(self.system.inspect(100)["cpu_percent"])
            self.assertEqual(self.system.inspect(100)["cpu_percent"], 100)
            self.assertIsNone(self.system.inspect(100)["cpu_percent"])
            self.assertIsNone(self.system.inspect(100)["cpu_percent"])
        with (
            patch("agentarm.system_processes.CPU_SAMPLE_LIMIT", 2),
            patch(
                "agentarm.system_processes._snapshot",
                side_effect=lambda pid: {"process_key": str(pid), "cpu_seconds": 0},
            ),
        ):
            for pid in range(10, 14):
                self.system.inspect(pid)
        self.assertEqual(list(self.system.samples), ["12", "13"])

    def test_process_pages_include_budget_and_unavailable_metadata(self):
        workspace = Workspace(AgentPolicy(self.root, max_output_chars=800))
        system = SystemProcesses(workspace, self.owned)

        def snapshot(pid):
            if pid == 103:
                raise PermissionError("fixture denied")
            return {
                "pid": pid,
                "process_key": str(pid),
                "name": "fixture " * 8,
                "status": "running",
                "rss_bytes": 1,
                "cpu_seconds": 0,
            }

        with (
            patch(
                "agentarm.system_processes.psutil.pids",
                return_value=list(range(100, 106)),
            ),
            patch("agentarm.system_processes._snapshot", side_effect=snapshot),
        ):
            offset, entries = 0, []
            while offset is not None:
                result = system.list(offset=offset)
                self.assertLessEqual(len(json.dumps(result, ensure_ascii=False)), 800)
                entries.extend(result["processes"])
                offset = result["next_offset"]
        self.assertEqual([entry["pid"] for entry in entries], list(range(100, 106)))
        denied = next(entry for entry in entries if entry["pid"] == 103)
        self.assertEqual(denied["status"], "unavailable")
        self.assertIsNone(denied["process_key"])
        for options in ({"offset": True}, {"limit": 0}, {"pid": False}, {"pid": -1}):
            with self.subTest(options=options), self.assertRaises(ValueError):
                system.list(**options)

    def test_permission_and_protected_ancestor_guards_do_not_open_target(self):
        readonly = SystemProcesses(Workspace(AgentPolicy(self.root)), self.owned)
        with patch("agentarm.system_processes.WindowsProcess") as native:
            with self.assertRaises(PermissionError):
                readonly.kill(100, "fixture")
            native.assert_not_called()
        with (
            patch("agentarm.system_processes.os.getpid", return_value=100),
            patch("agentarm.system_processes.psutil.Process") as process,
            patch("agentarm.system_processes.WindowsProcess") as native,
        ):
            process.return_value.parents.return_value = [Mock(pid=200)]
            for pid in (1, 100, 200):
                with self.subTest(pid=pid), self.assertRaises(PermissionError):
                    self.system.kill(pid, "fixture")
            native.assert_not_called()

    @unittest.skipUnless(os.name == "nt", "Windows critical-process guard")
    def test_critical_process_is_rejected_before_termination(self):
        with patch("agentarm.system_processes.WindowsProcess") as factory:
            process = factory.return_value.__enter__.return_value
            process.key.return_value = "fixture"
            process.times.return_value = (1, 0, 0)
            process.exited.return_value = False
            process.critical.return_value = True
            with self.assertRaisesRegex(PermissionError, "Critical"):
                self.system.kill(2147483647, "fixture")
            process.kill.assert_not_called()

    @unittest.skipUnless(CAN_KILL, "Identity-safe process handle backend unavailable")
    def test_stale_identity_is_rejected_and_only_fixture_process_is_killed(self):
        child = self.child()
        snapshot = self.system.inspect(child.pid)
        with self.assertRaisesRegex(ValueError, "identity conflict"):
            self.system.kill(child.pid, snapshot["process_key"] + "x")
        self.assertIsNone(child.poll())
        result = self.system.kill(child.pid, snapshot["process_key"])
        self.assertTrue(result["terminated"])
        self.assertEqual(result["termination_scope"], "single_process")
        child.wait(timeout=3)

    @unittest.skipUnless(os.name == "nt", "Windows process handle accounting")
    def test_repeated_native_inspection_releases_kernel_handles(self):
        self.system.inspect(os.getpid())
        process = psutil.Process()
        before = process.num_handles()
        for _ in range(40):
            self.system.inspect(os.getpid())
        self.assertLessEqual(process.num_handles(), before)

    @unittest.skipUnless(CAN_KILL, "Identity-safe process handle backend unavailable")
    def test_owned_session_root_uses_owned_tree_cleanup(self):
        started = self.owned.start(
            [sys.executable, "-c", "import time; time.sleep(60)"]
        )
        snapshot = self.system.inspect(started["pid"])
        result = self.system.kill(started["pid"], snapshot["process_key"])
        self.assertEqual(result["termination_scope"], "owned_session_tree")
        self.assertTrue(result["terminated"])
        self.assertIsNotNone(self.owned.sessions[started["session_id"]].process.poll())

    @unittest.skipUnless(CAN_KILL, "Identity-safe process handle backend unavailable")
    def test_mcp_inspection_and_identity_checked_fixture_termination(self):
        child = self.child()

        async def run():
            server = create_agent(self.workspace.policy)
            tools = {tool.name: tool for tool in await server.list_tools()}
            self.assertTrue(tools["kill_process"].annotations.destructive_hint)
            self.assertFalse(tools["kill_process"].annotations.read_only_hint)
            observed = await server.call_tool("get_processes", {"pid": child.pid})
            self.assertFalse(observed.is_error)
            record = json.loads(observed.content[0].text)["processes"][0]
            result = await server.call_tool(
                "kill_process", {"pid": child.pid, "process_key": record["process_key"]}
            )
            self.assertFalse(result.is_error)
            self.assertTrue(json.loads(result.content[0].text)["terminated"])

        asyncio.run(run())
        child.wait(timeout=3)
