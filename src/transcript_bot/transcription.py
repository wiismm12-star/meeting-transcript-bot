from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from openai import OpenAI

from transcript_bot.config import settings


@dataclass
class TranscriptSegment:
    speaker: str
    start: float | None
    end: float | None
    text: str


def transcribe_with_diarization(audio_path: Path) -> list[TranscriptSegment]:
    if settings.transcribe_provider == "deepgram":
        from transcript_bot.deepgram import transcribe_with_deepgram

        return transcribe_with_deepgram(audio_path)

    client = OpenAI(api_key=settings.openai_api_key)
    with audio_path.open("rb") as audio_file:
        response = client.audio.transcriptions.create(
            model=settings.openai_transcribe_model,
            file=audio_file,
            response_format="diarized_json",
            chunking_strategy="auto",
        )

    payload = response.model_dump() if hasattr(response, "model_dump") else response
    return parse_diarized_response(payload)


def parse_diarized_response(payload: dict[str, Any]) -> list[TranscriptSegment]:
    raw_segments = payload.get("segments") or payload.get("diarization") or []
    segments: list[TranscriptSegment] = []

    for index, item in enumerate(raw_segments, start=1):
        speaker = str(item.get("speaker") or item.get("speaker_label") or f"SPEAKER_{index:02d}")
        text = str(item.get("text") or "").strip()
        if not text:
            continue
        segments.append(
            TranscriptSegment(
                speaker=speaker,
                start=_to_float(item.get("start")),
                end=_to_float(item.get("end")),
                text=text,
            )
        )

    if not segments and payload.get("text"):
        segments.append(
            TranscriptSegment(
                speaker="Speaker 1",
                start=None,
                end=None,
                text=str(payload["text"]).strip(),
            )
        )

    return segments


def _to_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
