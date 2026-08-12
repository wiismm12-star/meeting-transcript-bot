from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from uuid import uuid4


@dataclass(frozen=True)
class JobPaths:
    job_id: str
    job_dir: Path
    input_audio: Path
    normalized_audio: Path
    transcript_txt: Path
    transcript_docx: Path


def ensure_data_dirs(data_dir: Path) -> None:
    data_dir.mkdir(parents=True, exist_ok=True)
    (data_dir / "jobs").mkdir(parents=True, exist_ok=True)


def purge_stale_transcription_jobs(data_dir: Path) -> list[str]:
    """Remove interrupted, unfinished jobs whose recorded owner is no longer alive.

    This is deliberately conservative: completed meetings, visible error cards,
    and jobs whose worker still exists are preserved.  It is safe to call before
    accepting a new upload from Web, Telegram, or LINE.
    """
    from transcript_bot.database import delete_meeting, list_local_meeting_exports
    from transcript_bot.job_status import clear_job_status, get_job_status, is_job_active

    removed: list[str] = []
    jobs_dir = (data_dir / "jobs").resolve()
    for meeting in list_local_meeting_exports(data_dir):
        if meeting.transcript_text.strip():
            continue
        status = get_job_status(data_dir, meeting.id)
        is_terminal_error = bool(status and status.get("step") == "error")
        has_stale_active_status = bool(status and not is_terminal_error and not is_job_active(status))
        if not has_stale_active_status:
            continue
        job_dir = (jobs_dir / meeting.id).resolve()
        if job_dir.parent == jobs_dir and job_dir.is_dir():
            import shutil
            shutil.rmtree(job_dir)
        if delete_meeting(data_dir, meeting.id, meeting.user_id):
            clear_job_status(data_dir, meeting.id)
            removed.append(meeting.id)
    return removed


def create_job_paths(data_dir: Path, suffix: str) -> JobPaths:
    job_id = uuid4().hex
    date_prefix = datetime.now().strftime("%Y%m%d")
    job_dir = data_dir / "jobs" / job_id
    job_dir.mkdir(parents=True, exist_ok=True)

    clean_suffix = suffix if suffix.startswith(".") else f".{suffix}"
    return JobPaths(
        job_id=job_id,
        job_dir=job_dir,
        input_audio=job_dir / f"input{clean_suffix}",
        normalized_audio=job_dir / "normalized.mp3",
        transcript_txt=job_dir / f"meeting_{date_prefix}_{job_id}.txt",
        transcript_docx=job_dir / f"meeting_{date_prefix}_{job_id}.docx",
    )
