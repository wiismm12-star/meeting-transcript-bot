from __future__ import annotations

from pathlib import Path
from typing import Any

import httpx

from transcript_bot.config import settings
from transcript_bot.retry import request_with_retry
from transcript_bot.transcription import TranscriptSegment


class DeepgramError(RuntimeError):
    pass


def transcribe_with_deepgram(audio_path: Path, word_timestamps: bool = False) -> list[TranscriptSegment]:
    keyterms = [term.strip() for term in settings.deepgram_keyterms.split(",") if term.strip()]
    params: dict[str, str | list[str]] = {
        "model": "nova-3",
        "smart_format": "true",
        "diarize_model": "latest",
        "utterances": "true",
        "language": "zh-TW",
    }
    if keyterms:
        params["keyterm"] = keyterms
    headers = {
        "Authorization": f"Token {settings.deepgram_api_key}",
        "Content-Type": "audio/mpeg",
    }

    try:
        audio_content = audio_path.read_bytes()
        response = request_with_retry(
            lambda: httpx.post(
            "https://api.deepgram.com/v1/listen",
            params=params,
            headers=headers,
                content=audio_content,
            timeout=settings.deepgram_timeout,
            ),
            action="Deepgram 轉錄",
        )
    except httpx.RequestError as exc:
        raise DeepgramError("Deepgram API 暫時無法連線，請稍後再試。") from exc

    if response.status_code >= 400:
        raise DeepgramError(_format_deepgram_error(response))

    payload = response.json()
    if word_timestamps:
        return _parse_words(payload, use_deepgram_speakers=False)
    segments = _parse_utterances(payload)
    if segments:
        return segments
    return _parse_words(payload)


def _parse_utterances(payload: dict[str, Any]) -> list[TranscriptSegment]:
    utterances = payload.get("results", {}).get("utterances") or []
    segments: list[TranscriptSegment] = []

    for index, utterance in enumerate(utterances, start=1):
        text = str(utterance.get("transcript") or "").strip()
        if not text:
            continue
        speaker = utterance.get("speaker")
        segments.append(
            TranscriptSegment(
                speaker=f"SPEAKER_{speaker}" if speaker is not None else f"SPEAKER_{index:02d}",
                start=_to_float(utterance.get("start")),
                end=_to_float(utterance.get("end")),
                text=text,
            )
        )
    # The API normally returns utterances in timeline order, but do not make
    # the player alignment depend on that transport ordering.
    return sorted(
        segments,
        key=lambda segment: (
            segment.start is None,
            segment.start if segment.start is not None else float("inf"),
            segment.end if segment.end is not None else float("inf"),
        ),
    )


def _parse_words(payload: dict[str, Any], use_deepgram_speakers: bool = True) -> list[TranscriptSegment]:
    channels = payload.get("results", {}).get("channels") or []
    if not channels:
        return []

    alternatives = channels[0].get("alternatives") or []
    if not alternatives:
        return []

    words = alternatives[0].get("words") or []
    if not words:
        transcript = str(alternatives[0].get("transcript") or "").strip()
        return [TranscriptSegment("Speaker 1", None, None, transcript)] if transcript else []

    if not use_deepgram_speakers:
        return [
            TranscriptSegment(
                speaker="UNASSIGNED",
                start=_to_float(word.get("start")),
                end=_to_float(word.get("end")),
                text=str(word.get("punctuated_word") or word.get("word") or "").strip(),
            )
            for word in words
            if str(word.get("punctuated_word") or word.get("word") or "").strip()
        ]

    segments: list[TranscriptSegment] = []
    current_speaker: str | None = None
    current_words: list[str] = []
    start: float | None = None
    end: float | None = None

    for word in words:
        speaker_value = word.get("speaker")
        speaker = f"SPEAKER_{speaker_value}" if speaker_value is not None else "SPEAKER_00"
        if current_speaker is not None and speaker != current_speaker:
            segments.append(TranscriptSegment(current_speaker, start, end, " ".join(current_words)))
            current_words = []
            start = None

        current_speaker = speaker
        if start is None:
            start = _to_float(word.get("start"))
        end = _to_float(word.get("end"))
        current_words.append(str(word.get("punctuated_word") or word.get("word") or "").strip())

    if current_speaker and current_words:
        segments.append(TranscriptSegment(current_speaker, start, end, " ".join(current_words)))

    return segments


def _format_deepgram_error(response: httpx.Response) -> str:
    if response.status_code in {401, 403}:
        return "Deepgram API 驗證失敗，請確認伺服器端 API 金鑰設定。"
    if response.status_code == 429:
        return "Deepgram API 暫時達到用量或速率限制，請稍後再試。"
    if response.status_code >= 500:
        return "Deepgram API 暫時無法處理請求，請稍後再試。"
    return f"Deepgram API 回傳錯誤 {response.status_code}，請確認音檔格式後再試。"


def _to_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
