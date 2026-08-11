from __future__ import annotations

import json
import collections
import hashlib
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
    find_matching_local_meeting,
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
    update_transcript_segment_speaker,
    update_meeting_transcript_text,
)
from transcript_bot.formatting import (
    apply_speaker_aliases,
    normalize_speaker_labels,
    polish_local_transcript,
    polish_transcript,
    MeetingSummary,
    build_fallback_meeting_summary,
    render_plain_transcript,
    split_segments_by_sentences,
)
from transcript_bot.exporters import write_docx, write_text
from transcript_bot.storage import create_job_paths, ensure_data_dirs
from transcript_bot.transcription import transcribe_audio_smart
from transcript_bot.ollama_client import OllamaError, summarize_meeting_with_ollama
from transcript_bot.line_bot import (
    LineBotError,
    acknowledgement_for_event,
    download_line_message_content,
    reply_to_line,
    verify_webhook_signature,
)
from transcript_bot.job_status import get_job_status, is_job_active, list_active_job_statuses, request_cancel

# Module-level job tracking for async transcription
_active_jobs: dict[str, threading.Thread] = {}
_cancel_flags: dict[str, threading.Event] = {}
_job_progress: dict[str, dict] = {}  # {job_id: {"step": str, "pct": int, "queued": bool, "position": int}}
_queued_payloads: dict[str, tuple] = {}  # {job_id: (data_dir, paths, original_filename, cancel_event)}
_job_queue: "collections.deque[str]" = collections.deque()
_job_lock = threading.Lock()
_running_count = 0  # number of currently running transcription threads

_TELEGRAM_STATUS_LABELS = {
    "running": "運行中",
    "starting": "啟動中",
    "disabled": "未啟用",
    "error": "錯誤",
    "unknown": "狀態未知",
}


def get_telegram_bot_status(data_dir: Path) -> dict[str, str]:
    """Read the watchdog's credential-free Telegram worker status file."""
    fallback = {"state": "unknown", "label": _TELEGRAM_STATUS_LABELS["unknown"], "message": "尚未收到 watchdog 狀態。", "updated_at": ""}
    try:
        raw = json.loads((data_dir / "telegram_bot_status.json").read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return fallback
    state = str(raw.get("state", "unknown"))
    if state not in _TELEGRAM_STATUS_LABELS:
        state = "unknown"
    return {
        "state": state,
        "label": _TELEGRAM_STATUS_LABELS[state],
        "message": str(raw.get("message", ""))[:160],
        "updated_at": str(raw.get("updated_at", ""))[:64],
    }


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
        telegram_jobs = list_active_job_statuses(app.config["DATA_DIR"])
        visible_jobs = {**_job_progress, **telegram_jobs}
        return render_template(
            "index.html",
            active_meetings=active_meetings,
            done_meetings=done_meetings,
            upload_error=request.args.get("error"),
            deleted_count=request.args.get("deleted", type=int),
            cancelled=request.args.get("cancelled") == "1",
            active_jobs={**_active_jobs, **telegram_jobs},
            job_progress=visible_jobs,
            telegram_status=get_telegram_bot_status(app.config["DATA_DIR"]),
        )

    @app.get("/api/telegram/status")
    def telegram_status():
        return jsonify(get_telegram_bot_status(app.config["DATA_DIR"]))

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
        """Accept signed LINE events and hand supported media to the local job queue."""
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
            message = event.get("message") or {}
            if message.get("type") in {"audio", "file", "video"} and message.get("id"):
                _start_line_media_download(
                    app.config["DATA_DIR"],
                    event,
                    settings.line_channel_access_token,
                )
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
        if _repair_overlong_segments(app.config["DATA_DIR"], meeting):
            # Refresh the cached record so the page and downloads use the same
            # repaired, bounded rows.
            meeting = get_local_meeting_export(app.config["DATA_DIR"], meeting_id)
            if not meeting:
                abort(404)

        def render_editor(*, error: str | None = None):
            aliases = get_speaker_aliases(app.config["DATA_DIR"], meeting.id, meeting.user_id)
            display_transcript_text = apply_speaker_aliases(meeting.transcript_text, aliases)
            active_tab = request.args.get("tab", "transcript")
            active_tab = active_tab if active_tab in {"transcript", "summary", "notes"} else "transcript"
            summary = _deserialize_summary(meeting.summary_text)
            labels = get_meeting_speaker_labels(app.config["DATA_DIR"], meeting.id)
            # Merge alias-only speakers (added via + button, have no segments yet)
            for alias_key in aliases:
                if alias_key not in labels:
                    labels.append(alias_key)
            # Stable sort by speaker number
            import re as _re
            labels.sort(key=lambda s: int(_re.search(r'\d+', s).group()) if _re.search(r'\d+', s) else 999)
            return render_template(
                "edit_meeting.html",
                meeting=meeting,
                sidebar_meetings=list_local_meeting_exports(app.config["DATA_DIR"]),
                segments=get_meeting_segments(app.config["DATA_DIR"], meeting.id, meeting.user_id),
                speaker_labels=labels,
                aliases=aliases,
                display_transcript_text=display_transcript_text,
                summary=summary,
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
                update_meeting_transcript_text(app.config["DATA_DIR"], meeting.id, transcript_text, preserve_summary=True)
                return jsonify({"ok": True})

            if request.form.get("form_action") == "speaker":
                try:
                    sequence = int(request.form.get("sequence", ""))
                except ValueError:
                    return jsonify({"error": "找不到逐字稿段落。"}), 400
                new_label = request.form.get("speaker_label", "").strip()
                if not new_label:
                    return jsonify({"error": "請指定主講人。"}), 400
                if not update_transcript_segment_speaker(
                    app.config["DATA_DIR"], meeting.id, meeting.user_id, sequence, new_label
                ):
                    return jsonify({"error": "逐字稿段落已不存在。"}), 400
                # Rebuild transcript_text after speaker change
                segments = get_meeting_segments(app.config["DATA_DIR"], meeting.id, meeting.user_id)
                transcript_text = "\n\n".join(
                    f"{segment.speaker}：{segment.text}" for segment in segments if segment.text.strip()
                )
                update_meeting_transcript_text(app.config["DATA_DIR"], meeting.id, transcript_text, preserve_summary=True)
                _aliases = get_speaker_aliases(app.config["DATA_DIR"], meeting.id, meeting.user_id)
                return jsonify({
                    "ok": True,
                    "speaker_label": new_label,
                    "display_name": _aliases.get(new_label, new_label),
                })

            if request.form.get("form_action") == "metadata":
                title = request.form.get("title", "").strip()[:160]
                update_meeting_metadata(app.config["DATA_DIR"], meeting.id, title, meeting.notes)
                return redirect(url_for("edit_meeting", meeting_id=meeting.id, metadata_saved="1"))

            if request.form.get("form_action") == "notes":
                notes = request.form.get("notes", "").strip()
                update_meeting_metadata(app.config["DATA_DIR"], meeting.id, meeting.title, notes)
                return redirect(url_for("edit_meeting", meeting_id=meeting.id, tab="notes", notes_saved="1"))

            if request.form.get("form_action") == "aliases":
                labels = set(get_meeting_speaker_labels(app.config["DATA_DIR"], meeting.id))
                # Also accept labels from form that aren't in DB yet (e.g. newly added via +)
                for key in request.form:
                    if key.startswith("alias_"):
                        labels.add(key[6:])
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

        external_status = get_job_status(app.config["DATA_DIR"], meeting_id)
        if is_job_active(external_status):
            request_cancel(app.config["DATA_DIR"], meeting_id)
            return redirect(url_for("index", cancelling="1"))

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
        prog = _job_progress.get(job_id) or get_job_status(app.config["DATA_DIR"], job_id)
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

    @app.get("/api/jobs/active")
    def active_jobs():
        """Expose active job IDs so an idle home page notices new Telegram work."""
        telegram_jobs = list_active_job_statuses(app.config["DATA_DIR"])
        return jsonify({"jobs": sorted(set(_job_progress) | set(telegram_jobs))})

    return app


def _line_user_id(event: dict) -> int:
    """Return a stable, non-reversible SQLite-safe ID for a LINE user."""
    user_id = str((event.get("source") or {}).get("userId") or "")
    if not user_id:
        return 0
    digest = hashlib.sha256(user_id.encode("utf-8")).digest()
    return int.from_bytes(digest[:8], "big") & ((1 << 63) - 1)


def _line_media_name(event: dict) -> str:
    """Choose an ffmpeg-friendly name without trusting a remote filename path."""
    message = event.get("message") or {}
    message_type = message.get("type")
    if message_type == "video":
        return "LINE 影片.mp4"
    if message_type == "file":
        supplied = secure_filename(str(message.get("fileName") or ""))
        suffix = Path(supplied).suffix.lower()
        if suffix in {".m4a", ".mp3", ".wav", ".ogg", ".webm", ".mp4", ".aac"}:
            return supplied
    return "LINE 錄音.m4a"


def _start_line_media_download(data_dir: Path, event: dict, access_token: str) -> str:
    """Create a LINE meeting now, then download its private content off the webhook thread."""
    message = event.get("message") or {}
    message_id = str(message.get("id") or "")
    filename = _line_media_name(event)
    paths = create_job_paths(data_dir, Path(filename).suffix)
    create_meeting(
        data_dir,
        MeetingRecord(
            id=paths.job_id,
            user_id=_line_user_id(event),
            source_platform="line_bot",
            audio_file_path=str(paths.input_audio),
            normalized_audio_path=str(paths.normalized_audio),
            transcript_txt_path=str(paths.transcript_txt),
            transcript_docx_path=str(paths.transcript_docx),
        ),
    )
    update_meeting_metadata(data_dir, paths.job_id, Path(filename).stem[:160], "")
    cancel_event = threading.Event()
    _cancel_flags[paths.job_id] = cancel_event
    _job_progress[paths.job_id] = {"step": "downloading", "pct": 0, "label": "downloading (從 LINE 下載音檔)"}
    thread = threading.Thread(
        target=_download_line_media_and_enqueue,
        args=(data_dir, paths, filename, message_id, access_token, cancel_event),
        daemon=True,
    )
    _active_jobs[paths.job_id] = thread
    thread.start()
    return paths.job_id


def _download_line_media_and_enqueue(
    data_dir: Path,
    paths,
    filename: str,
    message_id: str,
    access_token: str,
    cancel_event: threading.Event,
) -> None:
    """Download LINE media, then route it through the same bounded worker queue as Web uploads."""
    try:
        content = download_line_message_content(message_id, access_token)
        if cancel_event.is_set():
            return
        paths.input_audio.write_bytes(content)
        if cancel_event.is_set():
            return
        _enqueue_or_start(data_dir, paths.job_id, paths, filename, cancel_event)
    except LineBotError:
        _job_progress[paths.job_id] = {"step": "error", "pct": 0, "label": "error (無法從 LINE 下載音檔)"}
        _active_jobs.pop(paths.job_id, None)
    except OSError:
        _job_progress[paths.job_id] = {"step": "error", "pct": 0, "label": "error (儲存 LINE 音檔失敗)"}
        _active_jobs.pop(paths.job_id, None)


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
        _job_progress[job_id] = {"step": "normalizing", "pct": 20, "label": "normalizing (音檔標準化)"}

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

        # A byte-identical local recording has one already-confirmed timeline.
        # Reuse it rather than exposing a second, potentially different ASR
        # segmentation for the same audio.
        existing_meeting = find_matching_local_meeting(
            Path(data_dir),
            paths.normalized_audio,
            exclude_meeting_id=job_id,
            expected_duration=duration,
        )
        if existing_meeting:
            _job_progress[job_id] = {
                "step": "reusing",
                "pct": 82,
                "label": "reusing (重用相同音檔的既有逐字稿)",
            }
            segments = get_meeting_segments(Path(data_dir), existing_meeting.id, existing_meeting.user_id)
            if segments:
                segments = split_segments_by_sentences(segments)
                save_transcript_segments(data_dir, job_id, segments)
                update_meeting_transcript_text(data_dir, job_id, render_plain_transcript(segments))
                update_meeting_summary_text(data_dir, job_id, existing_meeting.summary_text)
                update_meeting_metadata(data_dir, job_id, Path(original_filename).stem[:160], "")
                _job_progress[job_id] = {"step": "done", "pct": 100, "label": "done (完成)"}
                return

        # Step 2: Transcribe with diarization (25 → 90%)
        if cancel_event.is_set():
            return

        def _on_transcribe_progress(last_segment_time: float, _unused: float) -> None:
            nonlocal duration
            if last_segment_time < 0:  # signal: transcription complete
                _job_progress[job_id] = {"step": "transcribing", "pct": 80, "label": "transcribing (語音辨識)"}
                return
            # Chunk-based progress: direct pct in 20 → 80 range.
            if _unused == -999.0:
                pct = min(80, max(25, int(last_segment_time)))
                existing_pct = _job_progress.get(job_id, {}).get("pct", 0)
                if existing_pct >= pct:
                    return
                _job_progress[job_id] = {**_job_progress.get(job_id, {}),
                                          "step": "transcribing", "pct": pct}
                return
            # Time-based progress (single-chunk path).
            if duration > 0:
                pct = min(80, 20 + int((last_segment_time / duration) * 60))
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

        # Step 3: Polish & save (80 → 88%)
        if cancel_event.is_set():
            return
        _job_progress[job_id] = {"step": "polishing", "pct": 82, "label": "polishing (潤稿整理)"}
        # Split long segments into sentence-level chunks before saving
        segments = split_segments_by_sentences(segments)
        save_transcript_segments(data_dir, job_id, segments)
        raw_text = render_plain_transcript(segments)
        try:
            transcript_text = polish_transcript(raw_text) if settings.enable_polish else polish_local_transcript(raw_text)
        except OllamaError:
            # Ollama unavailable (service down / model not pulled) must NOT scrap
            # the whole job. Degrade to local rule-based cleanup and keep the raw
            # transcript so the meeting still completes and is viewable.
            import logging as _logging
            _logging.getLogger("transcript_bot.web").warning(
                "Ollama 潤稿失敗，降級為本地規則清理並保留原始逐字稿 job=%s", job_id)
            transcript_text = polish_local_transcript(raw_text)
        _job_progress[job_id] = {"step": "saving", "pct": 86, "label": "saving (儲存中)"}
        # Keep the timeline aligned with the polished text.  A polish response
        # must never turn one row into an unbounded paragraph in the editor.
        segments = split_segments_by_sentences(
            _sync_polished_segments(transcript_text, segments)
        )
        save_transcript_segments(data_dir, job_id, segments)
        # Use the same bounded rows for downloads, summaries, and the editor.
        transcript_text = render_plain_transcript(segments)
        update_meeting_transcript_text(data_dir, job_id, transcript_text)
        update_meeting_metadata(data_dir, job_id, Path(original_filename).stem[:160], "")

        # Step 4: Summary (88 → 94%)
        if cancel_event.is_set():
            return
        _job_progress[job_id] = {"step": "summarizing", "pct": 90, "label": "summarizing (會議摘要)"}
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


def _sync_polished_segments(transcript_text: str, segments: list) -> list:
    """Map polished rows to source rows in order, while retaining their timestamps.

    Matching only by speaker used to repeatedly overwrite that speaker's first
    segment.  The editor would then show an occasional huge paragraph followed
    by the untouched short rows.  Each speaker now consumes source rows in the
    order in which they occur.
    """
    import re as _re

    source_positions: dict[str, collections.deque[int]] = {}
    for index, segment in enumerate(segments):
        source_positions.setdefault(segment.speaker.lower(), collections.deque()).append(index)

    synced = list(segments)
    for line in transcript_text.splitlines():
        match = _re.match(r"^(Speaker\s+\d+)[：:]\s*(.+)$", line.strip(), _re.IGNORECASE | _re.DOTALL)
        if not match:
            continue
        speaker_label, polished_text = match.groups()
        positions = source_positions.get(speaker_label.lower())
        if not positions:
            continue
        source_index = positions.popleft()
        source = segments[source_index]
        synced[source_index] = type(source)(
            speaker=source.speaker,
            start=source.start,
            end=source.end,
            text=polished_text.strip(),
        )

    return synced


def _repair_overlong_segments(data_dir: Path, meeting) -> bool:
    """Upgrade legacy or reused rows that predate the bounded-display rule."""
    segments = get_meeting_segments(data_dir, meeting.id, meeting.user_id)
    bounded_segments = split_segments_by_sentences(segments)
    if len(bounded_segments) == len(segments) and all(
        bounded.text == original.text
        and bounded.start == original.start
        and bounded.end == original.end
        for bounded, original in zip(bounded_segments, segments)
    ):
        return False

    save_transcript_segments(data_dir, meeting.id, bounded_segments)
    update_meeting_transcript_text(
        data_dir, meeting.id, render_plain_transcript(bounded_segments), preserve_summary=True
    )
    return True


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
        if (not meeting.transcript_text and meeting.id not in _active_jobs
                and not is_job_active(get_job_status(Path(data_dir), meeting.id))):
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


def _kill_stale_servers(port: int) -> None:
    """Kill other transcript_bot.web processes so this instance owns ``port``.

    Any process running ``transcript_bot.web`` other than ourselves is a stale
    server (whether launched from our ``.venv`` or another Python install such as
    uv's cached cpython) that would otherwise serve stale code or race for the
    port. Kill them all by command-line match, then by port binding, then wait
    until the port is actually free.
    """
    import os as _os
    import subprocess as _sp
    import time as _time

    current_pid = _os.getpid()

    # 1) Kill every transcript_bot.web process except ourselves.
    try:
        result = _sp.run(
            [
                "wmic", "process", "where", "Name like 'python%'",
                "get", "ProcessId,CommandLine", "/format:csv",
            ],
            capture_output=True, text=True, timeout=15, errors="ignore",
        )
        for line in result.stdout.splitlines():
            if not line.strip() or line.startswith("Node,"):
                continue
            parts = line.split(",")
            if len(parts) < 3:
                continue
            # CSV columns: Node, CommandLine, ProcessId
            try:
                cmd_line = parts[1].strip().lower()
                pid = int(parts[2].strip())
            except (ValueError, IndexError):
                continue
            if "transcript_bot.web" not in cmd_line:
                continue
            if pid == current_pid:
                continue
            _sp.run(
                ["taskkill", "/F", "/PID", str(pid)],
                capture_output=True, timeout=5,
            )
    except Exception:
        pass

    # 2) Kill whatever is already bound to port 8765 (if not us).
    try:
        result = _sp.run(
            ["netstat", "-ano"], capture_output=True, text=True, timeout=5, errors="ignore"
        )
        for line in result.stdout.splitlines():
            if f":{port}" in line and "LISTENING" in line:
                try:
                    pid = int(line.strip().split()[-1])
                except ValueError:
                    continue
                if pid == current_pid:
                    continue
                _sp.run(
                    ["taskkill", "/F", "/PID", str(pid)],
                    capture_output=True, timeout=5,
                )
    except Exception:
        pass

    # 3) Wait until port 8765 is actually free before returning.
    for _ in range(10):
        try:
            result = _sp.run(
                ["netstat", "-ano"], capture_output=True, text=True, timeout=5, errors="ignore"
            )
            if not any(f":{port}" in line and "LISTENING" in line
                       and line.strip().split()[-1] != str(current_pid)
                       for line in result.stdout.splitlines()):
                return
        except Exception:
            pass
        _time.sleep(0.5)


def main() -> None:
    _kill_stale_servers(settings.web_port)
    # Register bundled NVIDIA CUDA DLLs so ctranslate2/pyannote can find them on GPU.
    from transcript_bot.cuda_dlls import register_nvidia_dlls

    register_nvidia_dlls()
    app = create_web_app()
    app.jinja_env.auto_reload = True  # reload templates on every request in dev
    # Access control is enforced by the host firewall / reverse proxy, not Flask.
    # use_reloader=False prevents Werkzeug from spawning a child interpreter
    # (which on this machine resolves to uv's cached cpython and steals port 8765).
    app.run(host=settings.web_host, port=settings.web_port, debug=False, use_reloader=False)


def _register_nvidia_dlls() -> None:  # pragma: no cover - thin alias
    """Backwards-compatible alias. Real logic lives in ``transcript_bot.cuda_dlls``."""
    from transcript_bot.cuda_dlls import register_nvidia_dlls

    register_nvidia_dlls()


if __name__ == "__main__":
    main()
