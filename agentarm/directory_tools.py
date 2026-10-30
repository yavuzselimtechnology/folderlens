"""Complete, bounded tree fingerprints and no-replacement directory renames."""

from __future__ import annotations

import errno
import hashlib
import json
import os
import stat
import sys
import time
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .workspace import Workspace

TREE_SCAN_SECONDS = 10


def inspect_tree(
    workspace: Workspace, path: str, destination: str | None = None
) -> dict:
    base = workspace.path(path, root_ok=True)
    if not base.is_dir():
        raise ValueError("Expected a directory")
    stack = [base]
    records = []
    observed = []
    entries = directories = files = content_bytes = manifest_bytes = 0
    deadline = time.monotonic() + TREE_SCAN_SECONDS

    def record(relative, kind, info, content_hash=None):
        nonlocal entries, manifest_bytes
        entries += 1
        if entries > workspace.policy.max_scan_files:
            raise ValueError("Directory tree exceeds the entry scan budget")
        row = [relative, kind, stat.S_IMODE(info.st_mode)]
        if kind == "file":
            row.extend([info.st_size, info.st_mtime_ns, content_hash])
        encoded = json.dumps(row, ensure_ascii=True, separators=(",", ":")).encode(
            "ascii"
        )
        manifest_bytes += len(encoded)
        if manifest_bytes > workspace.policy.max_undo_bytes:
            raise ValueError("Directory fingerprint exceeds the metadata memory budget")
        records.append(encoded)
        observed.append((relative, info))

    record(".", "directory", base.stat())
    while stack:
        directory = stack.pop()
        relative_directory = directory.relative_to(workspace.policy.root).as_posix()
        workspace.path(relative_directory, root_ok=True)
        if time.monotonic() > deadline:
            raise TimeoutError(
                "Directory scan exceeded its 10 second budget; choose a smaller subtree"
            )
        with os.scandir(directory) as children:
            for child in children:
                if time.monotonic() > deadline:
                    raise TimeoutError(
                        "Directory scan exceeded its 10 second budget; choose a smaller subtree"
                    )
                relative = Path(child.path).relative_to(base).as_posix()
                name = Path(child.path).relative_to(workspace.policy.root).as_posix()
                checked = workspace.path(name)
                if destination is not None:
                    workspace.path(destination + "/" + relative)
                info = checked.stat()
                if stat.S_ISDIR(info.st_mode):
                    record(relative, "directory", info)
                    stack.append(checked)
                    directories += 1
                elif stat.S_ISREG(info.st_mode):
                    if info.st_size + content_bytes > workspace.policy.max_file_bytes:
                        raise ValueError(
                            "Directory contents exceed the aggregate hash byte budget"
                        )
                    hashed = hashlib.sha256()
                    with checked.open("rb") as source:
                        while chunk := source.read(
                            min(
                                1024 * 1024,
                                workspace.policy.max_file_bytes - content_bytes + 1,
                            )
                        ):
                            content_bytes += len(chunk)
                            if content_bytes > workspace.policy.max_file_bytes:
                                raise ValueError(
                                    "Directory contents exceed the aggregate hash byte budget"
                                )
                            if time.monotonic() > deadline:
                                raise TimeoutError(
                                    "Directory hashing exceeded its scan time budget"
                                )
                            hashed.update(chunk)
                        after = os.fstat(source.fileno())
                    if (info.st_size, info.st_mtime_ns, info.st_mode, info.st_ino) != (
                        after.st_size,
                        after.st_mtime_ns,
                        after.st_mode,
                        after.st_ino,
                    ):
                        raise ValueError(
                            "Directory file changed while being hashed; inspect again"
                        )
                    record(relative, "file", info, hashed.hexdigest())
                    files += 1
                else:
                    raise ValueError(
                        "Directory move excludes sockets, devices and other special entries"
                    )
    # Recheck every captured identity/metadata after traversing the whole tree:
    # a later subdirectory scan may overlap an external edit to an earlier entry.
    for relative, before in observed:
        if time.monotonic() > deadline:
            raise TimeoutError("Directory verification exceeded its scan time budget")
        target = base if relative == "." else base / relative
        name = target.relative_to(workspace.policy.root).as_posix()
        after = workspace.path(name, root_ok=True).stat()
        if (
            before.st_mode,
            before.st_size,
            before.st_mtime_ns,
            before.st_ino,
            before.st_dev,
        ) != (
            after.st_mode,
            after.st_size,
            after.st_mtime_ns,
            after.st_ino,
            after.st_dev,
        ):
            raise ValueError(
                "Directory tree changed while being inspected; inspect again"
            )
    hashed = hashlib.sha256()
    for row in sorted(records):
        hashed.update(row + b"\n")
    return {
        "sha256": hashed.hexdigest(),
        "hash_kind": "directory_tree_v1",
        "files": files,
        "directories": directories,
        "entries": entries,
        "total_bytes": content_bytes,
    }


def rename_no_replace(source: Path, destination: Path) -> None:
    """Atomic same-filesystem rename that fails even if a competing target appears."""
    if os.name == "nt":
        os.rename(source, destination)
        return
    if sys.platform == "linux":
        import ctypes

        function = getattr(ctypes.CDLL(None, use_errno=True), "renameat2", None)
        if function is None:
            raise OSError(errno.ENOSYS, "Safe no-replacement rename is unavailable")
        function.argtypes = [
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_uint,
        ]
        function.restype = ctypes.c_int
        # Linux UAPI constants: AT_FDCWD and RENAME_NOREPLACE (not an overwriting rename).
        if function(-100, os.fsencode(source), -100, os.fsencode(destination), 1) != 0:
            code = ctypes.get_errno()
            raise OSError(code, os.strerror(code), str(destination))
        return
    raise OSError(
        errno.ENOTSUP, "Atomic directory moves currently support Windows and Linux"
    )
