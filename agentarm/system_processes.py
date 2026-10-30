"""System process observations and identity-checked termination under explicit exec authority."""

from __future__ import annotations

import ctypes
import json
import os
import selectors
import signal
import sys
import time
from collections import OrderedDict
from ctypes import wintypes
from functools import lru_cache
from pathlib import Path

import psutil

CPU_SAMPLE_LIMIT = 4096
CPU_SAMPLE_TTL = 60
FILETIME_SECOND = 10_000_000
FILETIME_UNIX_EPOCH = 116_444_736_000_000_000
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
PROCESS_TERMINATE = 0x0001
SYNCHRONIZE = 0x00100000
WAIT_OBJECT_0 = 0
WAIT_TIMEOUT = 258


class MemoryCounters(ctypes.Structure):
    _fields_ = [
        ("cb", wintypes.DWORD),
        ("PageFaultCount", wintypes.DWORD),
        *[
            (name, ctypes.c_size_t)
            for name in (
                "PeakWorkingSetSize",
                "WorkingSetSize",
                "QuotaPeakPagedPoolUsage",
                "QuotaPagedPoolUsage",
                "QuotaPeakNonPagedPoolUsage",
                "QuotaNonPagedPoolUsage",
                "PagefileUsage",
                "PeakPagefileUsage",
            )
        ],
    ]


@lru_cache(maxsize=1)
def _kernel32():
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    definitions = {
        "OpenProcess": (
            wintypes.HANDLE,
            [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD],
        ),
        "CloseHandle": (wintypes.BOOL, [wintypes.HANDLE]),
        "GetProcessTimes": (
            wintypes.BOOL,
            [wintypes.HANDLE, *[ctypes.POINTER(wintypes.FILETIME)] * 4],
        ),
        "QueryFullProcessImageNameW": (
            wintypes.BOOL,
            [
                wintypes.HANDLE,
                wintypes.DWORD,
                wintypes.LPWSTR,
                ctypes.POINTER(wintypes.DWORD),
            ],
        ),
        "K32GetProcessMemoryInfo": (
            wintypes.BOOL,
            [wintypes.HANDLE, ctypes.POINTER(MemoryCounters), wintypes.DWORD],
        ),
        "IsProcessCritical": (
            wintypes.BOOL,
            [wintypes.HANDLE, ctypes.POINTER(wintypes.BOOL)],
        ),
        "WaitForSingleObject": (wintypes.DWORD, [wintypes.HANDLE, wintypes.DWORD]),
        "TerminateProcess": (wintypes.BOOL, [wintypes.HANDLE, wintypes.UINT]),
    }
    for name, (result, arguments) in definitions.items():
        function = getattr(kernel, name)
        function.restype, function.argtypes = result, arguments
    return kernel


def _filetime(value) -> int:
    return (value.dwHighDateTime << 32) | value.dwLowDateTime


class WindowsProcess:
    """All identity reads and termination use the same kernel process object."""

    def __init__(self, pid: int, terminate: bool = False):
        self.pid = pid
        self.kernel = _kernel32()
        rights = PROCESS_QUERY_LIMITED_INFORMATION | SYNCHRONIZE
        if terminate:
            rights |= PROCESS_TERMINATE
        self.handle = self.kernel.OpenProcess(rights, False, pid)
        if not self.handle:
            code = ctypes.get_last_error()
            if (
                code == 87
            ):  # ERROR_INVALID_PARAMETER: PID disappeared (PID zero is never a target).
                raise FileNotFoundError("Process no longer exists")
            raise ctypes.WinError(code)

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.kernel.CloseHandle(self.handle)

    def times(self) -> tuple[int, int, int]:
        created, exited, kernel, user = (wintypes.FILETIME() for _ in range(4))
        if not self.kernel.GetProcessTimes(
            self.handle,
            ctypes.byref(created),
            ctypes.byref(exited),
            ctypes.byref(kernel),
            ctypes.byref(user),
        ):
            raise ctypes.WinError(ctypes.get_last_error())
        return _filetime(created), _filetime(kernel), _filetime(user)

    def key(self, created: int) -> str:
        return f"windows:{self.pid}:{created:016x}"

    def exited(self, wait_ms: int = 0) -> bool:
        state = self.kernel.WaitForSingleObject(self.handle, wait_ms)
        if state not in {WAIT_OBJECT_0, WAIT_TIMEOUT}:
            raise ctypes.WinError(ctypes.get_last_error())
        return state == WAIT_OBJECT_0

    def critical(self) -> bool:
        value = wintypes.BOOL()
        if not self.kernel.IsProcessCritical(self.handle, ctypes.byref(value)):
            raise ctypes.WinError(ctypes.get_last_error())
        return bool(value.value)

    def snapshot(self) -> dict:
        created, kernel, user = self.times()
        name_buffer = ctypes.create_unicode_buffer(
            32768
        )  # Win32 maximum extended path length.
        size = wintypes.DWORD(len(name_buffer))
        name = None
        if self.kernel.QueryFullProcessImageNameW(
            self.handle, 0, name_buffer, ctypes.byref(size)
        ):
            name = Path(name_buffer.value).name
        memory = MemoryCounters()
        memory.cb = ctypes.sizeof(memory)
        rss = (
            memory.WorkingSetSize
            if self.kernel.K32GetProcessMemoryInfo(
                self.handle, ctypes.byref(memory), memory.cb
            )
            else None
        )
        return {
            "pid": self.pid,
            "name": name,
            "status": "completed" if self.exited() else "running",
            "rss_bytes": rss,
            "created_at": (created - FILETIME_UNIX_EPOCH) / FILETIME_SECOND,
            "process_key": self.key(created),
            "cpu_user_seconds": user / FILETIME_SECOND,
            "cpu_system_seconds": kernel / FILETIME_SECOND,
            "cpu_seconds": (user + kernel) / FILETIME_SECOND,
        }

    def kill(self) -> None:
        if self.critical():
            raise PermissionError(
                "Critical Windows process cannot be terminated through this tool"
            )
        if not self.kernel.TerminateProcess(self.handle, 1):
            code = ctypes.get_last_error()
            if not self.exited():
                raise ctypes.WinError(code)


def _snapshot(pid: int) -> dict:
    if os.name == "nt":
        with WindowsProcess(pid) as process:
            return process.snapshot()
    process = psutil.Process(pid)
    created = process.create_time()
    with process.oneshot():
        cpu = process.cpu_times()
        result = {
            "pid": pid,
            "name": process.name(),
            "status": process.status(),
            "rss_bytes": process.memory_info().rss,
            "created_at": created,
            "process_key": f"posix:{pid}:{created.hex()}",
            "cpu_user_seconds": cpu.user,
            "cpu_system_seconds": cpu.system,
            "cpu_seconds": cpu.user + cpu.system,
        }
    if psutil.Process(pid).create_time() != created:
        raise ValueError("Process identity changed during observation")
    return result


class SystemProcesses:
    def __init__(self, workspace, owned):
        self.workspace, self.owned = workspace, owned
        self.samples = OrderedDict()

    def inspect(self, pid: int) -> dict:
        result = _snapshot(pid)
        now = time.perf_counter()
        # Updated samples move to the end, so expiration only examines the oldest prefix.
        while self.samples:
            oldest, (observed, _) = next(iter(self.samples.items()))
            if now - observed <= CPU_SAMPLE_TTL:
                break
            del self.samples[oldest]
        key = result["process_key"]
        previous = self.samples.get(key)
        percent = interval = None
        if (
            previous is not None
            and now > previous[0]
            and result["cpu_seconds"] >= previous[1]
        ):
            interval = now - previous[0]
            percent = 100 * (result["cpu_seconds"] - previous[1]) / interval
        self.samples[key] = (now, result["cpu_seconds"])
        self.samples.move_to_end(key)
        while len(self.samples) > CPU_SAMPLE_LIMIT:
            self.samples.popitem(last=False)
        return {**result, "cpu_percent": percent, "cpu_interval_seconds": interval}

    def list(self, offset: int = 0, limit: int = 100, pid: int | None = None) -> dict:
        if (
            type(offset) is not int
            or offset < 0
            or type(limit) is not int
            or not 1 <= limit <= 500
            or (pid is not None and (type(pid) is not int or pid <= 0))
        ):
            raise ValueError("Use offset >= 0, limit 1..500 and optional positive PID")
        pids = [pid] if pid is not None else sorted(psutil.pids())
        result = {
            "processes": [],
            "next_offset": None,
            "cpu_percent_basis": "100% is one logical CPU; multithreaded use may exceed 100%",
            "sampling": "between observations; first/expired/changed-identity samples are null",
        }
        if (
            len(json.dumps(result, ensure_ascii=False))
            > self.workspace.policy.max_output_chars
        ):
            raise ValueError("Process metadata exceeds response budget")
        for index in range(offset, min(len(pids), offset + limit + 1)):
            if len(result["processes"]) == limit:
                result["next_offset"] = index
                break
            try:
                entry = self.inspect(pids[index])
            except (OSError, ValueError, psutil.Error) as exc:
                entry = {
                    "pid": pids[index],
                    "name": None,
                    "status": "unavailable",
                    "rss_bytes": None,
                    "process_key": None,
                    "cpu_percent": None,
                    "access_error": type(exc).__name__,
                }
            result["processes"].append(entry)
            result["next_offset"] = index + 1 if index + 1 < len(pids) else None
            if (
                len(json.dumps(result, ensure_ascii=False))
                > self.workspace.policy.max_output_chars
            ):
                result["processes"].pop()
                if not result["processes"]:
                    raise ValueError("Process metadata exceeds response budget")
                result["next_offset"] = index
                break
        return result

    def kill(self, pid: int, process_key: str, wait_ms: int = 3000) -> dict:
        if not self.workspace.policy.allow_exec:
            raise PermissionError(
                "Restart locally with --allow-exec to terminate system processes"
            )
        if (
            type(pid) is not int
            or pid <= 0
            or not isinstance(process_key, str)
            or not 1 <= len(process_key) <= 128
            or type(wait_ms) is not int
            or not 0 <= wait_ms <= 5000
        ):
            raise ValueError(
                "Use a positive PID, current process_key and wait_ms 0..5000"
            )
        try:
            protected = {
                os.getpid(),
                1,
                *(parent.pid for parent in psutil.Process().parents()),
            }
        except psutil.Error:
            raise PermissionError(
                "Cannot verify the protected process ancestor chain"
            ) from None
        if pid in protected:
            raise PermissionError(
                "FolderLens, its ancestors and the system init process are protected"
            )
        owned_key = next(
            (
                key
                for key, session in self.owned.sessions.items()
                if session.process.pid == pid and session.process.poll() is None
            ),
            None,
        )
        scope = "owned_session_tree" if owned_key else "single_process"
        if os.name == "nt":
            with WindowsProcess(pid, terminate=True) as process:
                if process.key(process.times()[0]) != process_key:
                    raise ValueError(
                        "Process identity conflict; inspect the current PID before retrying"
                    )
                already = process.exited()
                if not already:
                    if process.critical():
                        raise PermissionError(
                            "Critical Windows process cannot be terminated through this tool"
                        )
                    if owned_key:
                        self.owned.stop(owned_key)
                    else:
                        process.kill()
                done = process.exited(wait_ms)
        elif (
            sys.platform == "linux"
            and hasattr(os, "pidfd_open")
            and hasattr(signal, "pidfd_send_signal")
        ):
            descriptor = os.pidfd_open(pid)
            try:
                try:
                    current = _snapshot(pid)
                except psutil.NoSuchProcess:
                    raise FileNotFoundError(
                        "Process exited before identity could be verified"
                    ) from None
                except psutil.AccessDenied:
                    raise PermissionError(
                        "Process identity cannot be verified with current OS permissions"
                    ) from None
                if current["process_key"] != process_key:
                    raise ValueError(
                        "Process identity conflict; inspect the current PID before retrying"
                    )
                with selectors.DefaultSelector() as selector:
                    selector.register(descriptor, selectors.EVENT_READ)
                    already = bool(selector.select(0))
                    if not already:
                        if owned_key:
                            self.owned.stop(owned_key)
                        else:
                            signal.pidfd_send_signal(descriptor, signal.SIGKILL)
                    done = bool(selector.select(wait_ms / 1000))
            finally:
                os.close(descriptor)
        else:
            raise ValueError(
                "Identity-safe PID termination requires Windows handles or Linux pidfd support"
            )
        return {
            "pid": pid,
            "process_key": process_key,
            "termination_scope": scope,
            "termination_requested": not already,
            "terminated": done,
            "already_exited": already,
        }
