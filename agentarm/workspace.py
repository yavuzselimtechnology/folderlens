"""Local agent filesystem. Never imported by the public snapshot server."""

from __future__ import annotations

import difflib
import fnmatch
import hashlib
import io
import itertools
import json
import os
import secrets
import stat
import sys
import threading
import time
from collections import OrderedDict, deque
from collections.abc import Iterator
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Literal

from .device_config import DeviceConfig
from .paths import is_ignored_path, is_sensitive_path, safe_relative_path
from .search_history import SearchHistory
from .snapshots import DOCUMENT_EXTENSIONS, _decode_text, _extract_document


@dataclass(frozen=True)
class AgentPolicy:
    root: Path
    allow_write: bool = False
    allow_exec: bool = False
    max_file_bytes: int = 16 * 1024 * 1024
    max_output_chars: int = 64 * 1024
    max_undo_bytes: int = 16 * 1024 * 1024
    max_undo_records: int = 20
    max_searches: int = 8
    search_ttl: float = 300
    max_scan_files: int = 100_000
    url_hosts: tuple[str, ...] = ()
    protected_paths: tuple[Path, ...] = ()
    device_config: DeviceConfig = field(default_factory=DeviceConfig)

    def __post_init__(self) -> None:
        root = self.root.expanduser().resolve(strict=True)
        if not root.is_dir():
            raise ValueError("Workspace root must be a directory")
        object.__setattr__(self, "root", root)
        object.__setattr__(
            self,
            "protected_paths",
            tuple(path.expanduser().resolve() for path in self.protected_paths),
        )


@dataclass(frozen=True)
class DirectoryMoveUndo:
    source: str
    destination: str
    sha256: str
    total_bytes: int
    entries: int


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def text_line_window(text: str, offset: int, line_count: int, budget: int) -> dict:
    """Bound whole-line windows without changing source line endings."""
    if not 1 <= line_count <= 2000:
        raise ValueError("Use 1..2000 lines per window")
    total = sum(1 for _ in io.StringIO(text, newline=""))
    start = min(total, max(0, total + offset if offset < 0 else offset))
    lines = []
    used = 0
    for line in itertools.islice(
        io.StringIO(text, newline=""), start, start + line_count
    ):
        if used + len(line) > budget:
            if not lines:
                raise ValueError(
                    "Line exceeds character budget; use read_file character windows"
                )
            break
        lines.append(line)
        used += len(line)
    end = start + len(lines)
    return {
        "offset": start,
        "content": "".join(lines),
        "line_count": len(lines),
        "total_lines": total,
        "next_offset": end if end < total else None,
    }


class Workspace:
    def __init__(self, policy: AgentPolicy):
        self.policy = policy
        # The application directory is an import root, not just a collection of
        # existing .py files: adding a shadow module/package there also runs code.
        # File-tool authority must never rewrite code trusted by document helpers
        # or by the next server start, even when this workspace contains the app.
        self.runtime_paths = tuple(
            dict.fromkeys(
                (
                    Path(__file__).resolve().parent.parent,
                    Path(sys.prefix).resolve(),
                    Path(sys.base_prefix).resolve(),
                )
            )
        )
        self.lock = threading.RLock()
        self.undo: OrderedDict[
            str, tuple[str, bytes | None, str | None] | DirectoryMoveUndo
        ] = OrderedDict()
        self.undo_bytes = 0
        self.searches: dict[str, dict[str, Any]] = {}

    def reduce_config(self, key: str, value: bool | int) -> dict:
        """Tools may revoke capabilities or reduce budgets, never widen local authority."""
        with self.lock:
            if key in {"allow_write", "allow_exec"}:
                if value is not False:
                    raise ValueError(
                        "Capabilities can only be disabled; re-enable by restarting locally"
                    )
            elif key in {"max_file_bytes", "max_scan_files"}:
                if type(value) is not int or not 1 <= value <= getattr(
                    self.policy, key
                ):
                    raise ValueError("Budgets may only decrease to a positive integer")
                for record in self.undo.values():
                    if isinstance(record, DirectoryMoveUndo):
                        prevented = (
                            record.total_bytes > value
                            if key == "max_file_bytes"
                            else record.entries > value
                        )
                    elif key == "max_file_bytes":
                        path, before, _ = record
                        prevented = (before is not None and len(before) > value) or (
                            self.path(path).is_file()
                            and self.path(path).stat().st_size > value
                        )
                    else:
                        prevented = False
                    if prevented:
                        raise ValueError(
                            "This budget would prevent an existing undo; finish recovery first"
                        )
            else:
                raise ValueError(
                    "Only allow_write, allow_exec, max_file_bytes and max_scan_files may be reduced"
                )
            self.policy = replace(self.policy, **{key: value})
            return {
                "key": key,
                "value": getattr(self.policy, key),
                "restart_required_to_expand": True,
            }

    def path(self, name: str, *, root_ok: bool = False) -> Path:
        if name == "." and root_ok:
            return self.policy.root
        name = safe_relative_path(name)
        # Add a sentinel so the existing browser directory policy covers the leaf.
        if is_sensitive_path(name) or is_ignored_path(name + "/_"):
            raise PermissionError("Protected or generated path")
        if any(part.casefold() == ".folderlens" for part in Path(name).parts):
            raise PermissionError("Protected agent state")
        path = self.policy.root
        for part in name.split("/"):
            path = path / part
            try:
                info = path.lstat()
            except FileNotFoundError:
                continue
            if (
                stat.S_ISLNK(info.st_mode)
                or getattr(info, "st_file_attributes", 0) & 0x400
            ):
                raise PermissionError("Symbolic links and reparse points are excluded")
        resolved = path.resolve()
        if any(
            resolved == protected or resolved.is_relative_to(protected)
            for protected in self.policy.protected_paths
        ):
            raise PermissionError("Private agent authorization state")
        if not resolved.is_relative_to(self.policy.root):
            raise PermissionError("Path leaves workspace")
        return path

    def writable(self) -> None:
        if not self.policy.allow_write:
            raise PermissionError(
                "Restart locally with --allow-write to enable changes"
            )

    def mutation_path(self, name: str) -> Path:
        """Reject runtime descendants and ancestor renames; reads stay available."""
        path = self.path(name)
        resolved = path.resolve()
        if any(
            resolved == runtime
            or resolved.is_relative_to(runtime)
            or runtime.is_relative_to(resolved)
            for runtime in self.runtime_paths
        ):
            raise PermissionError(
                "Trusted runtime is read-only through file tools; use a separate "
                "workspace for data and an owner-authorized execution workflow "
                "for runtime maintenance"
            )
        return path

    def bytes(self, name: str) -> bytes:
        path = self.path(name)
        if not path.is_file():
            raise ValueError("Expected a regular file")
        with path.open("rb") as source:
            data = source.read(self.policy.max_file_bytes + 1)
        if len(data) > self.policy.max_file_bytes:
            raise ValueError("File exceeds agent read/write budget")
        return data

    def info(self, name: str = ".", inspect_tree: bool = False) -> dict:
        path = self.path(name, root_ok=True)
        info = path.stat()
        result = {
            "path": name,
            "kind": "directory" if path.is_dir() else "file",
            "size": info.st_size,
            "modified_ns": info.st_mtime_ns,
            "created_ns": getattr(
                info, "st_birthtime_ns", info.st_ctime_ns if os.name == "nt" else None
            ),
            "mode": stat.filemode(info.st_mode),
            "permission_representation": "mode bits; Windows ACLs are not represented",
        }
        if path.is_file() and info.st_size <= self.policy.max_file_bytes:
            data = self.bytes(name)
            result["sha256"] = digest(data)
            result["hash_kind"] = "file_bytes"
            if Path(name).suffix.casefold() not in DOCUMENT_EXTENSIONS:
                try:
                    encoding, text = _decode_text(data)
                except ValueError:
                    pass
                else:
                    if "\0" not in text:
                        lines = sum(1 for _ in io.StringIO(text, newline=""))
                        result.update(
                            encoding=encoding,
                            line_count=lines,
                            last_line=lines - 1 if lines else None,
                        )
        if path.is_dir() and inspect_tree:
            from .directory_tools import inspect_tree as inspect

            result.update(inspect(self, name))
        return result

    def walk(self, name: str = ".") -> Iterator[str]:
        base = self.path(name, root_ok=True)
        for directory, dirs, files in os.walk(base, followlinks=False):
            safe_dirs = []
            for entry in sorted(dirs):
                rel = (Path(directory) / entry).relative_to(self.policy.root).as_posix()
                try:
                    self.path(rel)
                    safe_dirs.append(entry)
                except (OSError, ValueError):
                    continue
            dirs[:] = safe_dirs
            for entry in sorted(files):
                rel = (Path(directory) / entry).relative_to(self.policy.root).as_posix()
                try:
                    self.path(rel)
                    yield rel
                except (OSError, ValueError):
                    continue

    def list_directory(
        self, path: str = ".", offset: int = 0, limit: int = 100, depth: int = 1
    ) -> dict:
        from .directory_listing import list_directory

        return list_directory(self, path, offset, limit, depth)

    def read_file(self, path: str, offset: int = 0, length: int = 16000) -> dict:
        if offset < 0 or not 1 <= length <= self.policy.max_output_chars:
            raise ValueError("Invalid character window")
        data = self.bytes(path)
        if Path(path).suffix.casefold() in DOCUMENT_EXTENSIONS:
            doc = _extract_document(1, path, data)
            if doc["kind"] == "extraction_error":
                raise ValueError(doc["error"])
            content = json.dumps(doc, ensure_ascii=False)
            representation = "extracted_document_json"
        else:
            _, content = _decode_text(data)
            representation = "text"
        end = min(offset + length, len(content))
        return {
            "path": path,
            "sha256": digest(data),
            "representation": representation,
            "offset": offset,
            "content": content[offset:end],
            "total_chars": len(content),
            "next_offset": end if end < len(content) else None,
        }

    def _current(self, path: str, expected: str | None) -> bytes | None:
        target = self.path(path)
        before = self.bytes(path) if target.exists() else None
        actual = digest(before) if before is not None else None
        if actual != expected:
            raise ValueError(
                "SHA-256 conflict: read the current file and retry; null is only for a new file"
            )
        return before

    def read_lines(
        self,
        path: str,
        offset: int = 0,
        line_count: int = 200,
        expected_sha256: str | None = None,
    ) -> dict:
        if not 1 <= line_count <= 2000:
            raise ValueError("Use 1..2000 lines per window")
        if Path(path).suffix.casefold() in DOCUMENT_EXTENSIONS:
            raise ValueError(
                "Line windows are for text files; use document tools for this file"
            )
        data = self.bytes(path)
        source_hash = digest(data)
        if expected_sha256 is not None and source_hash != expected_sha256:
            raise ValueError("SHA-256 conflict: file changed between line windows")
        _, text = _decode_text(data)
        return {
            "path": path,
            "sha256": source_hash,
            **text_line_window(text, offset, line_count, self.policy.max_output_chars),
        }

    def _remember(self, path: str, before: bytes | None, after: str | None) -> str:
        return self._store_undo((path, before, after))

    @staticmethod
    def _undo_size(record) -> int:
        if isinstance(record, DirectoryMoveUndo):
            return 0
        return len(record[1]) if record[1] else 0

    def _store_undo(self, record) -> str:
        size = self._undo_size(record)
        while self.undo and (
            self.undo_bytes + size > self.policy.max_undo_bytes
            or len(self.undo) >= self.policy.max_undo_records
        ):
            _, old = self.undo.popitem(last=False)
            self.undo_bytes -= self._undo_size(old)
        key = secrets.token_urlsafe(12)
        self.undo[key] = record
        self.undo_bytes += size
        return key

    def _atomic_write(self, path: str, data: bytes) -> None:
        target = self.mutation_path(path)
        if not target.parent.is_dir():
            raise ValueError("Parent directory is missing; create it first")
        temporary = target.with_name(".folderlens-" + secrets.token_hex(12))
        try:
            with temporary.open("xb") as output:
                output.write(data)
                output.flush()
                os.fsync(output.fileno())
            if target.exists():
                os.chmod(temporary, stat.S_IMODE(target.stat().st_mode))
            self.mutation_path(path)
            os.replace(temporary, target)
        finally:
            temporary.unlink(missing_ok=True)

    def write_bytes(self, path: str, data: bytes, base_sha256: str | None) -> dict:
        self.writable()
        self.mutation_path(path)
        if len(data) > min(self.policy.max_file_bytes, self.policy.max_undo_bytes):
            raise ValueError("Write exceeds file/undo budget")
        with self.lock:
            before = self._current(path, base_sha256)
            self._atomic_write(path, data)
            after = digest(data)
            undo_id = self._remember(path, before, after)
        return {"path": path, "sha256": after, "bytes": len(data), "undo_id": undo_id}

    def write_file(
        self,
        path: str,
        content: str,
        base_sha256: str | None,
        preview: bool = False,
        mode: Literal["rewrite", "append"] = "rewrite",
    ) -> dict:
        if mode not in {"rewrite", "append"}:
            raise ValueError("Use rewrite or append mode")
        data = content.encode("utf-8")
        if len(data) > self.policy.max_file_bytes:
            raise ValueError("Write exceeds file budget")
        with self.lock:
            before = self._current(path, base_sha256)
            old = ""
            if mode == "append":
                if Path(path).suffix.casefold() in DOCUMENT_EXTENSIONS:
                    raise ValueError(
                        "Append is for text files; use document tools for this format"
                    )
                encoding, old = (
                    _decode_text(before) if before is not None else ("utf-8", "")
                )
                if "\0" in old:
                    raise ValueError("Append requires a text file without NUL bytes")
                if encoding in {"utf-16", "utf-32"}:
                    # Preserve the original BOM and byte order; never insert another BOM.
                    byte_order = "le" if before.startswith(b"\xff\xfe") else "be"
                    data = content.encode(f"{encoding}-{byte_order}")
                data = (before or b"") + data
                content = old + content
                if len(data) > self.policy.max_file_bytes:
                    raise ValueError("Append exceeds file budget")
            if preview:
                if mode == "rewrite":
                    old = _decode_text(before)[1] if before is not None else ""
                diff = "".join(
                    difflib.unified_diff(
                        old.splitlines(keepends=True),
                        content.splitlines(keepends=True),
                        fromfile=path,
                        tofile=path,
                    )
                )
                return {
                    "preview": True,
                    "diff": diff[: self.policy.max_output_chars],
                    "truncated": len(diff) > self.policy.max_output_chars,
                    "sha256": digest(data),
                }
            return self.write_bytes(path, data, base_sha256)

    def edit_block(
        self,
        path: str,
        old_string: str,
        new_string: str,
        base_sha256: str,
        expected_replacements: int = 1,
        preview: bool = True,
    ) -> dict:
        if not old_string or not 1 <= expected_replacements <= 1000:
            raise ValueError(
                "Nonempty old_string and replacement count 1..1000 required"
            )
        with self.lock:
            original = self._current(path, base_sha256)
            encoding, content = _decode_text(original)
            if content.count(old_string) != expected_replacements:
                raise ValueError(
                    "Replacement count mismatch; use unique surrounding context"
                )
            changed = content.replace(old_string, new_string)
            if preview:
                result = self.write_file(path, changed, base_sha256, True)
                result["encoding"] = encoding
                return result
            return self.write_bytes(path, changed.encode(encoding), base_sha256)

    def delete_file(self, path: str, base_sha256: str) -> dict:
        self.writable()
        with self.lock:
            self.mutation_path(path)
            before = self._current(path, base_sha256)
            if before is None:
                raise FileNotFoundError(path)
            if len(before) > self.policy.max_undo_bytes:
                raise ValueError("Delete exceeds undo budget")
            self.mutation_path(path).unlink()
            return {
                "path": path,
                "deleted": True,
                "undo_id": self._remember(path, before, None),
            }

    def undo_change(self, undo_id: str) -> dict:
        self.writable()
        with self.lock:
            record = self.undo[undo_id]
            if isinstance(record, DirectoryMoveUndo):
                result = self._move_directory(
                    record.destination, record.source, record.sha256
                )
                del self.undo[undo_id]
                return {
                    "path": record.source,
                    "kind": "directory",
                    "restored": True,
                    **result,
                }
            path, before, after = record
            self.mutation_path(path)
            self._current(path, after)
            if before is None:
                self.mutation_path(path).unlink()
            else:
                self._atomic_write(path, before)
            del self.undo[undo_id]
            self.undo_bytes -= len(before) if before else 0
            return {
                "path": path,
                "restored": True,
                "sha256": digest(before) if before is not None else None,
            }

    def create_directory(self, path: str) -> dict:
        self.writable()
        with self.lock:
            self.mutation_path(path).mkdir(parents=True, exist_ok=True)
            return {"path": path, "created": True}

    def transfer_file(
        self, source: str, destination: str, base_sha256: str, move: bool = False
    ) -> dict:
        self.writable()
        with self.lock:
            self.mutation_path(destination)
            if move:
                self.mutation_path(source)
            if move and self.path(source).is_dir():
                result = self._move_directory(source, destination, base_sha256)
                undo_id = self._store_undo(
                    DirectoryMoveUndo(
                        source,
                        destination,
                        result["sha256"],
                        result["total_bytes"],
                        result["entries"],
                    )
                )
                return {
                    "path": destination,
                    "source": source,
                    "kind": "directory",
                    "undo_id": undo_id,
                    **result,
                }
            data = self._current(source, base_sha256)
            if data is None:
                raise FileNotFoundError(source)
            self._current(destination, None)
            result = self.write_bytes(destination, data, None)
            if move:
                try:
                    deletion = self.delete_file(source, base_sha256)
                except Exception:
                    self.undo_change(result["undo_id"])
                    raise
                result["source_undo_id"] = deletion["undo_id"]
            return result

    def _move_directory(self, source: str, destination: str, base_sha256: str) -> dict:
        from .directory_tools import inspect_tree, rename_no_replace

        origin, target = self.mutation_path(source), self.mutation_path(destination)
        # path() resolves and verifies both absolute targets remain inside this workspace.
        if target == origin or target.is_relative_to(origin):
            raise ValueError("Cannot move a directory into itself or its descendants")
        if target.exists():
            raise FileExistsError("Directory destination must be absent")
        if not target.parent.is_dir():
            raise ValueError("Parent directory is missing; create it first")
        if origin.stat().st_dev != target.parent.stat().st_dev:
            raise ValueError("Atomic directory moves require the same filesystem")
        result = inspect_tree(self, source, destination)
        if result["sha256"] != base_sha256:
            raise ValueError("SHA-256 conflict: directory tree changed; inspect again")
        self.mutation_path(source)
        self.mutation_path(destination)
        rename_no_replace(origin, target)
        return result

    def cleanup_searches(self) -> None:
        now = time.monotonic()
        for key, search in list(self.searches.items()):
            if now - search["touched"] > self.policy.search_ttl:
                search["iterator"].close()
                del self.searches[key]

    def start_search(
        self,
        pattern: str,
        path: str = ".",
        search_type: str = "content",
        file_pattern: str = "*",
        case_sensitive: bool = False,
        pattern_mode: str = "literal",
        context_lines: int = 0,
        max_results: int | None = None,
    ) -> dict:
        if (
            not pattern
            or len(pattern) > 1000
            or search_type not in {"content", "files"}
            or pattern_mode not in {"literal", "regex", "glob"}
            or (pattern_mode == "glob" and search_type != "files")
            or not file_pattern
            or len(file_pattern) > 1000
            or not 0 <= context_lines <= 10
            or (max_results is not None and not 1 <= max_results <= 100_000)
        ):
            raise ValueError(
                "Use a 1..1000 character pattern/filter, content/files type, literal/regex "
                "(glob for files only), 0..10 context lines and optional max_results 1..100000"
            )
        compiled = None
        if pattern_mode == "regex":
            import regex

            try:
                compiled = regex.compile(
                    pattern,
                    regex.VERSION1 | (0 if case_sensitive else regex.IGNORECASE),
                )
            except (regex.error, RecursionError) as exc:
                raise ValueError(f"Invalid regular expression: {exc}") from exc
        self.path(path, root_ok=True)
        self.cleanup_searches()
        if len(self.searches) >= self.policy.max_searches:
            raise ValueError("Search session limit reached; stop an existing search")
        key = secrets.token_urlsafe(12)
        self.searches[key] = {
            "iterator": self.walk(path),
            "pattern": pattern,
            "type": search_type,
            "file_pattern": file_pattern,
            "case_sensitive": case_sensitive,
            "mode": pattern_mode,
            "compiled": compiled,
            "context": context_lines,
            "max_results": max_results,
            "matched": 0,
            "current": None,
            "scanned": 0,
            "skipped": 0,
            "touched": time.monotonic(),
            "pending": deque(),
            "history": SearchHistory(),
            "status": "running",
        }
        return {"search_id": key, **self.more_search(key)}

    @staticmethod
    def _search_match(search: dict, text: str) -> bool:
        if search["compiled"] is not None:
            # Engine-level timeout applies inside backtracking, not after it finishes.
            return search["compiled"].search(text, timeout=0.05) is not None
        compare = (lambda value: value) if search["case_sensitive"] else str.casefold
        if search["mode"] == "glob":
            return fnmatch.fnmatchcase(compare(text), compare(search["pattern"]))
        return compare(search["pattern"]) in compare(text)

    def more_search(
        self, search_id: str, limit: int = 50, offset: int | None = None
    ) -> dict:
        if (
            type(limit) is not int
            or not 1 <= limit <= 100
            or (offset is not None and type(offset) is not int)
        ):
            raise ValueError(
                "Search page limit is 1..100; offset is an optional integer (negative for tail)"
            )
        self.cleanup_searches()
        search = self.searches[search_id]
        search["touched"] = time.monotonic()
        if offset is not None:
            return self._search_response(
                search,
                search["history"].page(offset, limit, self.policy.max_output_chars),
            )
        start_offset = search["history"].end
        results = []
        budget = self.policy.max_output_chars
        deadline = time.monotonic() + 2
        page_scans = 0
        while len(results) < limit and time.monotonic() < deadline:
            if search["pending"]:
                item = search["pending"].popleft()
                cost = len(json.dumps(item, ensure_ascii=False))
                if cost > budget:
                    search["pending"].appendleft(item)
                    if not results:
                        raise ValueError(
                            "Search result exceeds output budget; restart with fewer context lines"
                        )
                    break
                budget -= cost
                results.append(item)
                continue
            if search["status"] != "running":
                break
            current = search["current"]
            if current is not None:
                index = current["index"]
                lines = current["lines"]
                if index >= len(lines):
                    search["current"] = None
                    continue
                current["index"] += 1
                try:
                    matched = self._search_match(search, lines[index])
                except TimeoutError:
                    search["skipped"] += 1
                    search["status"] = "regex_timeout"
                    search["iterator"].close()
                    search["current"] = None
                    break
                if matched:
                    context = search["context"]
                    start, end = (
                        max(0, index - context),
                        min(len(lines), index + context + 1),
                    )
                    item = {
                        "path": current["path"],
                        "sha256": current["sha256"],
                        "line": index + 1,
                        "text": lines[index][:500],
                        "text_truncated": len(lines[index]) > 500,
                    }
                    if context:
                        item["context"] = [
                            {
                                "line": row + 1,
                                "text": lines[row][:500],
                                "text_truncated": len(lines[row]) > 500,
                            }
                            for row in range(start, end)
                        ]
                    search["pending"].append(item)
                    search["matched"] += 1
                if (
                    search["max_results"] is not None
                    and search["matched"] >= search["max_results"]
                ):
                    search["status"] = "result_limit"
                    search["iterator"].close()
                    search["current"] = None
                continue
            if search["scanned"] >= self.policy.max_scan_files:
                search["iterator"].close()
                search["status"] = "scan_limit"
                break
            if page_scans >= 1000:
                break
            try:
                path = next(search["iterator"])
            except StopIteration:
                search["status"] = "completed"
                break
            search["scanned"] += 1
            page_scans += 1
            if not fnmatch.fnmatchcase(path, search["file_pattern"]):
                continue
            if search["type"] == "files":
                try:
                    matched = self._search_match(search, path)
                except TimeoutError:
                    search["skipped"] += 1
                    search["status"] = "regex_timeout"
                    search["iterator"].close()
                    break
                if matched:
                    search["pending"].append({"path": path})
                    search["matched"] += 1
                    if (
                        search["max_results"] is not None
                        and search["matched"] >= search["max_results"]
                    ):
                        search["status"] = "result_limit"
                        search["iterator"].close()
                continue
            try:
                if self.path(path).stat().st_size > 1024 * 1024:
                    search["skipped"] += 1
                    continue
                data = self.bytes(path)
                if len(data) > 1024 * 1024:
                    search["skipped"] += 1
                    continue
                _, text = _decode_text(data)
            except (OSError, ValueError, UnicodeError):
                search["skipped"] += 1
                continue
            search["current"] = {
                "path": path,
                "lines": text.splitlines(),
                "index": 0,
                "sha256": digest(data),
            }
        search["history"].append(results)
        return self._search_response(
            search,
            {
                "results": results,
                "offset": start_offset,
                "next_offset": start_offset + len(results),
                "delivery": "discovery",
                "requested_results_dropped": False,
            },
        )

    @staticmethod
    def _search_response(search: dict, window: dict) -> dict:
        more = bool(search["pending"]) or search["status"] == "running"
        return {
            "status": search["status"],
            "has_more": more,
            "search_has_more": more,
            "scanned": search["scanned"],
            "matched": search["matched"],
            "pattern_mode": search["mode"],
            "skipped_files_or_match_overflows": search["skipped"],
            **search["history"].metadata(),
            **window,
        }

    def stop_search(self, search_id: str) -> dict:
        search = self.searches.pop(search_id)
        search["iterator"].close()
        return {"stopped": True}

    def read_workbook(
        self,
        path: str,
        sheet: str | int,
        cell_range: str = "A1:J50",
        expected_sha256: str | None = None,
    ) -> dict:
        from .workbook_tools import read_workbook

        return read_workbook(self, path, sheet, cell_range, expected_sha256)

    def write_workbook(
        self,
        path: str,
        sheet: str,
        start_cell: str,
        rows: list[list[Any]],
        base_sha256: str | None,
    ) -> dict:
        from .workbook_tools import write_workbook

        return write_workbook(self, path, sheet, start_cell, rows, base_sha256)

    def write_document(
        self,
        path: str,
        paragraphs: list[str],
        base_sha256: str | None,
        source_format: Literal["plain", "markdown"] = "plain",
    ) -> dict:
        self.writable()
        if (
            not paragraphs
            or len(paragraphs) > 2000
            or sum(len(p) for p in paragraphs) > self.policy.max_output_chars
        ):
            raise ValueError(
                "Use 1..2000 paragraphs within the output character budget"
            )
        output = io.BytesIO()
        suffix = Path(path).suffix.lower()
        if source_format not in {"plain", "markdown"}:
            raise ValueError("Use plain or markdown source_format")
        if source_format == "markdown":
            if suffix != ".docx":
                raise ValueError(
                    "Markdown document creation requires .docx; use write_pdf for rich PDF"
                )
            from .word_authoring import markdown_docx

            content = "\n\n".join(paragraphs)
            if len(content) > self.policy.max_output_chars:
                raise ValueError("Joined Markdown exceeds the character budget")
            with self.lock:
                self._current(path, base_sha256)
                data = markdown_docx(content)
                return self.write_bytes(path, data, base_sha256)
        if suffix == ".docx":
            from docx import Document

            doc = Document()
            for paragraph in paragraphs:
                doc.add_paragraph(paragraph)
            doc.save(output)
        elif suffix == ".pdf":
            from xml.sax.saxutils import escape

            from reportlab.lib.styles import getSampleStyleSheet
            from reportlab.pdfbase import pdfmetrics
            from reportlab.pdfbase.ttfonts import TTFont
            from reportlab.platypus import Paragraph, SimpleDocTemplate

            fonts = [
                Path(os.getenv("WINDIR", "C:/Windows")) / "Fonts/arial.ttf",
                Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"),
            ]
            style = getSampleStyleSheet()["BodyText"]
            font = next((font for font in fonts if font.is_file()), None)
            if font:
                pdfmetrics.registerFont(TTFont("FolderLensUnicode", str(font)))
                style.fontName = "FolderLensUnicode"
            elif any(ord(char) > 127 for paragraph in paragraphs for char in paragraph):
                raise ValueError(
                    "Unicode PDF needs system Arial or DejaVu Sans installed"
                )
            SimpleDocTemplate(output).build(
                [Paragraph(escape(p).replace("\n", "<br/>"), style) for p in paragraphs]
            )
        else:
            raise ValueError("Document creation supports .docx and .pdf")
        return self.write_bytes(path, output.getvalue(), base_sha256)
