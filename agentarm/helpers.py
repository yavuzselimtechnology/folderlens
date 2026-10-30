"""Bounded JSON exchanges with fixed, process-owned document helpers."""

from __future__ import annotations

import json
import queue
import sys
import tempfile
from dataclasses import replace
from pathlib import Path

from .processes import Processes
from .workspace import Workspace


def run_helper(
    workspace: Workspace, worker: str, request: dict, max_output: int
) -> dict:
    if worker not in {"pdf_worker.py", "workbook_worker.py"}:
        raise ValueError("Unknown fixed document helper")
    # Only fixed application code receives internal execution authority.
    owned = Processes(
        Workspace(replace(workspace.policy, allow_exec=True)),
        max_sessions=1,
        max_output_bytes=max_output,
        retention_seconds=60,
    )
    with tempfile.TemporaryDirectory(prefix="folderlens-helper-") as runtime:
        payload = json.dumps(
            {**request, "runtime_dir": runtime}, ensure_ascii=True
        ).encode("utf-8")
        if len(payload) > 32 * 1024 * 1024:
            raise ValueError("Encoded helper payload exceeds 32 MiB")
        try:
            started = owned.start(
                [sys.executable, str(Path(__file__).with_name(worker))],
                timeout_seconds=45,
            )
            session = owned.sessions[started["session_id"]]
            for position in range(0, len(payload), 65536):
                session.inputs.put(payload[position : position + 65536], timeout=10)
            session.inputs.put(None, timeout=10)
            if not session.readers_done.wait(timeout=50):
                raise TimeoutError(
                    "Document helper did not finish within its time budget"
                )
            session.process.wait(timeout=5)
            with session.lock:
                if session.timed_out:
                    raise TimeoutError("Document operation exceeded 45 seconds")
                if session.dropped:
                    raise ValueError(
                        "Document helper response exceeded its output budget"
                    )
                raw = bytes(session.output)
            try:
                result = json.loads(raw)
            except (UnicodeError, ValueError) as exc:
                raise ValueError(
                    "Document helper returned an invalid response"
                ) from exc
            if not isinstance(result, dict):
                raise TypeError("Document helper returned an invalid object")
            if "error" in result:
                raise ValueError(result["error"])
            if session.process.returncode:
                raise ValueError("Document helper failed")
            return result
        except queue.Full as exc:
            raise TimeoutError(
                "Document helper stopped consuming its bounded input queue"
            ) from exc
        finally:
            owned.close()
