from __future__ import annotations

import json
from pathlib import Path
import shutil

from flask import Flask, abort, jsonify, redirect, render_template, request, send_file, url_for
from werkzeug.utils import secure_filename

from transcript_bot.audio import AudioProcessingError, normalize_audio
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
from transcript_bot.transcription import transcribe_with_diarization
from transcript_bot.ollama_client import OllamaError, summarize_meeting_with_ollama


def create_web_app(data_dir: Path | None = None) -> Flask:
    """Create a correction interface intended exclusively for this computer."""
    active_data_dir = data_dir or settings.data_dir
    ensure_data_dirs(active_data_dir)
    init_database(active_data_dir)

    app = Flask(__name__)
    app.config["DATA_DIR"] = active_data_dir

    @app.get("/")
    def index():
        meetings = list_local_meeting_exports(app.config["DATA_DIR"])
        return render_template(
            "index.html",
            meetings=meetings,
            upload_error=request.args.get("error"),
            deleted_count=request.args.get("deleted", type=int),
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
            if paths.input_audio.stat().st_size > settings.max_audio_bytes:
                paths.input_audio.unlink(missing_ok=True)
                return redirect(url_for("index", error=f"音檔超過 {settings.max_audio_mb} MB 限制。"))

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
            normalize_audio(paths.input_audio, paths.normalized_audio)
            segments = normalize_speaker_labels(transcribe_with_diarization(paths.normalized_audio))
            save_transcript_segments(app.config["DATA_DIR"], paths.job_id, segments)
            raw_text = render_plain_transcript(segments)
            transcript_text = polish_transcript(raw_text) if settings.enable_polish else polish_local_transcript(raw_text)
            update_meeting_transcript_text(app.config["DATA_DIR"], paths.job_id, polish_local_transcript(transcript_text))
            update_meeting_metadata(app.config["DATA_DIR"], paths.job_id, Path(original_filename).stem[:160], "")
            summary = _generate_meeting_summary(transcript_text, Path(original_filename).stem[:160])
            update_meeting_summary_text(app.config["DATA_DIR"], paths.job_id, _serialize_summary(summary))
        except (AudioProcessingError, OSError, RuntimeError) as exc:
            failed_meeting = get_local_meeting_export(app.config["DATA_DIR"], paths.job_id)
            if failed_meeting:
                _delete_local_meeting_files(app.config["DATA_DIR"], paths.job_id)
                delete_meeting(app.config["DATA_DIR"], paths.job_id, failed_meeting.user_id)
            return redirect(url_for("index", error=f"處理音檔失敗：{exc}"))

        return redirect(url_for("edit_meeting", meeting_id=paths.job_id))

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
        _delete_local_meeting_files(app.config["DATA_DIR"], meeting.id)
        if not delete_meeting(app.config["DATA_DIR"], meeting.id, meeting.user_id):
            abort(500)
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

    return app


def _restore_speaker_labels(text: str, aliases: dict[str, str]) -> str:
    """Convert displayed aliases back to stored labels when saving editor text."""
    for original_label, display_name in aliases.items():
        text = text.replace(f"{display_name}：", f"{original_label}：")
        text = text.replace(f"{display_name}:", f"{original_label}：")
    return text


def _delete_local_meeting_files(data_dir: Path, meeting_id: str) -> None:
    """Remove just the verified per-meeting job directory, never the whole data directory."""
    audio_path = get_local_meeting_audio_path(data_dir, meeting_id)
    if not audio_path:
        return
    jobs_dir = (data_dir / "jobs").resolve()
    job_dir = audio_path.parent.resolve()
    if job_dir.parent == jobs_dir and job_dir.exists():
        shutil.rmtree(job_dir)


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
    app = create_web_app()
    # Loopback binding is deliberate: this MVP must not be exposed to a network.
    app.run(host="127.0.0.1", port=8765, debug=False)


if __name__ == "__main__":
    main()
