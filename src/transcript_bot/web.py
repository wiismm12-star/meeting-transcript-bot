from __future__ import annotations

from pathlib import Path

from flask import Flask, abort, redirect, render_template, request, send_file, url_for

from transcript_bot.config import settings
from transcript_bot.database import (
    get_local_meeting_export,
    get_local_meeting_audio_path,
    get_meeting_segments,
    init_database,
    list_local_meeting_exports,
    update_meeting_transcript_text,
)
from transcript_bot.storage import ensure_data_dirs


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
        return render_template("index.html", meetings=meetings)

    @app.route("/meetings/<meeting_id>", methods=["GET", "POST"])
    def edit_meeting(meeting_id: str):
        meeting = get_local_meeting_export(app.config["DATA_DIR"], meeting_id)
        if not meeting:
            abort(404)

        if request.method == "POST":
            transcript_text = request.form.get("transcript_text", "").strip()
            if not transcript_text:
                return render_template(
                    "edit_meeting.html",
                    meeting=meeting,
                    error="逐字稿不可留白。",
                ), 400
            update_meeting_transcript_text(app.config["DATA_DIR"], meeting.id, transcript_text)
            return redirect(url_for("edit_meeting", meeting_id=meeting.id, saved="1"))

        return render_template(
            "edit_meeting.html",
            meeting=meeting,
            segments=get_meeting_segments(app.config["DATA_DIR"], meeting.id, meeting.user_id),
            audio_available=get_local_meeting_audio_path(app.config["DATA_DIR"], meeting.id) is not None,
            saved=request.args.get("saved") == "1",
        )

    @app.get("/meetings/<meeting_id>/audio")
    def meeting_audio(meeting_id: str):
        audio_path = get_local_meeting_audio_path(app.config["DATA_DIR"], meeting_id)
        if not audio_path:
            abort(404)
        return send_file(audio_path, conditional=True)

    return app


def main() -> None:
    app = create_web_app()
    # Loopback binding is deliberate: this MVP must not be exposed to a network.
    app.run(host="127.0.0.1", port=8765, debug=False)


if __name__ == "__main__":
    main()
