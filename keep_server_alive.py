"""Keep the meeting-transcript server alive, independent of the Hermes terminal.

Launched DETACHED from the OS (via PowerShell Start-Process) so it survives the
Hermes desktop app closing or cleaning up its background terminals. Every 30s it
checks port 8765; if the server is down it kills stale instances and restarts
run_server.py (also detached). All server output is appended to server.log.
"""
from __future__ import annotations

import socket
import subprocess
import time
from pathlib import Path

ROOT = Path(r"C:\Users\lan\Documents\開會語音逐字稿")
# Use pythonw.exe (the windowless Windows launcher) so the server runs fully
# backgrounded with NO console/black window popping up.
VENV_PY = ROOT / ".venv" / "Scripts" / "pythonw.exe"
LOG = ROOT / "server.log"
PORT = 8765
CHECK_INTERVAL = 30


def log(msg: str) -> None:
    ts = time.strftime("%Y-%m-%d %H:%M:%S")
    try:
        with LOG.open("a", encoding="utf-8") as f:
            f.write(f"[{ts}] {msg}\n")
    except Exception:
        pass


def port_open() -> bool:
    try:
        with socket.create_connection(("127.0.0.1", PORT), timeout=3):
            return True
    except OSError:
        return False


def kill_stale() -> None:
    """Kill any leftover run_server.py processes (but never this watcher)."""
    ps = (
        "Get-CimInstance Win32_Process | "
        "Where-Object { $_.CommandLine -like '*run_server.py*' } | "
        "ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }"
    )
    subprocess.run(
        ["powershell", "-NoProfile", "-Command", ps],
        capture_output=True, text=True, check=False,
    )


def start_server() -> None:
    with LOG.open("a", encoding="utf-8") as f:
        subprocess.Popen(
            [str(VENV_PY), "run_server.py"],
            cwd=str(ROOT),
            stdout=f,
            stderr=subprocess.STDOUT,
            creationflags=subprocess.DETACHED_PROCESS
            | subprocess.CREATE_NEW_PROCESS_GROUP
            | getattr(subprocess, "CREATE_NO_WINDOW", 0),
            close_fds=True,
        )


def main() -> None:
    log("watchdog started")
    # Make sure we don't leave a duplicate already running from a previous launch.
    if not port_open():
        kill_stale()
        time.sleep(2)
        start_server()
        log("initial server start issued")
    else:
        log("server already up on launch")

    while True:
        if not port_open():
            log(f"port {PORT} closed -> restarting server")
            kill_stale()
            time.sleep(2)
            start_server()
            log("restart issued")
        time.sleep(CHECK_INTERVAL)


if __name__ == "__main__":
    main()
