from __future__ import annotations

import threading
from pathlib import Path
import re

from transcript_bot.config import settings
from transcript_bot.transcription import TranscriptSegment


class LocalWhisperError(RuntimeError):
    """Raised when the optional local Whisper runtime cannot transcribe audio."""


def load_whisper_model():
    """Load a faster-whisper model once; reused across parallel chunk jobs.

    Returns the loaded ``WhisperModel``. Raises ``LocalWhisperError`` if the
    optional dependency or model is unavailable.
    """
    _register_nvidia_dlls()
    try:
        from faster_whisper import WhisperModel
        from faster_whisper.utils import download_model
    except ImportError as exc:
        raise LocalWhisperError("尚未安裝本機 Whisper。請執行：uv sync --extra whisper") from exc

    device, compute_type = _runtime_options()
    prompt = settings.whisper_initial_prompt.strip()
    model_dir = _model_directory()
    try:
        if not (model_dir / "model.bin").is_file():
            model_dir.mkdir(parents=True, exist_ok=True)
            download_model(settings.whisper_model, output_dir=str(model_dir))
        model = WhisperModel(str(model_dir), device=device, compute_type=compute_type)
    except PermissionError as exc:
        raise LocalWhisperError("本機 Whisper 模型目錄沒有寫入權限，請確認 data/models 可寫入。") from exc
    except Exception as exc:
        raise LocalWhisperError(
            f"本機 Whisper 模型載入失敗（{device}/{compute_type}）。請稍後重試。"
        ) from exc
    model._whisper_prompt = prompt  # type: ignore[attr-defined]
    return model


def transcribe_with_local_whisper(audio_path: Path, progress_callback=None) -> list[TranscriptSegment]:
    """Transcribe one audio file locally with faster-whisper, with timestamps."""
    model = load_whisper_model()
    return transcribe_chunk_with_model(model, audio_path, progress_callback=progress_callback)


def transcribe_chunk_with_model(
    model, audio_path: Path, progress_callback=None
) -> list[TranscriptSegment]:
    """Transcribe a single chunk using a pre-loaded model (thread-safe enough for CPU)."""
    prompt = getattr(model, "_whisper_prompt", None)
    try:
        segments_iter, _ = model.transcribe(
            str(audio_path),
            language=settings.whisper_language or None,
            task="transcribe",
            beam_size=5,
            vad_filter=True,
                vad_parameters={
                    "min_silence_duration_ms": 500,
                },
                condition_on_previous_text=False,
            initial_prompt=prompt or None,
        )
        result = []
        for segment in segments_iter:
            if segment.text.strip():
                result.append(
                    TranscriptSegment(
                        "UNASSIGNED", float(segment.start), float(segment.end), segment.text.strip()
                    )
                )
                if progress_callback and segment.end is not None:
                    progress_callback(segment.end, float(segment.end))
    except Exception as exc:
        raise LocalWhisperError(
            f"本機 Whisper 轉錄失敗。請稍後重試。"
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


def _register_nvidia_dlls() -> None:
    """Make pip-installed NVIDIA CUDA DLLs discoverable on Windows.

    ``os.add_dll_directory`` alone is not enough for ctranslate2's lazy CUDA
    loading — we prepend the directory to ``PATH`` so any DLL lookup can succeed.
    """
    import os as _os
    _nv = Path(__file__).resolve().parents[2] / ".venv" / "Lib" / "site-packages" / "nvidia"
    for _sub in ("cublas/bin", "cuda_runtime/bin"):
        _p = _nv / _sub
        if _p.is_dir():
            _os.add_dll_directory(str(_p))
            _os.environ["PATH"] = str(_p) + _os.pathsep + _os.environ.get("PATH", "")
