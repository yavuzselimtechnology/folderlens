import asyncio
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import psutil
from mcp.server.mcpserver.exceptions import ToolError

from agentarm.mcp_server import create_agent
from agentarm.processes import Processes, Session
from agentarm.terminal_output import line_window
from agentarm.workspace import AgentPolicy, Workspace


class ProcessWaitTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.processes = Processes(Workspace(AgentPolicy(self.root, allow_exec=True)))
        self.addCleanup(self.processes.close)

    def test_verbose_timing_real_process_and_stable_completion(self):
        key = self.processes.start(
            [
                sys.executable,
                "-u",
                "-c",
                "import sys; print('first',flush=True); sys.stdin.readline(); print('second',flush=True)",
            ]
        )["session_id"]
        first = self.processes.read(key, offset=None, wait_ms=3000, verbose_timing=True)
        self.assertIsNotNone(first["timing"]["first_output_ms"])
        self.assertGreaterEqual(first["timing"]["first_output_ms"], 0)
        self.processes.interact(key, "go\n")
        session = self.processes.sessions[key]
        with session.changed:
            self.assertTrue(session.changed.wait_for(lambda: session.ended, timeout=5))
        result = self.processes.read(key, verbose_timing=True)
        timing = result["timing"]
        self.assertTrue(timing["completed"])
        self.assertGreaterEqual(timing["event_count"], 2)
        self.assertEqual(timing["events"][0]["byte_start"], 0)
        self.assertEqual(timing["events"][-1]["byte_end"], result["available_end"])
        self.assertEqual(timing["first_output_ms"], first["timing"]["first_output_ms"])
        self.assertEqual(
            timing["elapsed_ms"],
            self.processes.read(key, verbose_timing=True)["timing"]["elapsed_ms"],
        )
        self.assertNotIn("timing", self.processes.read(key))

    def test_timing_silent_exit_and_bounded_event_history(self):
        key = self.processes.start([sys.executable, "-c", "pass"])["session_id"]
        session = self.processes.sessions[key]
        with session.changed:
            self.assertTrue(session.changed.wait_for(lambda: session.ended, timeout=5))
        self.assertIsNone(
            self.processes.read(key, verbose_timing=True)["timing"]["first_output_ms"]
        )
        process = Mock()
        capacity = session.output_events.maxlen
        process.stdout.read.side_effect = [b"x"] * (capacity + 3) + [b""]
        fixture = Session(process, time.monotonic(), 1)
        self.processes._read(fixture)
        timing = fixture.timing()
        self.assertEqual(timing["dropped_events"], 3)
        self.assertEqual(len(timing["events"]), capacity)
        self.assertEqual(timing["events"][0]["sequence"], 3)
        self.assertEqual(timing["events"][-1]["byte_end"], capacity + 3)
        self.assertTrue(all("output" not in e for e in timing["events"]))

    def test_timing_timeout_and_explicit_stop_stay_fixed(self):
        for stop in (False, True):
            with self.subTest(explicit_stop=stop):
                key = self.processes.start(
                    [sys.executable, "-c", "import time; time.sleep(60)"],
                    timeout_seconds=1 if not stop else 60,
                )["session_id"]
                session = self.processes.sessions[key]
                if stop:
                    self.processes.stop(key)
                with session.changed:
                    self.assertTrue(
                        session.changed.wait_for(
                            lambda session=session: session.ended, timeout=6
                        )
                    )
                result = self.processes.read(key, verbose_timing=True)
                self.assertEqual(result["timed_out"], not stop)
                self.assertTrue(result["timing"]["completed"])
                self.assertIsNone(result["timing"]["first_output_ms"])
                self.assertGreaterEqual(result["timing"]["elapsed_ms"], 0)
                with patch(
                    "agentarm.processes.time.monotonic",
                    return_value=session.created + 1000,
                ):
                    self.assertEqual(
                        session.timing()["elapsed_ms"], result["timing"]["elapsed_ms"]
                    )

    def test_notification_wait_new_cursor_timeout_and_stdin_close(self):
        script = "import sys; print('ready',flush=True); v=sys.stdin.readline(); print('reply:'+v.strip(),flush=True); sys.stdin.readline()"
        key = self.processes.start([sys.executable, "-u", "-c", script])["session_id"]

        def complete_line(record):
            deadline = time.monotonic() + 5
            while not record["output"].endswith("\n"):
                self.assertLess(time.monotonic(), deadline)
                more = self.processes.read(key, offset=None, wait_ms=1000)
                record["output"] += more["output"]
                record["next_offset"] = more["next_offset"]
            return record

        first = complete_line(self.processes.read(key, offset=None, wait_ms=3000))
        self.assertEqual(first["output"].strip(), "ready")
        session = self.processes.sessions[key]
        waiting = threading.Event()
        results = []
        original = session.changed.wait

        def notified_wait(timeout=None):
            waiting.set()
            return original(timeout)

        def reader():
            try:
                results.append(self.processes.read(key, offset=None, wait_ms=3000))
            except Exception as exc:  # noqa: BLE001 - report fixture thread failures in the test thread.
                results.append(exc)

        with patch.object(session.changed, "wait", side_effect=notified_wait):
            thread = threading.Thread(target=reader)
            thread.start()
            self.assertTrue(waiting.wait(timeout=3))
            self.processes.interact(key, "hello\n")
            thread.join(timeout=5)
            self.assertFalse(thread.is_alive())
        complete_line(results[0])
        self.assertEqual(results[0]["output"].strip(), "reply:hello")
        self.assertEqual(results[0]["wait_status"], "data_available")
        self.assertEqual(results[0]["offset"], first["next_offset"])
        # Explicit windows do not rewind the automatic new-output cursor.
        self.assertIn("ready", self.processes.read(key, offset=0)["output"])
        timed = self.processes.read(key, offset=None, wait_ms=40)
        self.assertEqual(timed["output"], "")
        self.assertEqual(timed["wait_status"], "timeout")
        self.assertIsNone(session.process.poll())
        self.processes.interact(key, close_stdin=True)
        with self.assertRaisesRegex(ValueError, "closed or closing"):
            self.processes.interact(key, "too late")
        eof = self.processes.read(key, offset=None, wait_ms=3000)
        self.assertIn(eof["wait_status"], {"completed", "output_closed"})
        self.assertTrue(eof["stdin_close_requested"])
        session.process.wait(timeout=3)

    def test_repl_prompt_is_a_hint_and_never_claims_os_input_wait(self):
        key = self.processes.start([sys.executable, "-I", "-S", "-q", "-i"])[
            "session_id"
        ]
        prompt = self.processes.read(key, offset=None, wait_ms=3000)
        deadline = time.monotonic() + 5
        while prompt["prompt_hint"] is None:
            self.assertLess(time.monotonic(), deadline, repr(prompt))
            prompt = self.processes.read(key, offset=None, wait_ms=1000)
        self.assertEqual(prompt["prompt_hint"], "python_primary", repr(prompt))
        self.assertTrue(prompt["prompt_hint_is_heuristic"])
        self.assertIsNone(prompt["waiting_for_input"])
        self.assertFalse(prompt["stdin_closed"])

    def test_prompt_wait_skips_partial_output_and_preserves_prompt_bytes(self):
        script = (
            "import sys; print('partial',flush=True); sys.stdin.readline(); "
            "sys.stdout.write('>>> '); sys.stdout.flush(); sys.stdin.readline()"
        )
        key = self.processes.start([sys.executable, "-u", "-c", script])["session_id"]
        session = self.processes.sessions[key]
        # Establish output before waiting: data alone must not satisfy prompt mode.
        initial = self.processes.read(key, wait_ms=3000)
        deadline = time.monotonic() + 5
        while not initial["output"].endswith("\n"):
            self.assertLess(time.monotonic(), deadline)
            initial = self.processes.read(key, wait_ms=3000)
        partial = self.processes.read(key, wait_ms=20, wait_for_prompt=True)
        self.assertEqual(partial["wait_status"], "timeout")
        self.assertEqual(partial["output"].strip(), "partial")
        waiting = threading.Event()
        results = []
        original = session.changed.wait

        def notified_wait(timeout=None):
            waiting.set()
            return original(timeout)

        def reader():
            try:
                results.append(
                    self.processes.read(key, wait_ms=3000, wait_for_prompt=True)
                )
            except Exception as exc:  # noqa: BLE001 - report fixture thread failures in the test thread.
                results.append(exc)

        with patch.object(session.changed, "wait", side_effect=notified_wait):
            thread = threading.Thread(target=reader)
            thread.start()
            self.assertTrue(waiting.wait(timeout=3))
            self.processes.interact(key, "continue\n")
            thread.join(timeout=5)
            self.assertFalse(thread.is_alive())
        result = results[0]
        self.assertEqual(result["wait_status"], "prompt_detected")
        self.assertTrue(result["wait_for_prompt"])
        self.assertTrue(result["output"].endswith(">>> "))
        self.assertIsNone(result["waiting_for_input"])
        # A previously consumed prompt must not claim that new output arrived.
        consumed = self.processes.read(key, offset=None, wait_for_prompt=True)
        self.assertEqual(consumed["wait_status"], "prompt_detected")
        empty = self.processes.read(key, offset=None, wait_for_prompt=True)
        self.assertEqual(empty["output"], "")
        self.assertEqual(empty["wait_status"], "not_waiting")
        self.processes.interact(key, close_stdin=True)
        final = self.processes.read(key, wait_ms=3000, wait_for_prompt=True)
        self.assertIn(final["wait_status"], {"completed", "output_closed"})

    def test_mcp_prompt_wait_schema_and_real_python_repl(self):
        async def run():
            agent = create_agent(self.processes.workspace.policy)
            started = await agent.call_tool(
                "start_process", {"command": [sys.executable, "-I", "-S", "-q", "-i"]}
            )
            key = json.loads(started.content[0].text)["session_id"]
            try:
                result = await agent.call_tool(
                    "read_process_output",
                    {
                        "session_id": key,
                        "offset": None,
                        "wait_ms": 3000,
                        "wait_for_prompt": True,
                        "verbose_timing": True,
                    },
                )
                self.assertFalse(result.is_error)
                value = json.loads(result.content[0].text)
                self.assertEqual(value["wait_status"], "prompt_detected")
                self.assertEqual(value["prompt_hint"], "python_primary")
                self.assertTrue(value["output"].endswith(">>> "))
                self.assertIsNotNone(value["timing"]["first_output_ms"])
                self.assertFalse(value["timing"]["completed"])
                self.assertEqual(
                    value["timing"]["events"][-1]["byte_end"], value["available_end"]
                )
            finally:
                await agent.call_tool("force_terminate", {"session_id": key})

        asyncio.run(run())

    def test_mcp_start_wait_returns_prompt_and_continuation_cursor(self):
        async def run():
            agent = create_agent(self.processes.workspace.policy)
            result = await agent.call_tool(
                "start_process",
                {
                    "command": [sys.executable, "-I", "-S", "-q", "-i"],
                    "wait_ms": 3000,
                    "wait_for_prompt": True,
                    "verbose_timing": True,
                },
            )
            self.assertFalse(result.is_error)
            value = json.loads(result.content[0].text)
            key = value["session_id"]
            try:
                self.assertEqual(value["wait_status"], "prompt_detected")
                self.assertTrue(value["output"].endswith(">>> "))
                self.assertIsNotNone(value["timing"]["first_output_ms"])
                followup = await agent.call_tool(
                    "read_process_output",
                    {"session_id": key, "offset": value["next_offset"]},
                )
                self.assertEqual(json.loads(followup.content[0].text)["output"], "")
            finally:
                await agent.call_tool("force_terminate", {"session_id": key})

        asyncio.run(run())

    def test_mcp_start_invalid_wait_does_not_launch(self):
        async def run():
            agent = create_agent(self.processes.workspace.policy)
            with patch("agentarm.processes.Processes.start") as launch:
                for wait in (-1, 30001):
                    with self.assertRaisesRegex(ToolError, "wait_ms"):
                        await agent.call_tool(
                            "start_process",
                            {
                                "command": [sys.executable, "-c", "pass"],
                                "wait_ms": wait,
                            },
                        )
                launch.assert_not_called()

        asyncio.run(run())

    def test_mcp_interact_wait_returns_only_the_new_repl_response(self):
        async def run():
            agent = create_agent(self.processes.workspace.policy)
            started = await agent.call_tool(
                "start_process",
                {
                    "command": [sys.executable, "-I", "-S", "-q", "-i"],
                    "wait_ms": 3000,
                    "wait_for_prompt": True,
                },
            )
            initial = json.loads(started.content[0].text)
            key = initial["session_id"]
            try:
                result = await agent.call_tool(
                    "interact_with_process",
                    {
                        "session_id": key,
                        "text": "print('one-call-response')\n",
                        "wait_ms": 3000,
                        "wait_for_prompt": True,
                        "verbose_timing": True,
                    },
                )
                self.assertFalse(result.is_error)
                value = json.loads(result.content[0].text)
                self.assertTrue(value["queued"])
                self.assertEqual(value["wait_status"], "prompt_detected")
                self.assertIn("one-call-response", value["output"])
                self.assertTrue(value["output"].endswith(">>> "))
                self.assertFalse(value["output"].startswith(initial["output"]))
                self.assertGreater(value["next_offset"], initial["next_offset"])
                self.assertIsNotNone(value["timing"]["first_output_ms"])
            finally:
                await agent.call_tool("force_terminate", {"session_id": key})

        asyncio.run(run())

    def test_interact_invalid_wait_does_not_queue_input(self):
        key = self.processes.start([sys.executable, "-I", "-S", "-q", "-i"])[
            "session_id"
        ]
        try:
            with patch.object(
                self.processes.sessions[key].inputs, "put_nowait"
            ) as enqueue:
                with self.assertRaisesRegex(ValueError, "wait_ms"):
                    self.processes.interact(
                        key, "print('must-not-run')\n", wait_ms=30001
                    )
                enqueue.assert_not_called()
        finally:
            self.processes.stop(key)

    def test_line_windows_preserve_crlf_and_expose_partial_line_byte_cursor(self):
        raw = "one\r\nTürkçe\nlast".encode()
        first = line_window(raw, 0, False, 0, 0, 2, 100, False)
        self.assertEqual(first["output"], "one\r\nTürkçe\n")
        self.assertEqual(first["next_offset"], 2)
        self.assertFalse(first["last_line_incomplete"])
        tail = line_window(raw, 0, False, 0, -1, 10, 100, False)
        self.assertEqual(tail["output"], "last")
        self.assertEqual(tail["offset"], tail["next_offset"])
        self.assertTrue(tail["last_line_incomplete"])
        self.assertEqual(tail["next_byte_offset"], len(raw))
        final = line_window(raw, 0, False, 0, -1, 10, 100, True)
        self.assertEqual(final["next_offset"], 3)
        self.assertFalse(line_window(b"", 0, False, 0, -1, 10, 100, True)["output"])
        with self.assertRaisesRegex(ValueError, "unit=bytes"):
            line_window(b"x" * 100, 0, False, 0, 0, 10, 20, True)

    def test_ring_overflow_tracks_global_lines_and_cut_prefix(self):
        processes = Processes(self.processes.workspace, max_output_bytes=8)
        self.addCleanup(processes.close)
        payload = b"zero\none\ntwo\nthree\n"
        key = processes.start(
            [
                sys.executable,
                "-c",
                "import sys; sys.stdout.buffer.write(" + repr(payload) + ")",
            ]
        )["session_id"]
        session = processes.sessions[key]
        self.assertTrue(session.readers_done.wait(timeout=5))
        session.process.wait(timeout=5)
        first = processes.read(key, unit="lines")
        self.assertEqual(first["output"], "o\nthree\n")
        self.assertEqual(first["available_start"], 2)
        self.assertTrue(first["first_line_prefix_dropped"])
        self.assertTrue(first["requested_data_dropped"])
        tail = processes.read(key, offset=-1, unit="lines")
        self.assertEqual(tail["output"], "three\n")
        self.assertFalse(tail["requested_data_dropped"])
        for options in (
            {"wait_ms": -1},
            {"wait_ms": True},
            {"wait_ms": 30001},
            {"unit": "bad"},
            {"unit": "lines", "length": 2001},
            {"unit": "lines", "offset": None},
            {"wait_for_prompt": "true"},
        ):
            with self.subTest(options=options), self.assertRaises(ValueError):
                processes.read(key, **options)

    def test_mcp_line_mode_wait_and_automatic_byte_cursor(self):
        async def run():
            agent = create_agent(self.processes.workspace.policy)
            started = await agent.call_tool(
                "start_process",
                {
                    "command": [
                        sys.executable,
                        "-u",
                        "-c",
                        "print('one\\ntwo\\nthree',flush=True)",
                    ]
                },
            )
            key = json.loads(started.content[0].text)["session_id"]
            try:
                first = await agent.call_tool(
                    "read_process_output",
                    {"session_id": key, "offset": None, "wait_ms": 3000},
                )
                self.assertFalse(first.is_error)
                self.assertIn("three", json.loads(first.content[0].text)["output"])
                lines = await agent.call_tool(
                    "read_process_output",
                    {"session_id": key, "offset": -2, "length": 1, "unit": "lines"},
                )
                self.assertFalse(lines.is_error)
                self.assertEqual(
                    json.loads(lines.content[0].text)["output"].strip(), "two"
                )
            finally:
                await agent.call_tool("force_terminate", {"session_id": key})

        asyncio.run(run())

    def test_waiting_tail_resolves_against_the_returned_output_snapshot(self):
        raw = b"one\ntwo\nthree\n"
        for unit, offset, expected in (("bytes", -3, "ee\n"), ("lines", -1, "three\n")):
            process = Mock()
            process.poll.return_value = None
            session = Session(process, time.monotonic(), 10)
            self.processes.sessions["snapshot-fixture"] = session
            try:
                with patch.object(
                    session.changed,
                    "wait",
                    side_effect=lambda timeout, target=session: target.output.extend(
                        raw
                    ),
                ):
                    result = self.processes.read(
                        "snapshot-fixture", offset=offset, unit=unit, wait_ms=100
                    )
                self.assertEqual(result["output"], expected)
            finally:
                # This pure snapshot fixture owns no OS process or handle.
                del self.processes.sessions["snapshot-fixture"]

    @unittest.skipUnless(os.name == "nt", "Windows Job Object launch barrier")
    def test_windows_command_cannot_run_before_job_assignment(self):
        original = subprocess.Popen
        marker = self.root / "started.txt"

        def delayed_creation(*args, **kwargs):
            process = original(*args, **kwargs)
            # Deliberately expose the OS scheduling gap before start() assigns its job.
            threading.Event().wait(0.2)
            try:
                self.assertIsNone(
                    process.poll(),
                    "Process exited before its ownership job could be assigned",
                )
                self.assertFalse(
                    marker.exists(),
                    "User command ran before the ownership job was assigned",
                )
            except AssertionError:
                process.kill()
                process.wait(timeout=3)
                for stream in (process.stdin, process.stdout):
                    stream.close()
                raise
            return process

        with patch("agentarm.processes.subprocess.Popen", side_effect=delayed_creation):
            result = self.processes.start(
                [
                    sys.executable,
                    "-c",
                    "from pathlib import Path; Path('started.txt').write_text('fixture')",
                ]
            )
        session = self.processes.sessions[result["session_id"]]
        self.assertTrue(session.readers_done.wait(timeout=5))
        session.process.wait(timeout=3)
        self.assertEqual(marker.read_text(), "fixture")

    def test_unicode_argv_binary_stdin_and_exit_code_survive_launcher_frame(self):
        argument = "Türkçe 中文 😀"
        payload = "ü\n" * 10000
        script = "import sys,json,hashlib; data=sys.stdin.buffer.read(); print(json.dumps({'arg':sys.argv[1],'sha256':hashlib.sha256(data).hexdigest()})); sys.exit(9)"
        result = self.processes.start([sys.executable, "-c", script, argument])
        key = result["session_id"]
        self.processes.interact(key, payload)
        self.processes.interact(key, close_stdin=True)
        session = self.processes.sessions[key]
        self.assertTrue(session.readers_done.wait(timeout=5))
        session.process.wait(timeout=3)
        output = self.processes.read(key)
        record = json.loads(output["output"])
        self.assertEqual(record["arg"], argument)
        self.assertEqual(record["sha256"], hashlib.sha256(payload.encode()).hexdigest())
        self.assertEqual(output["exit_code"], 9)
        self.assertEqual(
            output["pid_role"], "owned_launcher" if os.name == "nt" else "command"
        )

    @unittest.skipUnless(os.name == "nt", "Windows asynchronous command launch")
    def test_missing_command_reports_launcher_error_and_exit_127(self):
        result = self.processes.start([str(self.root / "missing-program.exe")])
        key = result["session_id"]
        session = self.processes.sessions[key]
        self.assertTrue(session.readers_done.wait(timeout=5))
        session.process.wait(timeout=3)
        output = self.processes.read(key)
        self.assertEqual(output["exit_code"], 127)
        self.assertIn("FileNotFoundError", output["output"])

    def test_launcher_stop_terminates_the_owned_grandchild(self):
        script = "import subprocess,sys,time; p=subprocess.Popen([sys.executable,'-c','import time; time.sleep(60)']); print(p.pid,flush=True); time.sleep(60)"
        key = self.processes.start([sys.executable, "-u", "-c", script])["session_id"]
        output = ""
        deadline = time.monotonic() + 5
        while not output.endswith("\n"):
            self.assertLess(time.monotonic(), deadline)
            output += self.processes.read(key, offset=None, wait_ms=1000)["output"]
        child = psutil.Process(int(output.strip()))
        self.assertTrue(child.is_running())
        self.processes.stop(key)
        _, alive = psutil.wait_procs([child], timeout=3)
        self.assertFalse(alive, "Owned descendant survived launcher termination")
