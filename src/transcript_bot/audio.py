from __future__ import annotations

import shutil
import subprocess
from pathlib import Path


class AudioProcessingError(RuntimeError):
    pass


def ensure_ffmpeg() -> None:
    if shutil.which("ffmpeg") is None:
        raise AudioProcessingError("找不到 ffmpeg，請先安裝 ffmpeg 並加入 PATH。")


def normalize_audio(input_path: Path, output_path: Path) -> None:
    ensure_ffmpeg()
    command = [
        "ffmpeg",
        "-y",
        "-i",
        str(input_path),
        "-vn",
        "-ar",
        "16000",
        "-b:a",
        "64k",
        str(output_path),
    ]
    result = subprocess.run(command, capture_output=True, text=True, check=False)
    if result.returncode != 0:
        raise AudioProcessingError("音訊轉檔失敗，請確認檔案格式正確，或改傳其他音訊檔。")


def get_audio_duration(path: Path) -> float:
    """Return audio duration in seconds using ffprobe (requires ffmpeg)."""
    result = subprocess.run(
        [
            "ffprobe", "-v", "error", "-show_entries",
            "format=duration", "-of", "default=noprint_wrappers=1:nokey=1",
            str(path),
        ],
        capture_output=True, text=True, check=False,
    )
    if result.returncode != 0:
        return 0.0
    try:
        return float(result.stdout.strip())
    except ValueError:
        return 0.0
