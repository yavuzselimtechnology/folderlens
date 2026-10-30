import asyncio
import json
import sys
import tempfile
import time
import unittest
from pathlib import Path

from agentarm.mcp_server import create_agent
from agentarm.processes import Processes
from agentarm.workspace import AgentPolicy, Workspace


class ProcessTailTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)

    def process(self, payload: bytes, budget=1024):
        processes = Processes(
            Workspace(AgentPolicy(self.root, allow_exec=True)), max_output_bytes=budget
        )
        self.addCleanup(processes.close)
        result = processes.start(
            [
                sys.executable,
                "-c",
                "import sys; sys.stdout.buffer.write(" + repr(payload) + ")",
            ]
        )
        key = result["session_id"]
        self.assertTrue(processes.sessions[key].readers_done.wait(timeout=10))
        processes.sessions[key].process.wait(timeout=5)
        return processes, key

    def test_tail_absolute_continuation_empty_and_utf8_byte_boundaries(self):
        payload = "prefix\nTürkçe中文".encode()
        processes, key = self.process(payload)
        result = processes.read(key, offset=-6)
        self.assertEqual(result["output"], "中文")
        self.assertEqual(result["offset"], len(payload) - 6)
        self.assertEqual(result["next_offset"], len(payload))
        self.assertFalse(result["requested_data_dropped"])
        self.assertEqual(processes.read(key, result["next_offset"])["output"], "")
        self.assertEqual(processes.read(key, -999)["output"], payload.decode())
        partial = processes.read(key, -1)
        self.assertEqual(partial["output"], payload[-1:].decode("utf-8", "replace"))
        empty, empty_key = self.process(b"")
        self.assertEqual(empty.read(empty_key, -10)["offset"], 0)
        self.assertEqual(empty.read(empty_key, -10)["output"], "")

    def test_tail_clips_to_retained_ring_and_reports_lost_requested_data(self):
        payload = b"0123456789abcdefghijklmnopqrstuvwxyz"
        processes, key = self.process(payload, budget=8)
        result = processes.read(key, -20)
        self.assertEqual(result["output"], payload[-8:].decode())
        self.assertTrue(result["requested_data_dropped"])
        self.assertEqual(result["available_start"], len(payload) - 8)
        self.assertEqual(result["available_end"], len(payload))
        recent = processes.read(key, -4, length=2)
        self.assertEqual(recent["output"], "wx")
        self.assertFalse(recent["requested_data_dropped"])
        self.assertEqual(processes.read(key, recent["next_offset"])["output"], "yz")
        self.assertTrue(processes.read(key, 0)["requested_data_dropped"])
        for offset, length in ((True, 1), (1.5, 1), (0, False), (0, 0), (0, 65537)):
            with (
                self.subTest(offset=offset, length=length),
                self.assertRaises(ValueError),
            ):
                processes.read(key, offset, length)

    def test_mcp_tail_schema_and_actual_process_result(self):
        async def run():
            agent = create_agent(AgentPolicy(self.root, allow_exec=True))
            started = await agent.call_tool(
                "start_process",
                {"command": [sys.executable, "-u", "-c", "print('mcp-tail-fixture')"]},
            )
            key = json.loads(started.content[0].text)["session_id"]
            try:
                deadline = time.monotonic() + 10
                while True:
                    result = await agent.call_tool(
                        "read_process_output", {"session_id": key}
                    )
                    self.assertFalse(result.is_error)
                    full = json.loads(result.content[0].text)
                    if full["status"] == "completed":
                        break
                    self.assertLess(time.monotonic(), deadline)
                    await asyncio.sleep(0.02)
                self.assertIn("mcp-tail-fixture", full["output"])
                tail = await agent.call_tool(
                    "read_process_output", {"session_id": key, "offset": -5}
                )
                self.assertFalse(tail.is_error)
                record = json.loads(tail.content[0].text)
                self.assertGreaterEqual(record["offset"], 0)
                self.assertLessEqual(record["next_offset"] - record["offset"], 5)
                self.assertIn("available_start", record)
                self.assertEqual(
                    record["output"], full["output"].encode()[-5:].decode()
                )
            finally:
                await agent.call_tool("force_terminate", {"session_id": key})

        asyncio.run(run())
