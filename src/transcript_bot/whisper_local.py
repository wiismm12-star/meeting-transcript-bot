from __future__ import annotations

from pathlib import Path
import re

from transcript_bot.config import settings
from transcript_bot.transcription import TranscriptSegment


class LocalWhisperError(RuntimeError):
    """Raised when the optional local Whisper runtime cannot transcribe audio."""


def transcribe_with_local_whisper(audio_path: Path) -> list[TranscriptSegment]:
    """Transcribe locally with faster-whisper, including timestamps for diarization."""
    try:
        from faster_whisper import WhisperModel
        from faster_whisper.utils import download_model
    except ImportError as exc:
        raise LocalWhisperError("尚未安裝本機 Whisper。請執行：uv sync --extra whisper") from exc

    device, compute_type = _runtime_options()
    prompt = settings.whisper_initial_prompt.strip() or settings.deepgram_keyterms.strip()
    model_dir = _model_directory()
    try:
        if not (model_dir / "model.bin").is_file():
            model_dir.mkdir(parents=True, exist_ok=True)
            # output_dir writes real files instead of Hugging Face cache symlinks.
            download_model(settings.whisper_model, output_dir=str(model_dir))
        model = WhisperModel(str(model_dir), device=device, compute_type=compute_type)
        segments, _ = model.transcribe(
            str(audio_path),
            language=settings.whisper_language or None,
            task="transcribe",
            beam_size=5,
            vad_filter=True,
            condition_on_previous_text=False,
            initial_prompt=prompt or None,
        )
        result = [
            TranscriptSegment("UNASSIGNED", float(segment.start), float(segment.end), segment.text.strip())
            for segment in segments
            if segment.text.strip()
        ]
    except PermissionError as exc:
        raise LocalWhisperError("本機 Whisper 模型目錄沒有寫入權限，請確認 data/models 可寫入。") from exc
    except Exception as exc:
        raise LocalWhisperError(
            f"本機 Whisper 轉錄或模型下載失敗（{device}/{compute_type}）。請稍後重試。"
        ) from exc

    if not result:
        raise LocalWhisperError("本機 Whisper 未辨識到語音內容。")
    return result


def _runtime_options() -> tuple[str, str]:
    """Keep large-v3 reliable on the owner's 4 GB GPU by defaulting to CPU INT8."""
    device = settings.whisper_device.lower().strip()
    if device not in {"cpu", "cuda"}:
        raise LocalWhisperError("WHISPER_DEVICE 只能是 cpu 或 cuda。")
    compute_type = settings.whisper_compute_type.strip()
    if not compute_type:
        raise LocalWhisperError("WHISPER_COMPUTE_TYPE 不可留白。")
    return device, compute_type


def _model_directory() -> Path:
    safe_name = re.sub(r"[^A-Za-z0-9._-]+", "-", settings.whisper_model).strip(".-") or "whisper-model"
    return settings.whisper_model_dir / safe_name
