from __future__ import annotations

import time
from pathlib import Path
from typing import Any

import httpx

from transcript_bot.config import settings
from transcript_bot.transcription import TranscriptSegment


class GladiaError(RuntimeError):
    pass


_API_URL = "https://api.gladia.io/v2"


def transcribe_with_gladia(audio_path: Path) -> list[TranscriptSegment]:
    """Transcribe an uploaded recording with Gladia's pre-recorded API.

    Gladia requires a temporary upload URL before creating the transcription job.
    The completed remote job is deleted best-effort after its transcript is read.
    """

    headers = {"x-gladia-key": settings.gladia_api_key}
    job_id = ""
    try:
        with audio_path.open("rb") as audio_file:
            upload = httpx.post(
                f"{_API_URL}/upload",
                headers=headers,
                files={"audio": (audio_path.name, audio_file, "audio/mpeg")},
                timeout=300,
            )
        _raise_for_error(upload, "上傳音檔")
        audio_url = str(upload.json().get("audio_url") or "")
        if not audio_url:
            raise GladiaError("Gladia 上傳完成後未回傳音檔位置。")

        request_payload = _transcription_payload(audio_url)
        created = httpx.post(
            f"{_API_URL}/pre-recorded",
            headers={**headers, "Content-Type": "application/json"},
            json=request_payload,
            timeout=60,
        )
        _raise_for_error(created, "建立轉錄工作")
        job_id = str(created.json().get("id") or "")
        if not job_id:
            raise GladiaError("Gladia 未回傳轉錄工作 ID。")

        result = _wait_for_result(job_id, headers)
        return _parse_result(result)
    finally:
        if job_id:
            try:
                httpx.delete(f"{_API_URL}/pre-recorded/{job_id}", headers=headers, timeout=30)
            except httpx.HTTPError:
                pass


def _transcription_payload(audio_url: str) -> dict[str, Any]:
    vocabulary = [term.strip() for term in settings.gladia_vocabulary.split(",") if term.strip()]
    diarization_config: dict[str, int] = {}
    if settings.gladia_num_speakers > 0:
        diarization_config["number_of_speakers"] = settings.gladia_num_speakers

    payload: dict[str, Any] = {
        "audio_url": audio_url,
        "diarization": True,
        "diarization_config": diarization_config,
        "sentences": True,
        "punctuation_enhanced": True,
        "language_config": {"languages": ["zh", "en"], "code_switching": True},
        "custom_vocabulary": bool(vocabulary),
    }
    if vocabulary:
        payload["custom_vocabulary_config"] = {"vocabulary": vocabulary}
    return payload


def _wait_for_result(job_id: str, headers: dict[str, str]) -> dict[str, Any]:
    for _ in range(300):
        response = httpx.get(f"{_API_URL}/pre-recorded/{job_id}", headers=headers, timeout=60)
        _raise_for_error(response, "讀取轉錄結果")
        payload = response.json()
        status = str(payload.get("status") or "").lower()
        if status == "done":
            return payload
        if status in {"error", "failed", "canceled", "cancelled"}:
            raise GladiaError("Gladia 無法完成音檔轉錄，請確認音檔後再試。")
        time.sleep(1)
    raise GladiaError("Gladia 轉錄逾時，請稍後再試。")


def _parse_result(payload: dict[str, Any]) -> list[TranscriptSegment]:
    transcription = payload.get("result", {}).get("transcription", {})
    utterances = transcription.get("utterances") or []
    segments: list[TranscriptSegment] = []
    for index, utterance in enumerate(utterances, start=1):
        text = str(utterance.get("text") or "").strip()
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
    if segments:
        return segments

    text = str(transcription.get("full_transcript") or "").strip()
    return [TranscriptSegment("SPEAKER_01", None, None, text)] if text else []


def _raise_for_error(response: httpx.Response, action: str) -> None:
    if response.status_code < 400:
        return
    if response.status_code in {401, 403}:
        raise GladiaError("Gladia API 驗證失敗，請確認伺服器端 API 金鑰設定。")
    if response.status_code == 429:
        raise GladiaError("Gladia 免費額度或速率已達上限，請稍後再試。")
    if response.status_code >= 500:
        raise GladiaError("Gladia 暫時無法處理請求，請稍後再試。")
    raise GladiaError(f"Gladia {action}失敗，請確認音檔格式後再試。")


def _to_float(value: Any) -> float | None:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None
