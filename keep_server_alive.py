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
CHECK_INTERVAL = 2
WATCHED_SUFFIXES = {".py", ".html", ".css", ".js"}
_CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


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


def source_snapshot(root: Path = ROOT) -> dict[str, int]:
    """Return modification times for files that require a Flask restart.

    Templates are included deliberately: although Jinja can reload templates in
    development, this service runs with Flask's reloader disabled so a restart
    is the reliable way to apply every source change.
    """
    watched: list[Path] = [root / "run_server.py", root / ".env"]
    source_dir = root / "src"
    if source_dir.exists():
        watched.extend(
            path for path in source_dir.rglob("*")
            if path.is_file() and path.suffix.lower() in WATCHED_SUFFIXES
        )

    snapshot: dict[str, int] = {}
    for path in watched:
        try:
            snapshot[str(path.relative_to(root))] = path.stat().st_mtime_ns
        except FileNotFoundError:
            continue
    return snapshot


def kill_stale() -> None:
    """Stop only prior Web-server processes before a clean restart.

    Do not match ``keep_server_alive.py`` here.  On Windows ``pythonw.exe``
    starts a launcher and a worker process; killing the launcher can terminate
    this very watchdog as collateral damage.
    """
    ps = (
        "Get-CimInstance Win32_Process | "
        "Where-Object { $_.CommandLine -like '*run_server.py*' } | "
        "ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }"
    )
    subprocess.run(
        ["powershell", "-NoProfile", "-Command", ps],
        capture_output=True,
        text=True,
        check=False,
        creationflags=_CREATE_NO_WINDOW,
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
    watched_sources = source_snapshot()
    # On first launch the port is expected to be free; spawning the server
    # directly (without kill_stale) avoids a race where kill_stale's own
    # powershell probe could interfere with the just-spawned child.
    if not port_open():
        start_server()
        log("initial server start issued")
    else:
        log("server already up on launch")

    while True:
        current_sources = source_snapshot()
        if current_sources != watched_sources:
            log("source changed -> restarting server")
            kill_stale()
            time.sleep(2)
            start_server()
            log("source-change restart issued")
            watched_sources = current_sources
        elif not port_open():
            log(f"port {PORT} closed -> restarting server")
            kill_stale()
            time.sleep(2)
            start_server()
            log("health-check restart issued")
        time.sleep(CHECK_INTERVAL)


if __name__ == "__main__":
    main()
