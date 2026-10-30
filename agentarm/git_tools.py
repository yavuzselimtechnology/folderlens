"""Read-only Git subprocess path, available without the terminal capability."""

import os
import subprocess
import threading

from .workspace import Workspace


def inspect_git(
    workspace: Workspace,
    action: str = "status",
    revision: str = "HEAD",
    path: str = ".",
    limit: int = 30,
    offset: int = 0,
) -> dict:
    if not 1 <= limit <= 200 or offset < 0:
        raise ValueError("Use limit 1..200 and offset >= 0")
    if (
        not revision
        or revision.startswith("-")
        or len(revision) > 256
        or any(c in revision for c in "\0\n\r")
    ):
        raise ValueError("Invalid revision")
    workspace.path(path, root_ok=True)
    if action in {"diff", "show"} and path == ".":
        raise ValueError(
            "Specify an eligible file path for diff/show to avoid exposing protected files"
        )
    commands = {
        "status": ["status", "--short", "--untracked-files=normal", "--", path],
        "log": [
            "log",
            "--all",
            f"--max-count={limit}",
            f"--skip={offset}",
            "--format=fuller",
            "--",
            path,
        ],
        "diff": ["diff", "--no-ext-diff", "--no-textconv", revision, "--", path],
        "show": [
            "show",
            "--no-ext-diff",
            "--no-textconv",
            "--format=fuller",
            revision,
            "--",
            path,
        ],
    }
    if action not in commands:
        raise ValueError("Git action must be status, log, diff or show")
    command = [
        "git",
        "--no-pager",
        "--no-optional-locks",
        "-c",
        "core.fsmonitor=false",
        "-c",
        "core.hooksPath=",
        "-c",
        "core.quotePath=false",
        *commands[action],
    ]
    try:
        process = subprocess.Popen(
            command,
            cwd=workspace.policy.root,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
    except FileNotFoundError as exc:
        raise ValueError("Git is not installed or not on PATH") from exc
    expired = threading.Event()

    def stop():
        expired.set()
        process.kill()

    timer = threading.Timer(15, stop)
    timer.daemon = True
    timer.start()
    try:
        data = process.stdout.read(workspace.policy.max_output_chars + 1)
        if len(data) > workspace.policy.max_output_chars:
            process.kill()
        process.wait(timeout=3)
    finally:
        timer.cancel()
        if process.poll() is None:
            process.kill()
            process.wait()
        process.stdout.close()
    if expired.is_set():
        raise ValueError("Git inspection timed out")
    truncated = len(data) > workspace.policy.max_output_chars
    return {
        "action": action,
        "exit_code": process.returncode,
        "output": data[: workspace.policy.max_output_chars].decode(
            "utf-8", errors="replace"
        ),
        "truncated": truncated,
        "offset": offset,
        "next_offset_hint": offset + limit if action == "log" and data else None,
        "coverage": "Git may include tracked sensitive content; owner-authorized repository inspection",
    }
