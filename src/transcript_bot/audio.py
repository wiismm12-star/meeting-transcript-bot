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
        "-ac",
        "1",
        "-ar",
        "16000",
        "-b:a",
        "64k",
        str(output_path),
    ]
    result = subprocess.run(command, capture_output=True, text=True, check=False)
    if result.returncode != 0:
        raise AudioProcessingError("音訊轉檔失敗，請確認檔案格式正確，或改傳其他音訊檔。")
