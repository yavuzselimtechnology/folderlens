"""Bounded breadth-first directory discovery, using the workspace path policy."""

from __future__ import annotations

import json
import os
import stat
import time
from collections import deque
from pathlib import Path


def list_directory(workspace, path: str, offset: int, limit: int, depth: int) -> dict:
    if (
        type(offset) is not int
        or offset < 0
        or type(limit) is not int
        or not 1 <= limit <= 500
        or type(depth) is not int
        or depth < 1
    ):
        raise ValueError("Use offset >= 0, limit 1..500 and integer depth >= 1")
    base = workspace.path(path, root_ok=True)
    if not base.is_dir():
        raise ValueError("Expected a directory")
    pending = deque([(base, 1)])
    result = {
        "entries": [],
        "next_offset": None,
        "order": "breadth-first filesystem order; directory changes can alter pagination",
        "depth": depth,
        "scanned": 0,
        "status": "completed",
    }
    visible = 0
    deadline = time.monotonic() + 2  # Same bounded discovery slice as file search.

    def entries():
        while pending:
            if time.monotonic() >= deadline:
                result["status"] = "time_limit"
                return
            directory, level = pending.popleft()
            rel_directory = directory.relative_to(workspace.policy.root).as_posix()
            try:
                # Revalidate queued directories before opening, including reparse points.
                checked = workspace.path(rel_directory, root_ok=True)
                with os.scandir(checked) as items:
                    for item in items:
                        if result["scanned"] >= workspace.policy.max_scan_files:
                            result["status"] = "scan_limit"
                            return
                        if time.monotonic() >= deadline:
                            result["status"] = "time_limit"
                            return
                        result["scanned"] += 1
                        relative = (
                            Path(item.path)
                            .relative_to(workspace.policy.root)
                            .as_posix()
                        )
                        try:
                            info = workspace.path(relative).stat()
                        except (OSError, ValueError):
                            continue
                        is_directory = stat.S_ISDIR(info.st_mode)
                        if not is_directory and not stat.S_ISREG(info.st_mode):
                            continue
                        if is_directory and level < depth:
                            pending.append((Path(item.path), level + 1))
                        yield {
                            "path": relative,
                            "kind": "directory" if is_directory else "file",
                            "size": info.st_size,
                            "depth": level,
                        }
            except (OSError, ValueError):
                if directory == base:
                    raise
                yield {
                    "path": rel_directory,
                    "kind": "error",
                    "error": "directory_unavailable",
                    "depth": level - 1,
                }

    iterator = entries()
    try:
        for entry in iterator:
            if visible < offset:
                visible += 1
                continue
            if len(result["entries"]) >= limit:
                result.update(
                    status="page_limit", next_offset=offset + len(result["entries"])
                )
                break
            result["entries"].append(entry)
            # Include continuation metadata in the character budget calculation.
            result.update(
                status="page_limit", next_offset=offset + len(result["entries"])
            )
            if (
                len(
                    json.dumps(
                        {**result, "scanned": workspace.policy.max_scan_files},
                        ensure_ascii=False,
                    )
                )
                > workspace.policy.max_output_chars
            ):
                result["entries"].pop()
                if not result["entries"]:
                    raise ValueError(
                        "Directory entry exceeds response budget; select a narrower directory"
                    )
                result["next_offset"] = offset + len(result["entries"])
                break
            result.update(status="completed", next_offset=None)
    finally:
        iterator.close()
    return result
