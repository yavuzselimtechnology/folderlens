"""Windows ownership barrier: accept a command only after the parent assigns its job.

The launch frame is read with exact unbuffered reads so command stdin remains intact.
No user command, shell, startup profile or third-party module runs before the barrier.
"""

import json
import os
import subprocess
import sys

MAX_LAUNCH_BYTES = 256 * 1024


def read_exact(length: int) -> bytes:
    result = bytearray()
    while len(result) < length:
        data = os.read(sys.stdin.fileno(), length - len(result))
        if not data:
            raise ValueError("Launch frame ended early")
        result.extend(data)
    return bytes(result)


def main() -> int:
    try:
        if os.name == "nt":
            import msvcrt

            msvcrt.setmode(sys.stdin.fileno(), os.O_BINARY)
        size = int.from_bytes(read_exact(4), "big")
        if not 1 <= size <= MAX_LAUNCH_BYTES:
            raise ValueError("Launch frame exceeds its byte budget")
        command = json.loads(read_exact(size))["command"]
        if (
            not isinstance(command, list)
            or not 1 <= len(command) <= 100
            or any(not isinstance(value, str) or "\0" in value for value in command)
            or sum(map(len, command)) > 32768
        ):
            raise ValueError("Invalid launch argv")
        process = subprocess.Popen(
            command,
            stdin=sys.stdin,
            stdout=sys.stdout,
            stderr=sys.stderr,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
        return process.wait()
    except FileNotFoundError:
        print("FolderLens command launch failed (FileNotFoundError)", file=sys.stderr)
        return 127
    except (OSError, ValueError, KeyError, TypeError) as exc:
        print(
            f"FolderLens command launch failed ({type(exc).__name__})", file=sys.stderr
        )
        return 126


if __name__ == "__main__":
    raise SystemExit(main())
