from __future__ import annotations

import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from transcript_bot.config import settings


class AudioProcessingError(RuntimeError):
    pass


@dataclass
class AudioChunk:
    """One piece of a long recording.

    ``start``/``end`` are the *nominal* boundaries this chunk "owns" in the
    final merged timeline. ``audio_start``/``audio_end`` are the actual span of
    the re-encoded audio file, which is widened by the overlap margin on the
    interior sides so Whisper has context at the cut and the overlap can be
    de-duplicated after transcription.
    """

    index: int
    start: float
    end: float
    audio_start: float
    audio_end: float
    audio_path: Path


_SILENCE_NOISE_DB = "-30dB"
# ``capture_output`` hides ffmpeg's text, but on Windows it does not stop a
# console-mode executable from briefly creating its own black console window.
_CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def split_audio_at_silence(
    normalized_path: Path,
    out_dir: Path,
    chunk_max_seconds: int | None = None,
    overlap_seconds: float | None = None,
    min_silence_seconds: float | None = None,
) -> list[AudioChunk]:
    """Split a normalized recording at silence boundaries for parallel transcription.

    Short recordings (no longer than ``chunk_max_seconds``) are returned as a
    single chunk that reuses the already-normalized file, so there is no extra
    re-encode and no quality loss for the common case.
    """
    chunk_max_seconds = chunk_max_seconds or settings.chunk_max_seconds
    overlap_seconds = overlap_seconds if overlap_seconds is not None else settings.chunk_overlap_seconds
    min_silence_seconds = min_silence_seconds if min_silence_seconds is not None else settings.chunk_min_silence_seconds

    duration = get_audio_duration(normalized_path)
    if duration <= 0 or duration <= chunk_max_seconds:
        return [AudioChunk(0, 0.0, duration, 0.0, duration, normalized_path)]

    silences = _detect_silences(normalized_path, min_silence_seconds)
    boundaries = _compute_boundaries(duration, chunk_max_seconds, silences)
    out_dir.mkdir(parents=True, exist_ok=True)

    chunks: list[AudioChunk] = []
    for index, (start, end) in enumerate(boundaries):
        lead = overlap_seconds if index > 0 else 0.0
        tail = overlap_seconds if index < len(boundaries) - 1 else 0.0
        audio_start = max(0.0, start - lead)
        audio_end = min(duration, end + tail)
        chunk_path = out_dir / f"chunk_{index:03d}.mp3"
        _extract_span(normalized_path, chunk_path, audio_start, audio_end)
        chunks.append(AudioChunk(index, start, end, audio_start, audio_end, chunk_path))
    return chunks


def _detect_silences(normalized_path: Path, min_silence_seconds: float) -> list[tuple[float, float]]:
    """Return list of (silence_start, silence_end) via ffprobe silencedetect."""
    ensure_ffmpeg()
    result = subprocess.run(
        [
            "ffprobe", "-v", "error",
            "-af", f"silencedetect=noise={_SILENCE_NOISE_DB}:d={min_silence_seconds}",
            "-f", "null", "-",
            "-i", str(normalized_path),
        ],
        capture_output=True, text=True, check=False, creationflags=_CREATE_NO_WINDOW,
    )
    silence_starts: list[float] = []
    silences: list[tuple[float, float]] = []
    for line in result.stderr.splitlines():
        match = re.search(r"silence_start:\s*([0-9]*\.?[0-9]+)", line)
        if match:
            silence_starts.append(float(match.group(1)))
            continue
        match = re.search(r"silence_end:\s*([0-9]*\.?[0-9]+)", line)
        if match and silence_starts:
            silences.append((silence_starts.pop(0), float(match.group(1))))
    return silences


def _compute_boundaries(duration: float, chunk_max_seconds: int, silences: list[tuple[float, float]]) -> list[tuple[float, float]]:
    """Greedily cut near ``chunk_max_seconds`` but snap to the nearest silence."""
    boundaries: list[tuple[float, float]] = []
    cursor = 0.0
    tolerance = chunk_max_seconds * 0.5
    while cursor < duration - 1.0:
        target = cursor + chunk_max_seconds
        if target >= duration:
            boundaries.append((cursor, duration))
            break
        cut = _nearest_silence_midpoint(silences, target, tolerance) or target
        cut = min(max(cut, cursor + 30.0), duration - 30.0) if duration > 60 else cut
        boundaries.append((cursor, cut))
        cursor = cut
    return boundaries


def _nearest_silence_midpoint(silences: list[tuple[float, float]], target: float, tolerance: float) -> float | None:
    best: float | None = None
    best_dist = float("inf")
    for start, end in silences:
        midpoint = (start + end) / 2.0
        if target - tolerance <= midpoint <= target + tolerance:
            dist = abs(midpoint - target)
            if dist < best_dist:
                best_dist = dist
                best = midpoint
    return best


def _extract_span(input_path: Path, output_path: Path, start: float, end: float) -> None:
    ensure_ffmpeg()
    command = [
        "ffmpeg", "-y",
        "-ss", f"{start:.3f}",
        "-to", f"{end:.3f}",
        "-i", str(input_path),
        "-vn", "-ar", "16000", "-b:a", "64k",
        str(output_path),
    ]
    result = subprocess.run(
        command, capture_output=True, text=True, check=False, creationflags=_CREATE_NO_WINDOW
    )
    if result.returncode != 0:
        raise AudioProcessingError("音檔切割失敗，請確認 ffmpeg 可正常運作。")


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
    result = subprocess.run(
        command, capture_output=True, text=True, check=False, creationflags=_CREATE_NO_WINDOW
    )
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
        capture_output=True, text=True, check=False, creationflags=_CREATE_NO_WINDOW,
    )
    if result.returncode != 0:
        return 0.0
    try:
        return float(result.stdout.strip())
    except ValueError:
        return 0.0


def decode_audio_to_pcm(
    input_path: Path,
    sample_rate: int = 16000,
    channels: int = 1,
) -> tuple[np.ndarray, int]:
    """Decode any audio file to a float32 mono PCM waveform via ffmpeg.

    Returns ``(samples, sample_rate)`` where ``samples`` is a 1-D ``float32``
    numpy array in ``[-1, 1]``. This is the **same** decode path the diarizer
    uses, so Whisper and pyannote always consume bit-identical audio.

    Faster-whisper accepts a raw waveform array, letting us bypass torchcodec
    entirely — torchcodec fails to load on this machine, and its absence makes
    faster-whisper fall back to a different decoder whose MP3 priming-silence
    handling shifts the timeline (dropping the first seconds and ruining speaker
    alignment). Routing both stages through ffmpeg keeps results deterministic.
    """
    ensure_ffmpeg()
    result = subprocess.run(
        [
            "ffmpeg", "-v", "error",
            "-i", str(input_path),
            "-vn",
            "-ac", str(channels),
            "-ar", str(sample_rate),
            "-f", "f32le",
            "pipe:1",
        ],
        capture_output=True, check=False, creationflags=_CREATE_NO_WINDOW,
    )
    if result.returncode != 0 or not result.stdout:
        raise AudioProcessingError("音訊解碼失敗，請確認 ffmpeg 可正常運作。")
    waveform = np.frombuffer(bytearray(result.stdout), dtype=np.float32)
    return waveform, sample_rate
