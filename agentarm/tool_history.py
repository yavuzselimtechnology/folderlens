"""Opt-in, private, bounded tool history; never a source of execution authority."""

from __future__ import annotations

import json
import math
import sqlite3
import threading
import time
from datetime import datetime
from pathlib import Path

from .oauth import _private_permissions

MAX_RECORDS = 200
MAX_STORED_BYTES = 8 * 1024 * 1024
MAX_RECORD_BYTES = 32 * 1024
CAPTURE_CHARS = 6000
RETENTION_SECONDS = 30 * 24 * 3600
APPLICATION_ID = int.from_bytes(b"FLTH", "big")
SCHEMA_VERSION = 1
SQLITE_PAGE_BYTES = 4096
MAX_DATABASE_BYTES = 16 * 1024 * 1024


def parse_since(value: str | None) -> float | None:
    """Parse an unambiguous RFC 3339/ISO 8601 instant for history filtering."""
    if value is None:
        return None
    if type(value) is not str or not 1 <= len(value) <= 64:
        raise ValueError("since must be a timezone-aware RFC 3339 timestamp")
    normalized = value[:-1] + "+00:00" if value.endswith(("Z", "z")) else value
    try:
        parsed = datetime.fromisoformat(normalized)
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise ValueError
        timestamp = parsed.timestamp()
    except (OSError, OverflowError, ValueError):
        raise ValueError("since must be a timezone-aware RFC 3339 timestamp") from None
    if not math.isfinite(timestamp):
        raise ValueError("since must be a timezone-aware RFC 3339 timestamp")
    return timestamp


def validate_filters(tool_name: str | None, success: bool | None) -> None:
    if tool_name is not None and (
        type(tool_name) is not str or not 1 <= len(tool_name) <= 128
    ):
        raise ValueError("tool_name must be a non-empty name up to 128 characters")
    if success is not None and type(success) is not bool:
        raise ValueError("success must be a boolean")


def capture(value) -> dict:
    """A JSON text prefix, with native media payloads excluded before encoding."""
    if hasattr(value, "content") and hasattr(value, "is_error"):
        value = {
            "is_error": value.is_error,
            "content": [
                {"type": "text", "text": item.text}
                if item.type == "text"
                else {"type": item.type, "payload_omitted": True}
                for item in value.content
            ],
        }
    chunks = []
    remaining = CAPTURE_CHARS
    truncated = False
    encoder = json.JSONEncoder(
        ensure_ascii=True, default=lambda item: type(item).__name__
    )
    for chunk in encoder.iterencode(value):
        if len(chunk) > remaining:
            chunks.append(chunk[:remaining])
            truncated = True
            break
        chunks.append(chunk)
        remaining -= len(chunk)
    return {"json_prefix": "".join(chunks), "truncated": truncated}


class ToolHistory:
    def __init__(self, directory: Path, mode: str):
        if mode not in {"metadata", "content"}:
            raise ValueError("Persistent history mode must be metadata or content")
        self.mode = mode
        self.directory = directory.expanduser().absolute()
        self.lock = threading.RLock()
        self.last_error = None
        for component in [*self.directory.parents, self.directory]:
            if component.is_symlink() or (
                component.exists()
                and getattr(component.lstat(), "st_file_attributes", 0) & 0x400
            ):
                raise ValueError(
                    "History directory cannot traverse links/reparse points"
                )
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        expected = {"history.sqlite3", "history.sqlite3-journal"}
        if any(
            item.name not in expected
            or not item.is_file()
            or item.is_symlink()
            or getattr(item.lstat(), "st_file_attributes", 0) & 0x400
            for item in self.directory.iterdir()
        ):
            raise ValueError("History needs a dedicated directory")
        _private_permissions(self.directory, directory=True)
        database = self.directory / "history.sqlite3"
        existing = database.exists()
        for item in self.directory.iterdir():
            _private_permissions(item, directory=False)
        self.connection = sqlite3.connect(database, timeout=2, check_same_thread=False)
        try:
            signature = self.connection.execute("PRAGMA application_id").fetchone()[0]
            version = self.connection.execute("PRAGMA user_version").fetchone()[0]
            if existing and (signature != APPLICATION_ID or version != SCHEMA_VERSION):
                raise ValueError("Unsupported history database; choose a new directory")
            # 4096 SQLite pages x 4096 bytes: hard 16 MiB database ceiling.
            # FULL auto-vacuum keeps deleted history from accumulating free pages.
            self.connection.execute(f"PRAGMA page_size={SQLITE_PAGE_BYTES}")
            self.connection.execute("PRAGMA auto_vacuum=FULL")
            self.connection.execute(
                f"PRAGMA max_page_count={MAX_DATABASE_BYTES // SQLITE_PAGE_BYTES}"
            )
            self.connection.execute("PRAGMA journal_mode=DELETE")
            self.connection.execute("PRAGMA secure_delete=ON")
            self.connection.execute(f"PRAGMA application_id={APPLICATION_ID}")
            self.connection.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
            self.connection.execute(
                "CREATE TABLE IF NOT EXISTS calls "
                "(id INTEGER PRIMARY KEY AUTOINCREMENT, time REAL NOT NULL, payload TEXT NOT NULL, "
                "size INTEGER NOT NULL)"
            )
            self.connection.commit()
            _private_permissions(database, directory=False)
            with self.connection:
                self._prune()
        except BaseException:
            self.connection.close()
            raise

    def _prune(self):
        self.connection.execute(
            "DELETE FROM calls WHERE time < ?", (time.time() - RETENTION_SECONDS,)
        )
        rows = self.connection.execute(
            "SELECT id,size FROM calls ORDER BY id DESC"
        ).fetchall()
        total = 0
        for index, (identity, size) in enumerate(rows):
            total += size
            if index >= MAX_RECORDS or total > MAX_STORED_BYTES:
                self.connection.execute("DELETE FROM calls WHERE id<=?", (identity,))
                break

    def append(self, metadata: dict, scopes: list[str], arguments=None, result=None):
        record = {**metadata, "required_scopes": scopes}
        if self.mode == "content":
            record.update(arguments=capture(arguments), result=capture(result))
        payload = json.dumps(record, ensure_ascii=True)
        if len(payload) > MAX_RECORD_BYTES:
            raise ValueError("History record exceeds its storage budget")
        with self.lock, self.connection:
            self.connection.execute(
                "INSERT INTO calls(time,payload,size) VALUES (?,?,?)",
                (record["time"], payload, len(payload)),
            )
            self._prune()

    def recent(
        self,
        limit: int,
        scopes: set[str],
        budget: int,
        before_id: int | None = None,
        since: float | None = None,
        tool_name: str | None = None,
        success: bool | None = None,
    ) -> dict:
        if type(limit) is not int or not 1 <= limit <= MAX_RECORDS:
            raise ValueError("Use limit 1..200")
        if before_id is not None and (type(before_id) is not int or before_id < 1):
            raise ValueError("before_id must be a positive history ID")
        if since is not None and (
            type(since) not in {int, float} or not math.isfinite(since)
        ):
            raise ValueError("since must be a finite Unix timestamp")
        validate_filters(tool_name, success)
        with self.lock, self.connection:
            self._prune()
            rows = self.connection.execute(
                "SELECT id,payload FROM calls "
                "WHERE (? IS NULL OR id < ?) AND (? IS NULL OR time >= ?) "
                "ORDER BY id DESC LIMIT ?",
                (before_id, before_id, since, since, MAX_RECORDS),
            ).fetchall()
        selected = []
        eligible = []
        for identity, payload in rows:
            record = json.loads(payload)
            if not set(record["required_scopes"]).issubset(scopes):
                continue
            if tool_name is not None and record.get("tool") != tool_name:
                continue
            if success is not None and record.get("success") is not success:
                continue
            if self.mode != "content":
                record.pop("arguments", None)
                record.pop("result", None)
            eligible.append({**record, "id": identity})

        def response(records):
            more = len(records) < len(eligible)
            return {
                "calls": records,
                "persistent": True,
                "mode": self.mode,
                "has_more": more,
                "next_before_id": records[0]["id"] if records and more else None,
            }

        # Reserve wrapper metadata; budget is measured on the actual serialized response.
        for record in eligible:
            candidate = [record, *selected]
            if len(json.dumps(response(candidate), ensure_ascii=True)) > budget:
                if not selected:
                    raise ValueError(
                        "One history record exceeds the current output budget"
                    )
                break
            selected = candidate
            if len(selected) == limit:
                break
        result = response(selected)
        if len(json.dumps(result, ensure_ascii=True)) > budget:
            raise ValueError("History response metadata exceeds output budget")
        return result

    def close(self):
        with self.lock:
            self.connection.close()
