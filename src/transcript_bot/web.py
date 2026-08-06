from __future__ import annotations

import json
import collections
import threading
from pathlib import Path
import shutil

from flask import Flask, abort, jsonify, redirect, render_template, request, send_file, url_for
from werkzeug.utils import secure_filename

from transcript_bot.audio import AudioProcessingError, get_audio_duration, normalize_audio
from transcript_bot.config import settings
from transcript_bot.database import (
    MeetingRecord,
    create_meeting,
    delete_meeting,
    get_local_meeting_export,
    get_local_meeting_audio_path,
    get_meeting_segments,
    get_meeting_speaker_labels,
    get_speaker_aliases,
    init_database,
    list_local_meeting_exports,
    replace_speaker_aliases,
    save_transcript_segments,
    update_meeting_metadata,
    update_meeting_summary_text,
    update_transcript_segment_text,
    update_meeting_transcript_text,
)
from transcript_bot.formatting import (
    apply_speaker_aliases,
    normalize_speaker_labels,
    polish_local_transcript,
    polish_transcript,
    render_action_summary,
    MeetingSummary,
    build_fallback_meeting_summary,
    render_plain_transcript,
)
from transcript_bot.exporters import write_docx, write_text
from transcript_bot.storage import create_job_paths, ensure_data_dirs
from transcript_bot.transcription import transcribe_audio_smart
from transcript_bot.ollama_client import OllamaError, summarize_meeting_with_ollama
from transcript_bot.line_bot import LineBotError, acknowledgement_for_event, reply_to_line, verify_webhook_signature

# Module-level job tracking for async transcription
_active_jobs: dict[str, threading.Thread] = {}
_cancel_flags: dict[str, threading.Event] = {}
_job_progress: dict[str, dict] = {}  # {job_id: {"step": str, "pct": int, "queued": bool, "position": int}}
_queued_payloads: dict[str, tuple] = {}  # {job_id: (data_dir, paths, original_filename, cancel_event)}
_job_queue: "collections.deque[str]" = collections.deque()
_job_lock = threading.Lock()
_running_count = 0  # number of currently running transcription threads


def create_web_app(data_dir: Path | None = None) -> Flask:
    """Create a correction interface intended exclusively for this computer."""
    active_data_dir = data_dir or settings.data_dir
    ensure_data_dirs(active_data_dir)
    init_database(active_data_dir)

    # Clean up orphaned meetings from killed processes (empty transcript, no active thread)
    _purge_orphaned_meetings(active_data_dir)

    app = Flask(__name__)
    app.config["DATA_DIR"] = active_data_dir
    app.jinja_env.auto_reload = True  # always reload templates in dev
    app.add_template_filter(lambda p: Path(p).stem if p else "", "basename")

    @app.after_request
    def _no_cache(response):
        response.headers["Cache-Control"] = "no-store, must-revalidate"
        return response

    @app.get("/")
    def index():
        meetings = list_local_meeting_exports(app.config["DATA_DIR"])
        active_meetings = [m for m in meetings if not m.transcript_text]
        done_meetings = [m for m in meetings if m.transcript_text]
        return render_template(
            "index.html",
            active_meetings=active_meetings,
            done_meetings=done_meetings,
            upload_error=request.args.get("error"),
            deleted_count=request.args.get("deleted", type=int),
            cancelled=request.args.get("cancelled") == "1",
            active_jobs=_active_jobs,
            job_progress=_job_progress,
        )

    @app.post("/upload")
    def upload_audio():
        uploaded_file = request.files.get("audio_file")
        if not uploaded_file or not uploaded_file.filename:
            return redirect(url_for("index", error="請選擇一個音檔。"))

        original_filename = uploaded_file.filename
        filename = secure_filename(original_filename)
        suffix = Path(original_filename).suffix.lower()
        if suffix not in {".m4a", ".mp3", ".wav", ".ogg", ".webm", ".mp4", ".aac"}:
            return redirect(url_for("index", error="請上傳 m4a、mp3、wav、ogg、webm、mp4 或 aac 音檔。"))

        paths = create_job_paths(app.config["DATA_DIR"], suffix)
        try:
            uploaded_file.save(paths.input_audio)

            create_meeting(
                app.config["DATA_DIR"],
                MeetingRecord(
                    id=paths.job_id,
                    user_id=0,
                    source_platform="local_web",
                    audio_file_path=str(paths.input_audio),
                    normalized_audio_path=str(paths.normalized_audio),
                    transcript_txt_path=str(paths.transcript_txt),
                    transcript_docx_path=str(paths.transcript_docx),
                ),
            )

            # Set title immediately so the processing card shows the original filename
            update_meeting_metadata(app.config["DATA_DIR"], paths.job_id, Path(original_filename).stem[:160], "")

            # Start async background transcription (respects MAX_CONCURRENT_JOBS)
            cancel_event = threading.Event()
            _cancel_flags[paths.job_id] = cancel_event
            _enqueue_or_start(
                app.config["DATA_DIR"], paths.job_id, paths, original_filename, cancel_event
            )

        except OSError as exc:
            return redirect(url_for("index", error=f"儲存音檔失敗：{exc}"))

        return redirect(url_for("index", uploaded=paths.job_id))

    @app.post("/line/webhook")
    def line_webhook():
        """A small, signed LINE ingress used for safe connectivity testing first."""
        if not settings.enable_line_bot:
            abort(404)
        raw_body = request.get_data(cache=False)
        signature = request.headers.get("X-Line-Signature", "")
        if not verify_webhook_signature(raw_body, signature, settings.line_channel_secret):
            return "invalid signature", 400
        try:
            payload = json.loads(raw_body)
        except json.JSONDecodeError:
            return "invalid payload", 400

        for event in payload.get("events", []):
            reply_token = str(event.get("replyToken") or "")
            acknowledgement = acknowledgement_for_event(event)
            if reply_token and acknowledgement:
                try:
                    reply_to_line(reply_token, settings.line_channel_access_token, acknowledgement)
                except LineBotError:
                    # LINE retries webhooks. Return success after signature verification to avoid duplicate retries.
                    pass
        return "OK", 200

    @app.route("/meetings/<meeting_id>", methods=["GET", "POST"])
    def edit_meeting(meeting_id: str):
        meeting = get_local_meeting_export(app.config["DATA_DIR"], meeting_id)
        if not meeting:
            abort(404)

        def render_editor(*, error: str | None = None):
            aliases = get_speaker_aliases(app.config["DATA_DIR"], meeting.id, meeting.user_id)
            display_transcript_text = apply_speaker_aliases(meeting.transcript_text, aliases)
            active_tab = request.args.get("tab", "transcript")
            active_tab = active_tab if active_tab in {"transcript", "summary", "actions", "notes"} else "transcript"
            summary = _deserialize_summary(meeting.summary_text)
            if active_tab == "summary" and summary is None:
                summary = _generate_meeting_summary(display_transcript_text, meeting.title)
                update_meeting_summary_text(app.config["DATA_DIR"], meeting.id, _serialize_summary(summary))
            return render_template(
                "edit_meeting.html",
                meeting=meeting,
                sidebar_meetings=list_local_meeting_exports(app.config["DATA_DIR"]),
                segments=get_meeting_segments(app.config["DATA_DIR"], meeting.id, meeting.user_id),
                speaker_labels=get_meeting_speaker_labels(app.config["DATA_DIR"], meeting.id),
                aliases=aliases,
                display_transcript_text=display_transcript_text,
                summary=summary,
                action_text=render_action_summary(display_transcript_text),
                active_tab=active_tab,
                audio_available=get_local_meeting_audio_path(app.config["DATA_DIR"], meeting.id) is not None,
                saved=request.args.get("saved") == "1",
                aliases_saved=request.args.get("aliases_saved") == "1",
                metadata_saved=request.args.get("metadata_saved") == "1",
                notes_saved=request.args.get("notes_saved") == "1",
                error=error,
            )

        if request.method == "POST":
            if request.form.get("form_action") == "summary":
                aliases = get_speaker_aliases(app.config["DATA_DIR"], meeting.id, meeting.user_id)
                transcript_text = apply_speaker_aliases(meeting.transcript_text, aliases)
                summary = _generate_meeting_summary(transcript_text, meeting.title)
                update_meeting_summary_text(app.config["DATA_DIR"], meeting.id, _serialize_summary(summary))
                return redirect(url_for("edit_meeting", meeting_id=meeting.id, tab="summary"))

            if request.form.get("form_action") == "segment":
                try:
                    sequence = int(request.form.get("sequence", ""))
                except ValueError:
                    return jsonify({"error": "找不到逐字稿段落。"}), 400
                text = request.form.get("text", "").strip()
                if not text or not update_transcript_segment_text(
                    app.config["DATA_DIR"], meeting.id, meeting.user_id, sequence, text
                ):
                    return jsonify({"error": "逐字稿段落不可留白或已不存在。"}), 400
                segments = get_meeting_segments(app.config["DATA_DIR"], meeting.id, meeting.user_id)
                transcript_text = "\n\n".join(
                    f"{segment.speaker}：{segment.text}" for segment in segments if segment.text.strip()
                )
                update_meeting_transcript_text(app.config["DATA_DIR"], meeting.id, transcript_text)
                return jsonify({"ok": True})

            if request.form.get("form_action") == "metadata":
                title = request.form.get("title", "").strip()[:160]
                update_meeting_metadata(app.config["DATA_DIR"], meeting.id, title, meeting.notes)
                return redirect(url_for("edit_meeting", meeting_id=meeting.id, metadata_saved="1"))

            if request.form.get("form_action") == "notes":
                notes = request.form.get("notes", "").strip()
                update_meeting_metadata(app.config["DATA_DIR"], meeting.id, meeting.title, notes)
                return redirect(url_for("edit_meeting", meeting_id=meeting.id, tab="notes", notes_saved="1"))

            if request.form.get("form_action") == "aliases":
                labels = get_meeting_speaker_labels(app.config["DATA_DIR"], meeting.id)
                aliases = {
                    label: request.form.get(f"alias_{label}", "").strip()
                    for label in labels
                    if request.form.get(f"alias_{label}", "").strip()
                }
                replace_speaker_aliases(app.config["DATA_DIR"], meeting.id, meeting.user_id, aliases)
                if request.form.get("ajax") == "1":
                    return jsonify({"aliases": aliases})
                return redirect(url_for("edit_meeting", meeting_id=meeting.id, aliases_saved="1"))

            aliases = get_speaker_aliases(app.config["DATA_DIR"], meeting.id, meeting.user_id)
            transcript_text = _restore_speaker_labels(request.form.get("transcript_text", "").strip(), aliases)
            if not transcript_text:
                return render_editor(error="逐字稿不可留白。"), 400
            update_meeting_transcript_text(app.config["DATA_DIR"], meeting.id, transcript_text)
            return redirect(url_for("edit_meeting", meeting_id=meeting.id, saved="1"))

        return render_editor()

    @app.get("/meetings/<meeting_id>/audio")
    def meeting_audio(meeting_id: str):
        audio_path = get_local_meeting_audio_path(app.config["DATA_DIR"], meeting_id)
        if not audio_path:
            abort(404)
        return send_file(audio_path.resolve(), conditional=True)

    @app.post("/meetings/<meeting_id>/delete")
    def delete_local_meeting(meeting_id: str):
        meeting = get_local_meeting_export(app.config["DATA_DIR"], meeting_id)
        if not meeting:
            abort(404)

        # Signal cancel if job is currently running
        cancel_flag = _cancel_flags.get(meeting_id)
        if cancel_flag:
            cancel_flag.set()

        # Drop the job if it is still queued (not yet started) so it never launches
        if meeting_id in _queued_payloads:
            with _job_lock:
                if meeting_id in _queued_payloads:
                    _queued_payloads.pop(meeting_id, None)
                    try:
                        _job_queue.remove(meeting_id)
                    except ValueError:
                        pass
                    _job_progress.pop(meeting_id, None)
                    _refresh_queue_positions()

        _delete_local_meeting_files(app.config["DATA_DIR"], meeting.id)
        if not delete_meeting(app.config["DATA_DIR"], meeting.id, meeting.user_id):
            abort(500)
        if request.form.get("cancelled") == "1":
            return redirect(url_for("index", cancelled="1"))
        return redirect(url_for("index"))

    @app.post("/meetings/delete-selected")
    def delete_selected_local_meetings():
        """Delete only explicitly selected local meetings and their job directories."""
        meeting_ids = list(dict.fromkeys(item.strip() for item in request.form.getlist("meeting_ids") if item.strip()))
        deleted_count = 0
        for meeting_id in meeting_ids:
            meeting = get_local_meeting_export(app.config["DATA_DIR"], meeting_id)
            if not meeting:
                continue
            _delete_local_meeting_files(app.config["DATA_DIR"], meeting.id)
            if delete_meeting(app.config["DATA_DIR"], meeting.id, meeting.user_id):
                deleted_count += 1
        return redirect(url_for("index", deleted=deleted_count))

    @app.get("/meetings/<meeting_id>/download/<file_type>")
    def download_transcript(meeting_id: str, file_type: str):
        meeting = get_local_meeting_export(app.config["DATA_DIR"], meeting_id)
        if not meeting or file_type not in {"txt", "docx"}:
            abort(404)

        aliases = get_speaker_aliases(app.config["DATA_DIR"], meeting.id, meeting.user_id)
        transcript_text = apply_speaker_aliases(meeting.transcript_text, aliases)
        speaker_names = [
            aliases.get(label, label)
            for label in get_meeting_speaker_labels(app.config["DATA_DIR"], meeting.id)
        ]
        if file_type == "txt":
            export_path = Path(meeting.transcript_txt_path).resolve()
            write_text(export_path, transcript_text)
        else:
            export_path = Path(meeting.transcript_docx_path).resolve()
            write_docx(
                export_path,
                "會議逐字稿",
                transcript_text,
                meeting_id=meeting.id,
                meeting_date=meeting.created_at,
                speakers=speaker_names,
            )
        return send_file(export_path, as_attachment=True, download_name=export_path.name)

    @app.get("/api/jobs/<job_id>/progress")
    def job_progress(job_id: str):
        """Return transcription progress as JSON for JS polling."""
        prog = _job_progress.get(job_id)
        if not prog:
            return jsonify({"active": False})
        return jsonify(
            {
                "active": True,
                "queued": prog.get("queued", False),
                "position": prog.get("position"),
                "step": prog["step"],
                "pct": prog["pct"],
                "label": prog.get("label", prog["step"]),
            }
        )

    return app


def _enqueue_or_start(data_dir: str, job_id: str, paths, original_filename: str, cancel_event: threading.Event) -> None:
    """Launch the transcription thread immediately if a slot is free, otherwise queue it."""
    global _running_count
    with _job_lock:
        if _running_count < settings.max_concurrent_jobs:
            _running_count += 1
            _start_thread(data_dir, job_id, paths, original_filename, cancel_event)
        else:
            _job_queue.append(job_id)
            _queued_payloads[job_id] = (data_dir, job_id, paths, original_filename, cancel_event)
            _job_progress[job_id] = {
                "step": "queued",
                "pct": 0,
                "label": f"queued (排隊中 · 第 {len(_job_queue)} 位)",
                "queued": True,
                "position": len(_job_queue),
            }


def _start_thread(data_dir: str, job_id: str, paths, original_filename: str, cancel_event: threading.Event) -> None:
    thread = threading.Thread(
        target=_process_job,
        args=(data_dir, paths, original_filename, cancel_event),
        daemon=True,
    )
    _active_jobs[job_id] = thread
    thread.start()


def _refresh_queue_positions() -> None:
    for idx, queued_id in enumerate(_job_queue, start=1):
        prog = _job_progress.get(queued_id)
        if prog is not None:
            prog["position"] = idx
            prog["label"] = f"queued (排隊中 · 第 {idx} 位)"


def _on_job_complete(data_dir: str, _finished_id: str) -> None:
    """Release this job's slot and start the next queued job, if any."""
    global _running_count
    pending = None
    with _job_lock:
        _running_count -= 1
        if _job_queue:
            queued_id = _job_queue.popleft()
            pending = _queued_payloads.pop(queued_id, None)
            if pending is not None:
                _running_count += 1
                _refresh_queue_positions()
    if pending is not None:
        _start_thread(*pending)


def _process_job(data_dir: str, paths, original_filename: str, cancel_event: threading.Event) -> None:
    """Background thread with progress tracking: normalize → transcribe → polish → save."""
    job_id = paths.job_id
    _job_progress[job_id] = {"step": "loading", "pct": 0, "label": "loading (初始化)"}

    try:
        # Step 1: Normalize audio (0 → 25%)
        if cancel_event.is_set():
            return
        _job_progress[job_id] = {"step": "normalizing", "pct": 5, "label": "normalizing (音檔標準化)"}
        normalize_audio(paths.input_audio, paths.normalized_audio)
        _job_progress[job_id] = {"step": "normalizing", "pct": 25, "label": "normalizing (音檔標準化)"}

        # Reject only after normalization so long, high-bitrate recordings still
        # get in and are auto-split. The normalized 64kbps ceiling is the real cap.
        if paths.normalized_audio.stat().st_size > settings.max_audio_bytes:
            _job_progress[job_id] = {
                "step": "error",
                "pct": 25,
                "label": f"error (音檔過大：超過 {settings.max_audio_mb} MB 標準化上限)",
            }
            return

        # Get audio duration for percentage calculation
        duration = get_audio_duration(paths.normalized_audio)

        # Step 2: Transcribe with diarization (25 → 90%)
        if cancel_event.is_set():
            return

        def _on_transcribe_progress(last_segment_time: float, _unused: float) -> None:
            nonlocal duration
            if last_segment_time < 0:  # signal: transcription complete
                _job_progress[job_id] = {"step": "transcribing", "pct": 90, "label": "transcribing (語音辨識)"}
                return
            # Chunk-based progress: direct pct in 20 → 90 range.
            if _unused == -999.0:
                pct = min(90, max(25, int(last_segment_time)))
                existing_pct = _job_progress.get(job_id, {}).get("pct", 0)
                if existing_pct >= pct:
                    return
                _job_progress[job_id] = {**_job_progress.get(job_id, {}),
                                          "step": "transcribing", "pct": pct}
                return
            # Time-based progress (single-chunk path).
            if duration > 0:
                pct = min(90, 20 + int((last_segment_time / duration) * 70))
                existing_pct = _job_progress.get(job_id, {}).get("pct", 0)
                if existing_pct >= pct:
                    return
                _job_progress[job_id] = {**_job_progress.get(job_id, {}),
                                          "step": "transcribing", "pct": pct}

        _job_progress[job_id] = {"step": "transcribing", "pct": 20, "label": "transcribing (語音辨識)"}

        def _chunk_label(completed: int, total: int) -> None:
            prog = _job_progress.get(job_id) or {}
            prog["label"] = f"transcribing (分段語音辨識 {completed}/{total})"
            _job_progress[job_id] = prog

        def _stage_label(label: str) -> None:
            _job_progress[job_id] = {**_job_progress.get(job_id, {}),
                                      "step": "transcribing", "label": label}

        segments = normalize_speaker_labels(
            transcribe_audio_smart(
                paths.normalized_audio,
                progress_callback=_on_transcribe_progress,
                chunk_label_callback=_chunk_label,
                stage_callback=_stage_label,
            )
        )

        # Step 3: Polish & save (90 → 100%)
        if cancel_event.is_set():
            return
        _job_progress[job_id] = {"step": "polishing", "pct": 92, "label": "polishing (潤稿整理)"}
        save_transcript_segments(data_dir, job_id, segments)
        raw_text = render_plain_transcript(segments)
        transcript_text = polish_transcript(raw_text) if settings.enable_polish else polish_local_transcript(raw_text)
        _job_progress[job_id] = {"step": "saving", "pct": 97, "label": "saving (儲存中)"}
        update_meeting_transcript_text(data_dir, job_id, transcript_text)
        # Sync polished text back to individual segments so the editor shows punctuation.
        _sync_polished_segments(data_dir, job_id, transcript_text, segments)
        update_meeting_metadata(data_dir, job_id, Path(original_filename).stem[:160], "")
        summary = _generate_meeting_summary(transcript_text, Path(original_filename).stem[:160])
        update_meeting_summary_text(data_dir, job_id, _serialize_summary(summary))
        _job_progress[job_id] = {"step": "done", "pct": 100, "label": "done (完成)"}

    except Exception:
        import logging
        logging.getLogger("transcript_bot.web").exception("轉錄失敗 job=%s", job_id)
        _job_progress[job_id] = {"step": "error", "pct": 0, "label": "error (轉錄失敗，請重試)"}
    finally:
        _active_jobs.pop(job_id, None)
        _cancel_flags.pop(job_id, None)
        _job_progress.pop(job_id, None)
        _on_job_complete(data_dir, job_id)


def _restore_speaker_labels(text: str, aliases: dict[str, str]) -> str:
    """Convert displayed aliases back to stored labels when saving editor text."""
    for original_label, display_name in aliases.items():
        text = text.replace(f"{display_name}：", f"{original_label}：")
        text = text.replace(f"{display_name}:", f"{original_label}：")
    return text


def _sync_polished_segments(data_dir: str, meeting_id: str, transcript_text: str, segments: list) -> None:
    """Parse polished transcript_text back into per-speaker blocks and update DB segments."""
    import re as _re
    blocks = _re.split(r"\n\n+", transcript_text)
    for block in blocks:
        match = _re.match(r"^(Speaker\s+\d+)[：:]\s*(.+)", block.strip(), _re.IGNORECASE | _re.DOTALL)
        if not match:
            continue
        speaker_label = match.group(1)
        polished_text = match.group(2).strip()
        for i, seg in enumerate(segments, start=1):
            if seg.speaker == speaker_label:
                update_transcript_segment_text(data_dir, meeting_id, 0, i, polished_text)
                break


def _delete_local_meeting_files(data_dir: Path, meeting_id: str) -> None:
    """Remove just the verified per-meeting job directory, never the whole data directory."""
    audio_path = get_local_meeting_audio_path(data_dir, meeting_id)
    if not audio_path:
        return
    jobs_dir = (data_dir / "jobs").resolve()
    job_dir = audio_path.parent.resolve()
    if job_dir.parent == jobs_dir and job_dir.exists():
        shutil.rmtree(job_dir)


def _purge_orphaned_meetings(data_dir: str) -> None:
    """Delete meetings with empty transcript that have no active background thread (leftover from killed server)."""
    meetings = list_local_meeting_exports(data_dir)
    for meeting in meetings:
        if not meeting.transcript_text and meeting.id not in _active_jobs:
            _delete_local_meeting_files(data_dir, meeting.id)
            delete_meeting(data_dir, meeting.id, meeting.user_id)


def _generate_meeting_summary(transcript: str, meeting_title: str) -> MeetingSummary:
    """Use the local model when available, with a no-network readable fallback."""
    try:
        payload = summarize_meeting_with_ollama(transcript, meeting_title)
        return MeetingSummary(
            title=str(payload["title"]),
            overview=str(payload["overview"]),
            highlights=[str(item) for item in payload["highlights"]],
        )
    except OllamaError:
        return build_fallback_meeting_summary(transcript, meeting_title)


def _serialize_summary(summary: MeetingSummary) -> str:
    return json.dumps(
        {"title": summary.title, "overview": summary.overview, "highlights": summary.highlights},
        ensure_ascii=False,
    )


def _deserialize_summary(value: str) -> MeetingSummary | None:
    if not value.strip():
        return None
    try:
        payload = json.loads(value)
        title = str(payload["title"]).strip()
        overview = str(payload["overview"]).strip()
        highlights = [str(item).strip() for item in payload["highlights"] if str(item).strip()]
    except (TypeError, ValueError, KeyError):
        return None
    if not title or not overview or not highlights:
        return None
    return MeetingSummary(title, overview, highlights)


def main() -> None:
    # Register bundled NVIDIA CUDA DLLs so ctranslate2/pyannote can find them on GPU.
    from transcript_bot.cuda_dlls import register_nvidia_dlls

    register_nvidia_dlls()
    app = create_web_app()
    app.jinja_env.auto_reload = True  # reload templates on every request in dev
    # Loopback binding is deliberate: this MVP must not be exposed to a network.
    app.run(host="127.0.0.1", port=8765, debug=False)


def _register_nvidia_dlls() -> None:  # pragma: no cover - thin alias
    """Backwards-compatible alias. Real logic lives in ``transcript_bot.cuda_dlls``."""
    from transcript_bot.cuda_dlls import register_nvidia_dlls

    register_nvidia_dlls()


if __name__ == "__main__":
    main()
