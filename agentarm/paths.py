from __future__ import annotations

import re
from pathlib import PurePosixPath

_WINDOWS_RESERVED = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}
_SHARE_ID = re.compile(r"^[A-Za-z0-9_-]{12,32}$")
_PRIVATE_NAMES = {
    ".env",
    ".netrc",
    ".npmrc",
    ".pypirc",
    "credentials.json",
    "id_dsa",
    "id_ecdsa",
    "id_rsa",
    "id_ed25519",
    "secret.json",
    "secrets.json",
    "service-account.json",
    "service_account.json",
}
_IGNORED_DIRECTORIES = {
    ".agents",
    ".cache",
    ".codex",
    ".eggs",
    ".git",
    ".ipynb_checkpoints",
    ".mypy_cache",
    ".next",
    ".nox",
    ".parcel-cache",
    ".pytest_cache",
    ".ruff_cache",
    ".tmp",
    ".tox",
    ".turbo",
    ".venv",
    "__pycache__",
    "build",
    "coverage",
    "dist",
    "htmlcov",
    "logs",
    "node_modules",
    "target",
    "temp",
    "tmp",
    "var",
}


def safe_relative_path(value: str) -> str:
    if not value or "\x00" in value or "\\" in value or ":" in value:
        raise ValueError("Invalid relative path")
    path = PurePosixPath(value)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in value.split("/")):
        raise ValueError("Invalid relative path")
    if len(value.encode("utf-8")) > 1024 or len(path.parts) > 48:
        raise ValueError("Path is too long")
    for part in path.parts:
        stem = part.rstrip(" .").split(".", 1)[0].upper()
        if part != part.rstrip(" .") or stem in _WINDOWS_RESERVED or "~" in part:
            raise ValueError("Unsafe path component")
        if any(ord(char) < 32 for char in part):
            raise ValueError("Unsafe path component")
    return path.as_posix()


def is_sensitive_path(path: str) -> bool:
    parts = PurePosixPath(path).parts
    for part in parts:
        name = part.casefold()
        if name == ".git":
            return True
        if name in _PRIVATE_NAMES or name.endswith((".pem", ".key", ".p12", ".pfx")):
            return True
        if name.endswith(".env"):
            return True
        if name.startswith(".env.") and name not in {".env.example", ".env.sample"}:
            return True
    return False


def is_ignored_path(path: str) -> bool:
    return any(
        part.casefold() in _IGNORED_DIRECTORIES or part.casefold().endswith(".egg-info")
        for part in PurePosixPath(path).parts[:-1]
    )


def validate_share_id(value: str) -> str:
    if not _SHARE_ID.fullmatch(value):
        raise ValueError("Invalid share identifier")
    return value
