"""Pinned, project-private Java runtime for the fixed Apache POI XLS helper.

Setup is an owner CLI operation. Tool calls only use an already verified runtime;
they do not fetch dependencies or compile user-provided code.
"""

from __future__ import annotations

import contextlib
import ctypes
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import urllib.request
import zipfile
from pathlib import Path, PurePosixPath

SOURCE = Path(__file__).with_name("java") / "NativeXls.java"
LOCK = Path(__file__).with_name("legacy_excel.lock.json")
ROOT = Path(__file__).resolve().parent.parent / ".folderlens" / "xls-runtime"


def _windows_x64() -> bool:
    if os.name != "nt":
        return False
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.GetCurrentProcess.restype = ctypes.c_void_p
    kernel.IsWow64Process2.argtypes = [
        ctypes.c_void_p,
        ctypes.POINTER(ctypes.c_ushort),
        ctypes.POINTER(ctypes.c_ushort),
    ]
    process, native = ctypes.c_ushort(), ctypes.c_ushort()
    if not kernel.IsWow64Process2(
        kernel.GetCurrentProcess(), ctypes.byref(process), ctypes.byref(native)
    ):
        raise OSError(
            ctypes.get_last_error(), "Cannot determine native Windows architecture"
        )
    return native.value == 0x8664  # IMAGE_FILE_MACHINE_AMD64 (Windows PE protocol)


def _check_path(path: Path) -> None:
    for part in [path, *path.parents]:
        if not part.exists():
            continue
        info = part.lstat()
        if part.is_symlink() or getattr(info, "st_file_attributes", 0) & 0x400:
            raise ValueError("XLS runtime cannot traverse symlink/reparse points")


def _hash(path: Path, algorithm: str = "sha512") -> str:
    _check_path(path)
    value = hashlib.new(algorithm)
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            value.update(chunk)
    return value.hexdigest()


def _lock() -> dict:
    data = json.loads(LOCK.read_text(encoding="utf-8"))
    if data["schema"] != 1:
        raise ValueError("Unsupported XLS runtime recipe")
    return data


def _key() -> str:
    return hashlib.sha256(SOURCE.read_bytes() + LOCK.read_bytes()).hexdigest()


def _environment() -> dict[str, str]:
    return {
        key: value
        for key, value in os.environ.items()
        if key
        not in {"JAVA_TOOL_OPTIONS", "JDK_JAVA_OPTIONS", "_JAVA_OPTIONS", "CLASSPATH"}
    }


def _run(command: list[str], timeout: float = 30) -> subprocess.CompletedProcess:
    return subprocess.run(
        command,
        capture_output=True,
        check=False,
        timeout=timeout,
        env=_environment(),
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
    )


def _java_version(path: str) -> int:
    try:
        result = _run([path, "-version"], timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        return 0
    match = re.search(
        r'version "(\d+)(?:\.(\d+))?',
        (result.stdout + result.stderr).decode("utf-8", "replace"),
    )
    return (
        int(match[2] if match[1] == "1" else match[1])
        if match and result.returncode == 0
        else 0
    )


def _java() -> str | None:
    private = ROOT / "java/bin" / ("java.exe" if os.name == "nt" else "java")
    for candidate in [str(private), os.getenv("FOLDERLENS_JAVA"), shutil.which("java")]:
        if candidate and _java_version(candidate) >= 17:
            return candidate
    return None


def _download(item: dict, target: Path, algorithm: str, maximum: int) -> None:
    print(f"Preparing XLS dependency: {target.name}", file=sys.stderr, flush=True)
    value = hashlib.new(algorithm)
    with (
        urllib.request.urlopen(item["url"], timeout=30) as response,
        target.open("xb") as output,
    ):
        size = 0
        while chunk := response.read(1024 * 1024):
            size += len(chunk)
            if size > maximum:
                raise ValueError("XLS dependency exceeds its download budget")
            value.update(chunk)
            output.write(chunk)
    if size != item["size"] or value.hexdigest() != item[algorithm]:
        raise ValueError("XLS dependency integrity mismatch")


def _extract_java(archive: Path, destination: Path) -> None:
    with zipfile.ZipFile(archive) as source:
        entries = source.infolist()
        if len(entries) > 5000:
            raise ValueError("Java archive has too many entries")
        roots = {PurePosixPath(entry.filename).parts[0] for entry in entries}
        if len(roots) != 1:
            raise ValueError("Java archive needs one distribution root")
        expanded = 0
        for entry in entries:
            path = PurePosixPath(entry.filename)
            if (
                path.is_absolute()
                or "\\" in entry.filename
                or ":" in entry.filename
                or ".." in path.parts
            ):
                raise ValueError("Unsafe Java archive path")
            if stat.S_ISLNK(entry.external_attr >> 16):
                raise ValueError("Java archive links are not supported")
            expanded += entry.file_size
            if expanded > 512 * 1024 * 1024 or entry.file_size > 128 * 1024 * 1024:
                raise ValueError("Java archive exceeds its expanded byte budget")
            if len(path.parts) == 1:
                continue
            target = destination.joinpath(*path.parts[1:])
            if not target.resolve().is_relative_to(destination.resolve()):
                raise ValueError("Java archive leaves its private target")
            if entry.is_dir():
                target.mkdir(parents=True, exist_ok=True)
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                with source.open(entry) as content, target.open("xb") as output:
                    shutil.copyfileobj(content, output, length=1024 * 1024)


@contextlib.contextmanager
def _setup_lock():
    with (ROOT / ".setup.lock").open("a+b") as handle:
        handle.seek(0)
        if not handle.read(1):
            handle.write(b"0")
            handle.flush()
        handle.seek(0)
        if os.name == "nt":
            import msvcrt

            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            yield
        finally:
            handle.seek(0)
            if os.name == "nt":
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def ready() -> dict:
    _check_path(ROOT)
    manifest = ROOT / "manifest.json"
    _check_path(manifest)
    if not manifest.is_file() or manifest.stat().st_size > 65536:
        raise ValueError(
            "Run python folderlens.py --setup-xls in the local terminal first"
        )
    state = json.loads(manifest.read_text(encoding="utf-8"))
    if not state.get("classes") or "NativeXls.class" not in state["classes"]:
        raise ValueError("XLS runtime manifest has no entry class")
    if state["key"] != _key():
        raise ValueError("XLS helper source changed; rerun --setup-xls locally")
    recipe = _lock()
    for item in recipe["jars"]:
        if _hash(ROOT / "lib" / item["name"]) != item["sha512"]:
            raise ValueError("XLS library changed; rerun --setup-xls locally")
    for name, expected in state["classes"].items():
        if (
            Path(name).name != name
            or not name.endswith(".class")
            or _hash(ROOT / "classes" / name) != expected
        ):
            raise ValueError("XLS helper bytecode integrity mismatch")
    java = _java()
    if java is None:
        raise ValueError("Java 17+ is unavailable; rerun --setup-xls locally")
    jars = [
        str(ROOT / "lib" / item["name"])
        for item in recipe["jars"]
        if not item["compiler"]
    ]
    return {
        "java": java,
        "classpath": os.pathsep.join([str(ROOT / "classes"), *jars]),
        "key": state["key"],
    }


def install() -> dict:
    _check_path(ROOT)
    ROOT.mkdir(parents=True, exist_ok=True)
    recipe = _lock()
    with (
        _setup_lock(),
        tempfile.TemporaryDirectory(prefix="setup-", dir=ROOT) as directory,
    ):
        stage = Path(directory).resolve()
        if not stage.is_relative_to(ROOT.resolve()):
            raise ValueError("XLS setup staging leaves the managed runtime")
        (ROOT / "lib").mkdir(exist_ok=True)
        _check_path(ROOT / "lib")
        for item in recipe["jars"]:
            target = ROOT / "lib" / item["name"]
            if target.is_file() and _hash(target) == item["sha512"]:
                continue
            download = stage / item["name"]
            _download(item, download, "sha512", 16 * 1024 * 1024)
            os.replace(download, target)
        java = _java()
        if java is None:
            if not _windows_x64():
                raise ValueError(
                    "Install Java 17+ or set FOLDERLENS_JAVA; private Java provisioning supports Windows x64"
                )
            archive = stage / "java.zip"
            _download(recipe["java"], archive, "sha256", 64 * 1024 * 1024)
            candidate = stage / "java"
            candidate.mkdir()
            _extract_java(archive, candidate)
            if _java_version(str(candidate / "bin/java.exe")) < 17:
                raise ValueError("Downloaded Java runtime failed its version check")
            target = ROOT / "java"
            if target.exists():
                _check_path(target)
                if not target.resolve().is_relative_to(ROOT.resolve()):
                    raise ValueError("Java cleanup leaves the managed runtime")
                shutil.rmtree(target)
            os.replace(candidate, target)
            java = str(target / "bin/java.exe")
        classes = stage / "classes"
        classes.mkdir()
        compiler = next(item for item in recipe["jars"] if item["compiler"])
        jars = os.pathsep.join(
            str(ROOT / "lib" / item["name"])
            for item in recipe["jars"]
            if not item["compiler"]
        )
        result = _run(
            [
                java,
                "-Xmx128m",
                "-jar",
                str(ROOT / "lib" / compiler["name"]),
                "-17",
                "-encoding",
                "UTF-8",
                "-classpath",
                jars,
                "-d",
                str(classes),
                str(SOURCE),
            ],
            timeout=60,
        )
        if result.returncode:
            raise ValueError(
                "XLS helper compilation failed: "
                + result.stderr.decode("utf-8", "replace")[:1000]
            )
        hashes = {path.name: _hash(path) for path in classes.glob("*.class")}
        if "NativeXls.class" not in hashes:
            raise ValueError("XLS helper compiler did not emit its entry class")
        target = ROOT / "classes"
        if target.exists():
            _check_path(target)
            if not target.resolve().is_relative_to(ROOT.resolve()):
                raise ValueError("Bytecode cleanup leaves the managed runtime")
            shutil.rmtree(target)
        os.replace(classes, target)
        manifest = stage / "manifest.json"
        manifest.write_text(
            json.dumps({"key": _key(), "classes": hashes}), encoding="utf-8"
        )
        os.replace(manifest, ROOT / "manifest.json")
    return ready()
