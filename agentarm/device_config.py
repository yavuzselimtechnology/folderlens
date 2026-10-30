"""Dependency-free owner device profiles; no credentials or network access here."""

from __future__ import annotations

import hashlib
import ipaddress
import json
import re
import stat
from dataclasses import asdict, dataclass
from pathlib import Path, PurePosixPath, PureWindowsPath
from urllib.parse import urlsplit

from .http_security import canonical_origin

MAX_CONFIG_BYTES = 64 * 1024
MAX_DEVICES = 16
ROOT_PREVIEW_CHARS = 1024


@dataclass(frozen=True)
class DeviceProfile:
    id: str
    url: str
    root: str
    token_env: str
    allow_write: bool = False
    allow_exec: bool = False


@dataclass(frozen=True)
class DeviceConfig:
    source: Path | None = None
    profiles: tuple[DeviceProfile, ...] = ()
    binding: str = ""


def endpoint(value: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) > 8192
        or any(ord(char) <= 32 or char == "\\" for char in value)
    ):
        raise ValueError(
            "Device endpoint must be an HTTPS MCP URL or literal loopback HTTP URL"
        )
    parsed = urlsplit(value)
    if (
        parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
        or parsed.path != "/mcp"
    ):
        raise ValueError(
            "Device URL must end in /mcp without credentials, query or fragment"
        )
    if parsed.scheme == "https":
        return canonical_origin("https://" + parsed.netloc) + "/mcp"
    if parsed.scheme != "http" or not parsed.hostname:
        raise ValueError("Use HTTPS for device endpoints")
    try:
        address = ipaddress.ip_address(parsed.hostname)
    except ValueError:
        raise ValueError("Plain HTTP requires a literal loopback address") from None
    if not address.is_loopback:
        raise ValueError("Plain HTTP requires a literal loopback address")
    port = parsed.port
    if port is not None and not 1 <= port <= 65535:
        raise ValueError("Device endpoint port must be 1..65535")
    host = f"[{address}]" if address.version == 6 else str(address)
    return "http://" + host + (f":{port}" if port not in {None, 80} else "") + "/mcp"


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Device configuration has duplicate JSON keys")
        result[key] = value
    return result


def load_devices(path: Path | None, local_id: str) -> DeviceConfig:
    if path is None:
        return DeviceConfig()
    path = path.expanduser().absolute()
    for item in [path, *path.parents]:
        try:
            info = item.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
            raise ValueError(
                "Device configuration cannot traverse links/reparse points"
            )
    if not stat.S_ISREG(path.stat().st_mode):
        raise ValueError("Device configuration must be a regular JSON file")
    with path.open("rb") as source:
        data = source.read(MAX_CONFIG_BYTES + 1)
    if len(data) > MAX_CONFIG_BYTES:
        raise ValueError("Device configuration exceeds 64 KiB")
    document = json.loads(data.decode("utf-8-sig"), object_pairs_hook=unique_object)
    if (
        not isinstance(document, dict)
        or set(document) != {"version", "devices"}
        or type(document["version"]) is not int
        or document["version"] != 1
    ):
        raise ValueError("Device configuration needs version=1 and devices")
    rows = document["devices"]
    if not isinstance(rows, list) or not 1 <= len(rows) <= MAX_DEVICES:
        raise ValueError("Configure 1..16 devices")
    profiles, identities = [], {local_id}
    required = {"id", "url", "root", "token_env"}
    allowed = required | {"allow_write", "allow_exec"}
    for row in rows:
        if (
            not isinstance(row, dict)
            or not required.issubset(row)
            or not set(row).issubset(allowed)
        ):
            raise ValueError(
                "Each device needs id/url/root/token_env; raw credentials and unknown fields are rejected"
            )
        identity = row["id"]
        if (
            not isinstance(identity, str)
            or not re.fullmatch(r"[a-zA-Z][a-zA-Z0-9_-]{0,63}", identity)
            or identity in identities
        ):
            raise ValueError(
                "Device IDs must be unique, differ from this server and use 1..64 ASCII letters/digits/_/-"
            )
        root = row["root"]
        if (
            not isinstance(root, str)
            or not root
            or len(root) > 32768
            or any(ord(char) < 32 for char in root)
        ):
            raise ValueError(
                "Device root must exactly match the remote get_config.root"
            )
        if not (
            PureWindowsPath(root).is_absolute() or PurePosixPath(root).is_absolute()
        ):
            raise ValueError("Device root must be the remote absolute workspace path")
        token_env = row["token_env"]
        if (
            not isinstance(token_env, str)
            or len(token_env) > 128
            or not re.fullmatch(r"FOLDERLENS_PEER_[A-Z0-9_]+_TOKEN", token_env)
        ):
            raise ValueError(
                "token_env must use the FOLDERLENS_PEER_<NAME>_TOKEN namespace"
            )
        for key in ("allow_write", "allow_exec"):
            if key in row and type(row[key]) is not bool:
                raise ValueError("Device permission ceilings must be JSON booleans")
        identities.add(identity)
        profiles.append(
            DeviceProfile(
                identity,
                endpoint(row["url"]),
                root,
                token_env,
                row.get("allow_write", False),
                row.get("allow_exec", False),
            )
        )
    canonical = json.dumps(
        [
            asdict(profile)
            for profile in sorted(profiles, key=lambda profile: profile.id)
        ],
        sort_keys=True,
    )
    return DeviceConfig(
        path.resolve(), tuple(profiles), hashlib.sha256(canonical.encode()).hexdigest()
    )
