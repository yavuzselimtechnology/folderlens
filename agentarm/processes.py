"""Bounded, owned terminal sessions. Execution is an explicit host capability."""

from __future__ import annotations

import json
import os
import queue
import secrets
import signal
import subprocess
import sys
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path

import psutil

from .workspace import Workspace


@dataclass
class Session:
    process: subprocess.Popen
    created: float
    timeout: float
    output: bytearray = field(default_factory=bytearray)
    dropped: int = 0
    ended: bool = False
    timed_out: bool = False
    error: str | None = None
    lock: threading.RLock = field(default_factory=threading.RLock)
    inputs: queue.Queue = field(default_factory=lambda: queue.Queue(maxsize=8))
    readers_done: threading.Event = field(default_factory=threading.Event)
    job: object | None = None
    changed: threading.Condition = field(init=False, repr=False)
    dropped_lines: int = 0
    first_line_prefix_dropped: bool = False
    output_closed: bool = False
    stdin_closed: bool = False
    stdin_close_requested: bool = False
    stdin_write_in_progress: bool = False
    stdin_error: str | None = None
    read_cursor: int = 0
    first_output_at: float | None = None
    completed_at: float | None = None
    output_events: deque = field(default_factory=lambda: deque(maxlen=200))
    output_event_count: int = 0

    def timing(self) -> dict:
        now = time.monotonic()
        return {
            "elapsed_ms": (
                (now if self.completed_at is None else self.completed_at) - self.created
            )
            * 1000,
            "first_output_ms": None
            if self.first_output_at is None
            else (self.first_output_at - self.created) * 1000,
            "completed": self.completed_at is not None,
            "events": list(self.output_events),
            "event_count": self.output_event_count,
            "dropped_events": self.output_event_count - len(self.output_events),
            "event_capacity": self.output_events.maxlen,
            "clock": "monotonic; output events are observed pipe reads, not child write timestamps",
        }

    def __post_init__(self):
        self.changed = threading.Condition(self.lock)

    def prompt_hint(self) -> str | None:
        if self.ended or self.stdin_closed or self.stdin_close_requested:
            return None
        last_line = bytes(self.output[-8192:]).rsplit(b"\n", 1)[-1]
        for name, marker in (
            ("python_primary", b">>> "),
            ("python_continuation", b"... "),
            ("generic_repl", b"> "),
        ):
            if last_line == marker:
                return name
        return None


class Processes:
    def __init__(
        self,
        workspace: Workspace,
        max_sessions: int = 8,
        max_output_bytes: int = 1024 * 1024,
        retention_seconds: float = 3600,
    ):
        self.workspace = workspace
        self.max_sessions = max_sessions
        self.max_output_bytes = max_output_bytes
        self.retention_seconds = retention_seconds
        self.sessions: dict[str, Session] = {}
        self.lock = threading.RLock()

    def cleanup(self) -> None:
        now = time.monotonic()
        for key, session in list(self.sessions.items()):
            if session.ended and now - session.created > self.retention_seconds:
                del self.sessions[key]

    def start(
        self, command: list[str], cwd: str = ".", timeout_seconds: float = 300
    ) -> dict:
        if not self.workspace.policy.allow_exec:
            raise PermissionError(
                "Restart locally with --allow-exec; commands have full OS user access"
            )
        if (
            not command
            or len(command) > 100
            or any(not isinstance(x, str) or "\0" in x for x in command)
            or sum(map(len, command)) > 32768
            or not 1 <= timeout_seconds <= 3600
        ):
            raise ValueError("Use an argv list and timeout 1..3600 seconds")
        directory = self.workspace.path(cwd, root_ok=True)
        if not directory.is_dir():
            raise ValueError("cwd must be an existing workspace directory")
        with self.lock:
            self.cleanup()
            if len(self.sessions) >= self.max_sessions:
                completed = next((k for k, s in self.sessions.items() if s.ended), None)
                if completed is None:
                    raise ValueError("Terminal session limit reached; stop a session")
                del self.sessions[completed]
            env = {
                key: value
                for key, value in os.environ.items()
                if not any(
                    secret in key.upper()
                    for secret in ("TOKEN", "SECRET", "PASSWORD", "API_KEY")
                )
            }
            env["PYTHONIOENCODING"] = "utf-8"
            env["PYTHONUNBUFFERED"] = "1"
            launch_frame = None
            launch_command = command
            if os.name == "nt":
                payload = json.dumps({"command": command}, ensure_ascii=False).encode(
                    "utf-8"
                )
                launch_frame = len(payload).to_bytes(4, "big") + payload
                launch_command = [
                    sys.executable,
                    "-I",
                    "-S",
                    str(Path(__file__).with_name("process_worker.py")),
                ]
            launched_at = time.monotonic()
            process = subprocess.Popen(
                launch_command,
                cwd=directory,
                env=env,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                bufsize=0,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
                start_new_session=os.name != "nt",
            )
            session = Session(process, launched_at, timeout_seconds)
            if os.name == "nt":
                # A kill-on-close job also owns descendants left behind by a REPL.
                import win32api
                import win32con
                import win32job

                job = None
                try:
                    job = win32job.CreateJobObject(
                        None, "FolderLens-" + secrets.token_hex(16)
                    )
                    info = win32job.QueryInformationJobObject(
                        job, win32job.JobObjectExtendedLimitInformation
                    )
                    info["BasicLimitInformation"]["LimitFlags"] = (
                        win32job.JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
                    )
                    win32job.SetInformationJobObject(
                        job, win32job.JobObjectExtendedLimitInformation, info
                    )
                    handle = win32api.OpenProcess(
                        win32con.PROCESS_SET_QUOTA | win32con.PROCESS_TERMINATE,
                        False,
                        process.pid,
                    )
                    try:
                        win32job.AssignProcessToJobObject(job, handle)
                    finally:
                        handle.Close()
                    session.job = job
                    # The fixed worker cannot execute command code before receiving this frame.
                    remaining = memoryview(launch_frame)
                    while remaining:
                        written = process.stdin.write(remaining)
                        if not written:
                            raise BrokenPipeError(
                                "Owned process launcher stopped accepting its frame"
                            )
                        remaining = remaining[written:]
                    process.stdin.flush()
                except Exception:
                    if job is not None:
                        job.Close()
                    process.kill()
                    process.wait()
                    for stream in (process.stdin, process.stdout):
                        stream.close()
                    raise
            key = secrets.token_urlsafe(12)
            self.sessions[key] = session
            threading.Thread(target=self._read, args=(session,), daemon=True).start()
            threading.Thread(target=self._write, args=(session,), daemon=True).start()
            threading.Thread(target=self._watch, args=(session,), daemon=True).start()
            return {
                "session_id": key,
                "pid": process.pid,
                "pid_role": "owned_launcher" if os.name == "nt" else "command",
                "timeout_seconds": timeout_seconds,
                "execution_boundary": "OS user privileges; workspace cwd is not a sandbox",
            }

    def _read(self, session: Session) -> None:
        try:
            while True:
                data = session.process.stdout.read(8192)
                if not data:
                    break
                with session.changed:
                    observed = time.monotonic()
                    if session.first_output_at is None:
                        session.first_output_at = observed
                    start_byte = session.dropped + len(session.output)
                    session.output_events.append(
                        {
                            "sequence": session.output_event_count,
                            "elapsed_ms": (observed - session.created) * 1000,
                            "byte_start": start_byte,
                            "byte_end": start_byte + len(data),
                        }
                    )
                    session.output_event_count += 1
                    session.output.extend(data)
                    overflow = len(session.output) - self.max_output_bytes
                    if overflow > 0:
                        removed = session.output[:overflow]
                        session.dropped_lines += removed.count(b"\n")
                        session.first_line_prefix_dropped = not removed.endswith(b"\n")
                        del session.output[:overflow]
                        session.dropped += overflow
                    session.changed.notify_all()
        except OSError as exc:
            with session.lock:
                session.error = type(exc).__name__
        finally:
            session.process.stdout.close()
            session.readers_done.set()
            with session.changed:
                session.output_closed = True
                session.changed.notify_all()

    def _write(self, session: Session) -> None:
        try:
            while session.process.poll() is None:
                try:
                    data = session.inputs.get(timeout=0.2)
                except queue.Empty:
                    continue
                if data is None:
                    break
                with session.changed:
                    session.stdin_write_in_progress = True
                remaining = memoryview(data)
                while remaining:
                    count = session.process.stdin.write(remaining)
                    if not count:
                        raise BrokenPipeError("Process stdin stopped accepting data")
                    remaining = remaining[count:]
                session.process.stdin.flush()
                with session.changed:
                    session.stdin_write_in_progress = False
                    session.changed.notify_all()
        except (BrokenPipeError, OSError, ValueError) as exc:
            with session.lock:
                session.stdin_error = type(exc).__name__
        finally:
            try:
                session.process.stdin.close()
            except OSError:
                pass
            with session.changed:
                session.stdin_closed = True
                session.stdin_write_in_progress = False
                session.changed.notify_all()

    def _watch(self, session: Session) -> None:
        try:
            session.process.wait(timeout=session.timeout)
        except subprocess.TimeoutExpired:
            session.timed_out = True
            self._terminate(session)
        # Prevent a child retaining stdout or surviving its original parent.
        self._terminate_descendants(session)
        session.readers_done.wait(timeout=2)
        with session.changed:
            if session.completed_at is None:
                session.completed_at = time.monotonic()
            session.ended = True
            session.changed.notify_all()

    def _terminate(self, session: Session) -> None:
        self._terminate_descendants(session)
        try:
            parent = psutil.Process(session.process.pid)
            # Popen's handle tracks the original process; never kill a reused PID.
            if session.process.poll() is not None:
                return
            children = parent.children(recursive=True)
            for child in reversed(children):
                try:
                    child.kill()
                except psutil.NoSuchProcess:
                    pass
            parent.kill()
            psutil.wait_procs([*children, parent], timeout=2)
        except psutil.NoSuchProcess:
            pass
        session.process.wait(timeout=3)

    def _terminate_descendants(self, session: Session) -> None:
        with session.lock:
            if session.job is not None:
                session.job.Close()
                session.job = None
            elif os.name != "nt":
                try:
                    os.killpg(session.process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass

    def read(
        self,
        session_id: str,
        offset: int | None = 0,
        length: int | None = None,
        unit: str = "bytes",
        wait_ms: int = 0,
        wait_for_prompt: bool = False,
        verbose_timing: bool = False,
    ) -> dict:
        length = (16000 if unit == "bytes" else 1000) if length is None else length
        if (
            (offset is not None and type(offset) is not int)
            or type(length) is not int
            or unit not in {"bytes", "lines"}
            or not 1 <= length <= (65536 if unit == "bytes" else 2000)
            or type(wait_ms) is not int
            or not 0 <= wait_ms <= 30000
            or type(wait_for_prompt) is not bool
            or type(verbose_timing) is not bool
            or (unit == "lines" and offset is None)
        ):
            raise ValueError(
                "Use bytes/lines, integer offset (null for new bytes), length 1..65536 bytes/1..2000 lines, wait_ms 0..30000 and boolean wait_for_prompt"
            )
        self.cleanup()
        session = self.sessions[session_id]
        started = time.monotonic()
        deadline = started + wait_ms / 1000
        with session.changed:
            # Wait for data at the initial cursor; resolve tail against the returned snapshot.
            end = session.dropped + len(session.output)
            if unit == "bytes":
                requested = (
                    session.read_cursor
                    if offset is None
                    else max(0, end + offset)
                    if offset < 0
                    else offset
                )
                ready = lambda: (
                    session.dropped + len(session.output)
                    > max(requested, session.dropped)
                )
            else:
                from .terminal_output import line_window

                line_end = (
                    session.dropped_lines
                    + session.output.count(b"\n")
                    + int(bool(session.output) and not session.output.endswith(b"\n"))
                )
                requested = max(0, line_end + offset) if offset < 0 else offset
                ready = lambda: (
                    session.dropped_lines
                    + session.output.count(b"\n")
                    + int(bool(session.output) and not session.output.endswith(b"\n"))
                    > max(requested, session.dropped_lines)
                )

            def wait_satisfied():
                return ready() and (
                    not wait_for_prompt or session.prompt_hint() is not None
                )

            while (
                not wait_satisfied() and not session.ended and not session.output_closed
            ):
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                session.changed.wait(remaining)
            end = session.dropped + len(session.output)
            if unit == "bytes":
                if offset is not None and offset < 0:
                    requested = max(0, end + offset)
                start = max(requested, session.dropped)
                position = start - session.dropped
                data = bytes(session.output[position : position + length])
                window = {
                    "unit": "bytes",
                    "output": data.decode("utf-8", "replace"),
                    "offset": start,
                    "next_offset": start + len(data),
                    "available_start": session.dropped,
                    "available_end": end,
                    "requested_data_dropped": requested < session.dropped,
                    "encoding": "UTF-8 with replacement; byte offsets, no ANSI/terminal emulation",
                }
                if offset is None:
                    session.read_cursor = window["next_offset"]
            else:
                window = line_window(
                    bytes(session.output),
                    session.dropped_lines,
                    session.first_line_prefix_dropped,
                    session.dropped,
                    offset if offset < 0 else requested,
                    length,
                    self.workspace.policy.max_output_chars,
                    session.ended or session.output_closed,
                )
            prompt = session.prompt_hint()
            if wait_for_prompt:
                wait_status = (
                    "prompt_detected"
                    if ready() and prompt is not None
                    else "completed"
                    if session.ended
                    else "output_closed"
                    if session.output_closed
                    else "timeout"
                    if wait_ms
                    else "not_waiting"
                )
            else:
                wait_status = (
                    "data_available"
                    if window["output"]
                    else "completed"
                    if session.ended
                    else "output_closed"
                    if session.output_closed
                    else "timeout"
                    if wait_ms
                    else "not_waiting"
                )
            return {
                "session_id": session_id,
                "pid_role": "owned_launcher" if os.name == "nt" else "command",
                "dropped_bytes": session.dropped,
                "status": "completed" if session.ended else "running",
                "exit_code": session.process.poll(),
                "timed_out": session.timed_out,
                "error": session.error,
                "waited_ms": round((time.monotonic() - started) * 1000, 2),
                "wait_status": wait_status,
                "wait_for_prompt": wait_for_prompt,
                "stdin_closed": session.stdin_closed,
                "stdin_close_requested": session.stdin_close_requested,
                "stdin_write_in_progress": session.stdin_write_in_progress,
                "stdin_error": session.stdin_error,
                "input_queue_size": session.inputs.qsize(),
                "input_queue_capacity": session.inputs.maxsize,
                "prompt_hint": prompt,
                "prompt_hint_is_heuristic": True,
                "waiting_for_input": None,
                **({"timing": session.timing()} if verbose_timing else {}),
                **window,
            }

    def interact(
        self,
        session_id: str,
        text: str = "",
        close_stdin: bool = False,
        wait_ms: int = 0,
        wait_for_prompt: bool = False,
        verbose_timing: bool = False,
    ) -> dict:
        if not self.workspace.policy.allow_exec:
            raise PermissionError(
                "Process interaction has been disabled locally or by capability revocation"
            )
        if (
            type(wait_ms) is not int
            or not 0 <= wait_ms <= 30000
            or type(wait_for_prompt) is not bool
            or type(verbose_timing) is not bool
        ):
            raise ValueError(
                "wait_ms must be 0..30000; prompt/timing flags must be booleans"
            )
        session = self.sessions[session_id]
        data = text.encode("utf-8")
        if len(data) > 65536:
            raise ValueError("Input exceeds 64 KiB")
        if session.process.poll() is not None:
            raise ValueError("Session has exited")
        with session.changed:
            output_start = session.dropped + len(session.output)
            if session.stdin_closed or session.stdin_close_requested:
                raise ValueError("Session stdin is closed or closing")
            try:
                session.inputs.put_nowait(data if not close_stdin else None)
            except queue.Full as exc:
                raise ValueError(
                    "Input queue full; process is not consuming input"
                ) from exc
            session.stdin_close_requested = close_stdin
            session.changed.notify_all()
        queued = {"queued": True, "closed_stdin": close_stdin}
        if wait_ms or wait_for_prompt or verbose_timing:
            return {
                **queued,
                **self.read(
                    session_id,
                    offset=output_start,
                    wait_ms=wait_ms,
                    wait_for_prompt=wait_for_prompt,
                    verbose_timing=verbose_timing,
                ),
            }
        return queued

    def stop(self, session_id: str) -> dict:
        session = self.sessions[session_id]
        self._terminate(session)
        session.readers_done.wait(timeout=2)
        with session.changed:
            if session.completed_at is None:
                session.completed_at = time.monotonic()
            session.ended = True
            session.changed.notify_all()
        return self.read(session_id)

    def list_sessions(self) -> list[dict]:
        self.cleanup()
        return [
            {
                "session_id": key,
                "pid": session.process.pid,
                "pid_role": "owned_launcher" if os.name == "nt" else "command",
                "status": "completed" if session.ended else "running",
                "exit_code": session.process.poll(),
            }
            for key, session in self.sessions.items()
        ]

    def close(self) -> None:
        for key in list(self.sessions):
            self.stop(key)
