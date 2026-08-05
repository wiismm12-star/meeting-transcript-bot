from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from openai import OpenAI

from transcript_bot.audio import split_audio_at_silence
from transcript_bot.config import settings


@dataclass
class TranscriptSegment:
    speaker: str
    start: float | None
    end: float | None
    text: str


def transcribe_audio_smart(audio_path: Path, progress_callback=None, chunk_label_callback=None) -> list[TranscriptSegment]:
    """Transcribe a recording, splitting long files into chunks for parallel work.

    Short recordings (<= chunk_max_seconds) go through the single-file path
    unchanged. Longer recordings are split at silence boundaries, transcribed in
    parallel, then merged into one continuous timeline so the caller still gets a
    single transcript and a single meeting record.
    ``chunk_label_callback(completed, total)`` is invoked each time a chunk
    finishes so the UI can display e.g. ``分段語音辨識 (5/12)``.
    """
    chunks = split_audio_at_silence(audio_path, audio_path.parent / "chunks")
    if len(chunks) == 1:
        return transcribe_with_diarization(audio_path, progress_callback=progress_callback)

    provider = settings.transcribe_provider
    if provider == "whisper":
        from transcript_bot.whisper_local import load_whisper_model

        model = load_whisper_model()
        return _transcribe_chunks_parallel(
            chunks,
            lambda chunk, cb: _transcribe_whisper_chunk(model, chunk, cb),
            provider,
            progress_callback=progress_callback,
            chunk_label_callback=chunk_label_callback,
        )

    return _transcribe_chunks_parallel(
        chunks,
        lambda chunk, cb: _transcribe_cloud_chunk(chunk, cb),
        provider,
        progress_callback=progress_callback,
        chunk_label_callback=chunk_label_callback,
    )


def _transcribe_whisper_chunk(model, chunk, progress_callback=None):
    from transcript_bot.whisper_local import transcribe_chunk_with_model

    return transcribe_chunk_with_model(model, chunk.audio_path, progress_callback=progress_callback)


def _transcribe_cloud_chunk(chunk, progress_callback=None) -> list[TranscriptSegment]:
    return transcribe_with_diarization(chunk.audio_path, progress_callback=progress_callback)


def _transcribe_chunks_parallel(chunks, transcribe_fn, provider, progress_callback=None, chunk_label_callback=None) -> list[TranscriptSegment]:
    """Transcribe chunks in parallel and merge into one timeline.

    ``transcribe_fn`` receives a ``(chunk, chunk_progress)`` pair where
    ``chunk_progress`` is a callback accepting the same (local_time, _) contract
    as the rest of the codebase; it is bridged to the outer ``progress_callback``
    by adding the chunk's global audio offset, so the caller's progress math (a
    fraction of the full recording) keeps working unchanged.

    ``chunk_label_callback(completed, total)`` is called each time a chunk
    finishes, giving the UI a chance to display chunk-level progress.

    When ``chunk_max_workers == 1``, chunks are transcribed sequentially in the
    calling thread (avoids CTranslate2 / ThreadPoolExecutor compatibility issues).
    """
    total = len(chunks)
    results: list[list[TranscriptSegment] | None] = [None] * total
    errors: list[Exception | None] = [None] * total
    completed = 0

    def _full_duration() -> float:
        return chunks[-1].end

    if settings.chunk_max_workers <= 1:
        # Sequential: no thread pool, safe for CTranslate2's single-thread model.
        for i, chunk in enumerate(chunks):
            try:
                raw = transcribe_fn(chunk, None)
                results[i] = _shift_and_clamp(chunk, raw)
            except Exception as exc:  # noqa: BLE001
                errors[i] = exc
            completed += 1
            if chunk_label_callback is not None:
                chunk_label_callback(completed, total)
            if progress_callback is not None:
                pct = 20 + int((completed / total) * 70)
                progress_callback(float(pct), -999.0)
    else:
        counters_lock = threading.Lock()
        def _bridge(chunk, local_time: float, _unused: float) -> None:
            if progress_callback is None:
                return
            if local_time < 0:
                progress_callback(min(chunk.end, _full_duration()), 0.0)
            else:
                progress_callback(chunk.audio_start + local_time, 0.0)

        def _worker(index: int, chunk) -> None:
            nonlocal completed
            try:
                raw = transcribe_fn(chunk, lambda t, u: _bridge(chunk, t, u))
                results[index] = _shift_and_clamp(chunk, raw)
            except Exception as exc:  # noqa: BLE001
                errors[index] = exc
            finally:
                with counters_lock:
                    completed += 1
                if chunk_label_callback is not None:
                    chunk_label_callback(completed, total)
                if progress_callback is not None:
                    pct = 20 + int((completed / total) * 70)
                    progress_callback(float(pct), -999.0)

        with ThreadPoolExecutor(max_workers=settings.chunk_max_workers) as executor:
            futures = [executor.submit(_worker, i, c) for i, c in enumerate(chunks)]
            for _ in as_completed(futures):
                pass

    first_error = next((err for err in errors if err is not None), None)
    if first_error is not None:
        raise first_error

    merged: list[TranscriptSegment] = []
    for segs in results:
        if segs:
            merged.extend(segs)
    merged.sort(key=lambda seg: seg.start if seg.start is not None else 0.0)
    if progress_callback is not None:
        progress_callback(-1, -1)  # signal completion
    return merged


def _shift_and_clamp(chunk, segments: list[TranscriptSegment]) -> list[TranscriptSegment]:
    """Offset chunk-local times to the global timeline and drop the overlap margins.

    The chunk audio was widened by the overlap margin on its interior sides so the
    model has context at the cut. We keep only the span this chunk "owns"
    ([chunk.start, chunk.end]); content in the lead/tail overlap belongs to the
    neighbouring chunk and would otherwise be duplicated.
    """
    owned: list[TranscriptSegment] = []
    for seg in segments:
        speaker = _chunk_speaker_label(chunk.index, seg.speaker)
        if seg.start is None or seg.end is None:
            owned.append(
                TranscriptSegment(speaker=speaker, start=chunk.start, end=chunk.end, text=seg.text)
            )
            continue
        global_start = chunk.audio_start + seg.start
        global_end = chunk.audio_start + seg.end
        if global_start < chunk.start or global_end > chunk.end:
            continue
        owned.append(
            TranscriptSegment(speaker=speaker, start=global_start, end=global_end, text=seg.text)
        )
    return owned


def _chunk_speaker_label(chunk_index: int, raw_speaker: str) -> str:
    """Prefix chunk index only when the provider produced real speaker labels.

    Without pyannote / diarization every chunk returns ``UNASSIGNED``; prefixing
    those would turn one speaker into N fake ones (one per chunk) after
    ``normalize_speaker_labels`` runs on the merged result.
    """
    if raw_speaker and raw_speaker != "UNASSIGNED":
        return f"c{chunk_index}_{raw_speaker}"
    return raw_speaker


def transcribe_with_diarization(audio_path: Path, progress_callback=None) -> list[TranscriptSegment]:
    if settings.transcribe_provider == "deepgram":
        from transcript_bot.deepgram import transcribe_with_deepgram

        if settings.enable_pyannote_diarization:
            from transcript_bot.pyannote_diarization import apply_pyannote_speakers, diarize_with_pyannote

            transcript_segments = transcribe_with_deepgram(audio_path, word_timestamps=True)
            return apply_pyannote_speakers(transcript_segments, diarize_with_pyannote(audio_path))
        return transcribe_with_deepgram(audio_path)

    if settings.transcribe_provider == "gladia":
        from transcript_bot.gladia import transcribe_with_gladia

        return transcribe_with_gladia(audio_path)

    if settings.transcribe_provider == "whisper":
        from transcript_bot.whisper_local import transcribe_with_local_whisper

        transcript_segments = transcribe_with_local_whisper(audio_path, progress_callback=progress_callback)
        if settings.enable_pyannote_diarization:
            from transcript_bot.pyannote_diarization import apply_pyannote_speakers, diarize_with_pyannote

            return apply_pyannote_speakers(transcript_segments, diarize_with_pyannote(audio_path))
        return transcript_segments

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
