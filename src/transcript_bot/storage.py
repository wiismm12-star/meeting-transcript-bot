from __future__ import annotations

from dataclasses import dataclass
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


def create_job_paths(data_dir: Path, suffix: str) -> JobPaths:
    job_id = uuid4().hex
    job_dir = data_dir / "jobs" / job_id
    job_dir.mkdir(parents=True, exist_ok=True)

    clean_suffix = suffix if suffix.startswith(".") else f".{suffix}"
    return JobPaths(
        job_id=job_id,
        job_dir=job_dir,
        input_audio=job_dir / f"input{clean_suffix}",
        normalized_audio=job_dir / "normalized.mp3",
        transcript_txt=job_dir / "transcript.txt",
        transcript_docx=job_dir / "transcript.docx",
    )
