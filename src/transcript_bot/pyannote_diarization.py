from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import subprocess

from transcript_bot.config import settings
from transcript_bot.transcription import TranscriptSegment


class PyannoteDiarizationError(RuntimeError):
    pass


@dataclass(frozen=True)
class SpeakerTurn:
    start: float
    end: float
    speaker: str


def diarize_with_pyannote(audio_path: Path) -> list[SpeakerTurn]:
    try:
        import torch
        from pyannote.audio import Pipeline
    except ImportError as exc:
        raise PyannoteDiarizationError(
            "尚未安裝 pyannote.audio。請依 README 建立 Python 3.11 虛擬環境並安裝 pyannote 選用套件。"
        ) from exc

    try:
        pipeline = Pipeline.from_pretrained(
            settings.pyannote_model,
            token=settings.pyannote_hf_token,
        )
        if torch.cuda.is_available():
            pipeline.to(torch.device("cuda"))

        options: dict[str, int] = {}
        if settings.pyannote_num_speakers:
            options["num_speakers"] = settings.pyannote_num_speakers
        result = pipeline(_load_audio_for_pyannote(audio_path, torch), **options)
        diarization = getattr(result, "exclusive_speaker_diarization", result.speaker_diarization)
        return [
            SpeakerTurn(start=float(turn.start), end=float(turn.end), speaker=str(speaker))
            for turn, _, speaker in diarization.itertracks(yield_label=True)
        ]
    except Exception as exc:
        raise PyannoteDiarizationError(
            "pyannote 語者分離失敗。請確認已接受 Hugging Face 模型使用條款、Token 有讀取權限，且音檔可正常讀取。"
        ) from exc


def _load_audio_for_pyannote(audio_path: Path, torch):
    """Decode through ffmpeg to avoid Windows torchcodec DLL compatibility issues."""
    result = subprocess.run(
        [
            "ffmpeg",
            "-v",
            "error",
            "-i",
            str(audio_path),
            "-vn",
            "-ac",
            "1",
            "-ar",
            "16000",
            "-f",
            "f32le",
            "pipe:1",
        ],
        capture_output=True,
        check=False,
    )
    if result.returncode != 0 or not result.stdout:
        raise PyannoteDiarizationError("無法將音檔載入 pyannote 進行語者分離。")

    waveform = torch.frombuffer(bytearray(result.stdout), dtype=torch.float32).reshape(1, -1)
    return {"waveform": waveform, "sample_rate": 16000}


def apply_pyannote_speakers(segments: list[TranscriptSegment], turns: list[SpeakerTurn]) -> list[TranscriptSegment]:
    if not turns:
        raise PyannoteDiarizationError("pyannote 未偵測到可用的語者區段。")

    assigned: list[TranscriptSegment] = []
    for segment in segments:
        speaker = _speaker_with_greatest_overlap(segment, turns)
        assigned.append(
            TranscriptSegment(
                speaker=speaker or segment.speaker,
                start=segment.start,
                end=segment.end,
                text=segment.text,
            )
        )
    return assigned


def _speaker_with_greatest_overlap(segment: TranscriptSegment, turns: list[SpeakerTurn]) -> str | None:
    if segment.start is None or segment.end is None:
        return None

    scores: dict[str, float] = {}
    for turn in turns:
        overlap = max(0.0, min(segment.end, turn.end) - max(segment.start, turn.start))
        if overlap:
            scores[turn.speaker] = scores.get(turn.speaker, 0.0) + overlap
    return max(scores, key=scores.get) if scores else None
