from __future__ import annotations

import os
import subprocess
import sys
import webbrowser
from pathlib import Path

PUBLIC_URL = os.getenv("AGENTARM_PUBLIC_URL", "https://yavuzselimtech.tr/AgentArm/")


def _venv_python(root: Path) -> Path:
    if os.name == "nt":
        return root / ".venv" / "Scripts" / "python.exe"
    return root / ".venv" / "bin" / "python"


def _bootstrap_and_restart(exc: ModuleNotFoundError) -> None:
    """Create FolderLens' isolated environment and restart inside it."""
    root = Path(__file__).resolve().parent
    venv_python = _venv_python(root)
    print(
        "\nFolderLens dependencies are missing; preparing the isolated "
        f"environment at {venv_python.parent.parent}...\n",
        file=sys.stderr,
    )
    try:
        if not venv_python.exists():
            subprocess.run(
                [sys.executable, "-m", "venv", str(root / ".venv")],
                cwd=root,
                check=True,
            )
        subprocess.run(
            [
                str(venv_python),
                "-m",
                "pip",
                "install",
                "--disable-pip-version-check",
                "-r",
                str(root / "requirements.txt"),
            ],
            cwd=root,
            check=True,
        )
    except (OSError, subprocess.CalledProcessError) as bootstrap_exc:
        print(
            "\nAutomatic setup failed. Check the error above, then run "
            ".\\start.ps1 from the FolderLens directory.\n",
            file=sys.stderr,
        )
        raise SystemExit(2) from bootstrap_exc

    os.execv(
        str(venv_python),
        [str(venv_python), str(Path(__file__).resolve()), *sys.argv[1:]],
    )
    raise SystemExit(2) from exc


def _dependency_error(exc: ModuleNotFoundError) -> None:
    print(
        "\nFolderLens dependencies are not installed in this Python environment.\n"
        "Recommended PowerShell startup:\n\n"
        "  .\\start.ps1\n\n"
        "Manual installation:\n\n"
        "  python -m pip install -r requirements.txt\n"
        "  python run.py\n",
        file=sys.stderr,
    )
    raise SystemExit(2) from exc


def main() -> int:
    if "--agent" in sys.argv:
        from folderlens import main as agent_main

        return agent_main([arg for arg in sys.argv[1:] if arg != "--agent"])
    if "--server" not in sys.argv:
        print("\nFolderLens temporary local workspace bridge")
        print(f"Opening : {PUBLIC_URL}")
        print(
            "Choose a local folder. Keep the FolderLens tab open while an AI reads "
            "or changes the temporary workspace.\n"
        )
        if not webbrowser.open(PUBLIC_URL):
            print(f"Open this address manually: {PUBLIC_URL}", file=sys.stderr)
        return 0

    try:
        import uvicorn

        from agentarm.config import Settings
        from agentarm.web import create_app
    except ModuleNotFoundError as exc:
        _bootstrap_and_restart(exc)

    settings = Settings.from_env()
    host = os.getenv("AGENTARM_HOST", "127.0.0.1")
    port = int(os.getenv("AGENTARM_PORT", "8765"))
    print("\nFolderLens local development server")
    print(f"URL      : http://{host}:{port}/")
    print("Storage  : process memory only")
    print("Press Ctrl+C to stop.\n")
    uvicorn.run(create_app(settings), host=host, port=port, proxy_headers=False)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

try:
    from agentarm.config import Settings
    from agentarm.web import create_app
except ModuleNotFoundError as exc:
    _dependency_error(exc)

app = create_app(Settings.from_env())
