"""Small cross-process progress and cancellation records for transcription jobs."""
from __future__ import annotations

import json
import os
from pathlib import Path


def _directory(data_dir: Path) -> Path:
    return data_dir / "job-status"


def _path(data_dir: Path, job_id: str) -> Path:
    return _directory(data_dir) / f"{job_id}.json"


def write_job_status(data_dir: Path, job_id: str, **status) -> None:
    """Atomically publish only non-sensitive UI state shared with the Web app."""
    status.setdefault("owner_pid", os.getpid())
    directory = _directory(data_dir)
    directory.mkdir(parents=True, exist_ok=True)
    target = _path(data_dir, job_id)
    temporary = target.with_suffix(".tmp")
    temporary.write_text(json.dumps(status, ensure_ascii=False), encoding="utf-8")
    temporary.replace(target)


def get_job_status(data_dir: Path, job_id: str) -> dict | None:
    try:
        payload = json.loads(_path(data_dir, job_id).read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def list_active_job_statuses(data_dir: Path) -> dict[str, dict]:
    records: dict[str, dict] = {}
    try:
        paths = list(_directory(data_dir).glob("*.json"))
    except OSError:
        return records
    for path in paths:
        # A writer replaces its JSON atomically. Windows can invalidate a
        # directory entry held by glob during that replacement; skip that one
        # polling tick instead of returning HTTP 500 to the Web page.
        try:
            job_id = path.name.removesuffix(".json")
        except OSError:
            continue
        status = get_job_status(data_dir, job_id)
        if is_job_active(status):
            records[job_id] = status
    return records


def is_job_active(status: dict | None) -> bool:
    """A status belongs to a live Telegram worker, not a killed predecessor."""
    if not status or status.get("source") != "telegram" or status.get("step") in {"done", "error", "cancelled"}:
        return False
    owner_pid = status.get("owner_pid")
    if not isinstance(owner_pid, int):
        return False
    if os.name == "nt":
        # Unlike POSIX, ``os.kill(pid, 0)`` is not a harmless existence probe
        # on Windows: it can terminate the target process. Query its handle
        # instead, because this function runs every two seconds in the Web UI.
        import ctypes

        process_query_limited_information = 0x1000
        handle = ctypes.windll.kernel32.OpenProcess(
            process_query_limited_information, False, owner_pid
        )
        if not handle:
            return False
        ctypes.windll.kernel32.CloseHandle(handle)
        return True
    try:
        os.kill(owner_pid, 0)
    except OSError:
        return False
    return True


def request_cancel(data_dir: Path, job_id: str) -> None:
    status = get_job_status(data_dir, job_id) or {"source": "telegram"}
    status.update(step="cancelling", label="cancelling (等待目前步驟停止)", cancel_requested=True)
    write_job_status(data_dir, job_id, **status)


def cancel_requested(data_dir: Path, job_id: str) -> bool:
    return bool((get_job_status(data_dir, job_id) or {}).get("cancel_requested"))


def clear_job_status(data_dir: Path, job_id: str) -> None:
    try:
        _path(data_dir, job_id).unlink()
    except FileNotFoundError:
        pass
