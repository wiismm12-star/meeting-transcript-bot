"""Keep the meeting-transcript server alive, independent of the Hermes terminal.

Launched DETACHED from the OS (via PowerShell Start-Process) so it survives the
Hermes desktop app closing or cleaning up its background terminals. Every 30s it
checks port 8765; if the server is down it kills stale instances and restarts
run_server.py (also detached). All server output is appended to server.log.
"""
from __future__ import annotations

import json
import socket
import subprocess
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(r"C:\Users\lan\Documents\開會語音逐字稿")
# Use pythonw.exe (the windowless Windows launcher) so the server runs fully
# backgrounded with NO console/black window popping up.
VENV_PY = ROOT / ".venv" / "Scripts" / "pythonw.exe"
LOG = ROOT / "server.log"
TELEGRAM_LOG = ROOT / "telegram_bot.log"
PORT = 8765
CHECK_INTERVAL = 2
BOT_RESTART_SECONDS = 15
WATCHED_SUFFIXES = {".py", ".html", ".css", ".js"}
_CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
_last_bot_start = 0.0


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


def _env_value(root: Path, name: str) -> str:
    """Read one simple .env value without loading or exposing credentials."""
    try:
        for line in (root / ".env").read_text(encoding="utf-8").splitlines():
            key, separator, value = line.partition("=")
            if separator and key.strip() == name:
                return value.strip().strip('"').strip("'")
    except FileNotFoundError:
        pass
    return ""


def telegram_status_path(root: Path = ROOT) -> Path:
    configured = _env_value(root, "DATA_DIR") or "./data"
    data_dir = Path(configured)
    if not data_dir.is_absolute():
        data_dir = root / data_dir
    return data_dir / "telegram_bot_status.json"


def write_telegram_status(state: str, message: str, root: Path = ROOT) -> None:
    """Publish a credential-free Telegram worker status for the local Web UI."""
    path = telegram_status_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "state": state,
        "message": message,
        "updated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
    }
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    temporary.replace(path)


def telegram_configured(root: Path = ROOT) -> bool:
    return bool(_env_value(root, "TELEGRAM_BOT_TOKEN"))


def telegram_bot_running() -> bool:
    ps = (
        "Get-CimInstance Win32_Process | "
        "Where-Object { $_.CommandLine -match 'transcript_bot\\.main' } | "
        "Select-Object -First 1 -ExpandProperty ProcessId"
    )
    result = subprocess.run(
        ["powershell", "-NoProfile", "-Command", ps],
        capture_output=True,
        text=True,
        check=False,
        creationflags=_CREATE_NO_WINDOW,
    )
    return bool(result.stdout.strip())


def start_telegram_bot() -> None:
    with TELEGRAM_LOG.open("a", encoding="utf-8") as log_file:
        subprocess.Popen(
            [str(VENV_PY), "-m", "transcript_bot.main"],
            cwd=str(ROOT),
            stdout=log_file,
            stderr=subprocess.STDOUT,
            creationflags=subprocess.DETACHED_PROCESS
            | subprocess.CREATE_NEW_PROCESS_GROUP
            | _CREATE_NO_WINDOW,
            close_fds=True,
        )


def kill_stale_telegram_bot() -> None:
    ps = (
        "Get-CimInstance Win32_Process | "
        "Where-Object { $_.CommandLine -match 'transcript_bot\\.main' } | "
        "ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }"
    )
    subprocess.run(
        ["powershell", "-NoProfile", "-Command", ps],
        capture_output=True,
        text=True,
        check=False,
        creationflags=_CREATE_NO_WINDOW,
    )


def ensure_telegram_bot() -> None:
    global _last_bot_start
    if not telegram_configured():
        write_telegram_status("disabled", "尚未設定 Telegram Bot Token。")
        return
    if telegram_bot_running():
        write_telegram_status("running", "Polling 已啟用，等待 Telegram 訊息。")
        return
    now = time.monotonic()
    if now - _last_bot_start >= BOT_RESTART_SECONDS:
        _last_bot_start = now
        write_telegram_status("starting", "Bot 未運行，正在自動重新啟動。")
        start_telegram_bot()
        log("Telegram Bot restart issued")
    else:
        write_telegram_status("starting", "Bot 啟動中，正在等待連線。")


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
    global _last_bot_start
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
    ensure_telegram_bot()

    while True:
        current_sources = source_snapshot()
        if current_sources != watched_sources:
            log("source changed -> restarting server")
            kill_stale()
            kill_stale_telegram_bot()
            _last_bot_start = 0.0
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
        ensure_telegram_bot()
        time.sleep(CHECK_INTERVAL)


if __name__ == "__main__":
    main()
