from __future__ import annotations

import sqlite3
import hashlib
import secrets
import time
from dataclasses import dataclass
from contextlib import contextmanager
from pathlib import Path
from typing import Iterable, Iterator

from transcript_bot.transcription import TranscriptSegment


@dataclass(frozen=True)
class MeetingRecord:
    id: str
    user_id: int
    source_platform: str
    audio_file_path: str
    normalized_audio_path: str
    transcript_txt_path: str
    transcript_docx_path: str


@dataclass(frozen=True)
class MeetingExportRecord:
    id: str
    user_id: int
    created_at: str
    title: str
    notes: str
    transcript_text: str
    summary_text: str
    action_text: str
    transcript_txt_path: str
    transcript_docx_path: str
    audio_file_path: str = ""


@dataclass(frozen=True)
class SpeakerSample:
    speaker_label: str
    text: str


def database_path(data_dir: Path) -> Path:
    return data_dir / "transcript_bot.sqlite3"


def init_database(data_dir: Path) -> None:
    data_dir.mkdir(parents=True, exist_ok=True)
    with _connect(data_dir) as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS meetings (
                id TEXT PRIMARY KEY,
                user_id INTEGER NOT NULL,
                source_platform TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT (datetime('now','localtime')),
                audio_file_path TEXT NOT NULL,
                normalized_audio_path TEXT NOT NULL,
                transcript_txt_path TEXT NOT NULL,
                transcript_docx_path TEXT NOT NULL,
                transcript_text TEXT NOT NULL DEFAULT '',
                summary_text TEXT NOT NULL DEFAULT '',
                title TEXT NOT NULL DEFAULT '',
                notes TEXT NOT NULL DEFAULT ''
            );

            CREATE TABLE IF NOT EXISTS transcript_segments (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                meeting_id TEXT NOT NULL,
                sequence INTEGER NOT NULL,
                speaker_label TEXT NOT NULL,
                start_time REAL,
                end_time REAL,
                text TEXT NOT NULL,
                FOREIGN KEY (meeting_id) REFERENCES meetings(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS speaker_aliases (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                meeting_id TEXT NOT NULL,
                user_id INTEGER NOT NULL,
                original_label TEXT NOT NULL,
                display_name TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT (datetime('now','localtime')),
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(meeting_id, user_id, original_label),
                FOREIGN KEY (meeting_id) REFERENCES meetings(id) ON DELETE CASCADE
            );

            CREATE INDEX IF NOT EXISTS idx_meetings_user_created
                ON meetings(user_id, created_at DESC);

            CREATE INDEX IF NOT EXISTS idx_segments_meeting_sequence
                ON transcript_segments(meeting_id, sequence);

            CREATE INDEX IF NOT EXISTS idx_aliases_meeting_user
                ON speaker_aliases(meeting_id, user_id);

            CREATE TABLE IF NOT EXISTS web_identities (
                user_id INTEGER PRIMARY KEY,
                provider TEXT NOT NULL,
                subject TEXT NOT NULL,
                email TEXT NOT NULL,
                display_name TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(provider, subject)
            );

            CREATE TABLE IF NOT EXISTS meeting_claims (
                token_hash TEXT PRIMARY KEY,
                meeting_id TEXT NOT NULL UNIQUE,
                expires_at INTEGER NOT NULL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (meeting_id) REFERENCES meetings(id) ON DELETE CASCADE
            );
            """
        )
        _ensure_column(conn, "meetings", "transcript_text", "TEXT NOT NULL DEFAULT ''")
        _ensure_column(conn, "meetings", "summary_text", "TEXT NOT NULL DEFAULT ''")
        _ensure_column(conn, "meetings", "title", "TEXT NOT NULL DEFAULT ''")
        _ensure_column(conn, "meetings", "notes", "TEXT NOT NULL DEFAULT ''")
        _ensure_column(conn, "meetings", "action_text", "TEXT NOT NULL DEFAULT ''")


def create_meeting(data_dir: Path, meeting: MeetingRecord) -> None:
    from datetime import datetime

    with _connect(data_dir) as conn:
        conn.execute(
            """
            INSERT INTO meetings (
                id,
                user_id,
                source_platform,
                created_at,
                audio_file_path,
                normalized_audio_path,
                transcript_txt_path,
                transcript_docx_path
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                meeting.id,
                meeting.user_id,
                meeting.source_platform,
                datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                meeting.audio_file_path,
                meeting.normalized_audio_path,
                meeting.transcript_txt_path,
                meeting.transcript_docx_path,
            ),
        )


def save_transcript_segments(data_dir: Path, meeting_id: str, segments: Iterable[TranscriptSegment]) -> None:
    with _connect(data_dir) as conn:
        conn.execute("DELETE FROM transcript_segments WHERE meeting_id = ?", (meeting_id,))
        conn.executemany(
            """
            INSERT INTO transcript_segments (
                meeting_id,
                sequence,
                speaker_label,
                start_time,
                end_time,
                text
            )
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    meeting_id,
                    index,
                    segment.speaker,
                    segment.start,
                    segment.end,
                    segment.text,
                )
                for index, segment in enumerate(segments, start=1)
            ],
        )


def update_meeting_transcript_text(data_dir: Path, meeting_id: str, transcript_text: str, *, preserve_summary: bool = False) -> None:
    with _connect(data_dir) as conn:
        if preserve_summary:
            conn.execute(
                "UPDATE meetings SET transcript_text = ? WHERE id = ?",
                (transcript_text, meeting_id),
            )
        else:
            conn.execute(
                "UPDATE meetings SET transcript_text = ?, summary_text = '', action_text = '' WHERE id = ?",
                (transcript_text, meeting_id),
            )
        if preserve_summary:
            # Speaker or segment edits change the evidence for a cached action
            # list even when callers deliberately keep the meeting summary.
            conn.execute("UPDATE meetings SET action_text = '' WHERE id = ?", (meeting_id,))


def update_meeting_summary_text(data_dir: Path, meeting_id: str, summary_text: str) -> None:
    with _connect(data_dir) as conn:
        conn.execute(
            "UPDATE meetings SET summary_text = ? WHERE id = ?",
            (summary_text, meeting_id),
        )


def update_transcript_segment_text(
    data_dir: Path, meeting_id: str, user_id: int, sequence: int, text: str
) -> bool:
    with _connect(data_dir) as conn:
        result = conn.execute(
            """
            UPDATE transcript_segments
            SET text = ?
            WHERE meeting_id = ? AND sequence = ?
              AND EXISTS (SELECT 1 FROM meetings WHERE id = ? AND user_id = ?)
            """,
            (text, meeting_id, sequence, meeting_id, user_id),
        )
    return result.rowcount == 1


def update_transcript_segment_speaker(
    data_dir: Path, meeting_id: str, user_id: int, sequence: int, speaker_label: str
) -> bool:
    with _connect(data_dir) as conn:
        result = conn.execute(
            """
            UPDATE transcript_segments
            SET speaker_label = ?
            WHERE meeting_id = ? AND sequence = ?
              AND EXISTS (SELECT 1 FROM meetings WHERE id = ? AND user_id = ?)
            """,
            (speaker_label, meeting_id, sequence, meeting_id, user_id),
        )
    return result.rowcount == 1


def update_meeting_action_text(data_dir: Path, meeting_id: str, action_text: str) -> None:
    with _connect(data_dir) as conn:
        conn.execute(
            "UPDATE meetings SET action_text = ? WHERE id = ?",
            (action_text, meeting_id),
        )


def update_meeting_metadata(data_dir: Path, meeting_id: str, title: str, notes: str) -> None:
    with _connect(data_dir) as conn:
        conn.execute(
            "UPDATE meetings SET title = ?, notes = ? WHERE id = ?",
            (title, notes, meeting_id),
        )


def get_meeting_export(data_dir: Path, meeting_id: str, user_id: int) -> MeetingExportRecord | None:
    with _connect(data_dir) as conn:
        row = conn.execute(
            """
            SELECT
                id,
                user_id,
                created_at,
                title,
                notes,
                transcript_text,
                summary_text,
                action_text,
                transcript_txt_path,
                transcript_docx_path,
                audio_file_path
            FROM meetings
            WHERE id = ? AND user_id = ?
            """,
            (meeting_id, user_id),
        ).fetchone()

    return _meeting_export_from_row(row)


def get_local_meeting_export(data_dir: Path, meeting_id: str) -> MeetingExportRecord | None:
    """Read a meeting for the loopback-only local correction interface."""
    with _connect(data_dir) as conn:
        row = conn.execute(
            """
            SELECT id, user_id, created_at, title, notes, transcript_text, summary_text, action_text, transcript_txt_path, transcript_docx_path, audio_file_path
            FROM meetings
            WHERE id = ?
            """,
            (meeting_id,),
        ).fetchone()
    return _meeting_export_from_row(row)


def list_meeting_exports(data_dir: Path, user_id: int, limit: int = 100) -> list[MeetingExportRecord]:
    """List only records owned by one authenticated Web identity."""
    with _connect(data_dir) as conn:
        rows = conn.execute(
            """
            SELECT id, user_id, created_at, title, notes, transcript_text, summary_text,
                   action_text, transcript_txt_path, transcript_docx_path, audio_file_path
            FROM meetings WHERE user_id = ? ORDER BY created_at DESC LIMIT ?
            """,
            (user_id, limit),
        ).fetchall()
    return [meeting for row in rows if (meeting := _meeting_export_from_row(row))]


def get_or_create_web_identity(
    data_dir: Path, provider: str, subject: str, email: str, display_name: str
) -> int:
    """Return a negative user id so OAuth identities cannot collide with bot IDs."""
    with _connect(data_dir) as conn:
        row = conn.execute(
            "SELECT user_id FROM web_identities WHERE provider = ? AND subject = ?",
            (provider, subject),
        ).fetchone()
        if row:
            user_id = int(row["user_id"])
            conn.execute(
                "UPDATE web_identities SET email = ?, display_name = ?, updated_at = CURRENT_TIMESTAMP WHERE user_id = ?",
                (email, display_name, user_id),
            )
            return user_id
        row = conn.execute("SELECT MIN(user_id) AS lowest FROM web_identities").fetchone()
        user_id = min(-1, int(row["lowest"] or 0) - 1)
        conn.execute(
            "INSERT INTO web_identities (user_id, provider, subject, email, display_name) VALUES (?, ?, ?, ?, ?)",
            (user_id, provider, subject, email, display_name),
        )
        return user_id


def create_meeting_claim(data_dir: Path, meeting_id: str, *, ttl_seconds: int = 7 * 24 * 3600) -> str:
    """Issue a one-time, stored-hashed claim token for a bot-originated meeting."""
    token = secrets.token_urlsafe(32)
    token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
    with _connect(data_dir) as conn:
        conn.execute("DELETE FROM meeting_claims WHERE meeting_id = ?", (meeting_id,))
        conn.execute(
            "INSERT INTO meeting_claims (token_hash, meeting_id, expires_at) VALUES (?, ?, ?)",
            (token_hash, meeting_id, int(time.time()) + ttl_seconds),
        )
    return token


def claim_meeting(data_dir: Path, meeting_id: str, token: str, user_id: int) -> bool:
    """Atomically transfer one claimed bot meeting to an authenticated Web user."""
    token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
    with _connect(data_dir) as conn:
        claim = conn.execute(
            "SELECT meeting_id, expires_at FROM meeting_claims WHERE token_hash = ?", (token_hash,)
        ).fetchone()
        if not claim or str(claim["meeting_id"]) != meeting_id or int(claim["expires_at"]) < int(time.time()):
            return False
        old_owner = conn.execute("SELECT user_id FROM meetings WHERE id = ?", (meeting_id,)).fetchone()
        if not old_owner:
            return False
        conn.execute("UPDATE meetings SET user_id = ? WHERE id = ?", (user_id, meeting_id))
        conn.execute(
            "UPDATE speaker_aliases SET user_id = ? WHERE meeting_id = ? AND user_id = ?",
            (user_id, meeting_id, int(old_owner["user_id"])),
        )
        conn.execute("DELETE FROM meeting_claims WHERE token_hash = ?", (token_hash,))
        return True


def list_local_meeting_exports(data_dir: Path, limit: int = 100) -> list[MeetingExportRecord]:
    """List recent meetings for a local-only interface on the owner machine."""
    with _connect(data_dir) as conn:
        rows = conn.execute(
            """
            SELECT id, user_id, created_at, title, notes, transcript_text, summary_text, action_text, transcript_txt_path, transcript_docx_path, audio_file_path
            FROM meetings
            ORDER BY created_at DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
    return [meeting for row in rows if (meeting := _meeting_export_from_row(row))]


def get_local_meeting_audio_path(data_dir: Path, meeting_id: str) -> Path | None:
    with _connect(data_dir) as conn:
        row = conn.execute(
            "SELECT audio_file_path, normalized_audio_path FROM meetings WHERE id = ?",
            (meeting_id,),
        ).fetchone()
    if not row:
        return None
    normalized_path = Path(str(row["normalized_audio_path"]))
    if normalized_path.is_file():
        return normalized_path
    input_path = Path(str(row["audio_file_path"]))
    return input_path if input_path.is_file() else None


def find_matching_local_meeting(
    data_dir: Path,
    normalized_audio_path: Path,
    *,
    exclude_meeting_id: str,
    expected_duration: float | None = None,
) -> MeetingExportRecord | None:
    """Return an earlier completed local meeting with byte-identical audio.

    A repeated upload must not silently produce a different timeline simply
    because the remote recognizer returned different turn boundaries.  Compare
    the already-normalized files so equivalent source formats also match.
    """
    try:
        target_hash = _file_sha256(normalized_audio_path)
    except OSError:
        return None

    with _connect(data_dir) as conn:
        rows = conn.execute(
            """
            SELECT id, user_id, created_at, title, notes, transcript_text,
                   summary_text, action_text, transcript_txt_path,
                   transcript_docx_path, audio_file_path
            FROM meetings
            WHERE source_platform IN ('local_web', 'line_bot')
              AND id != ?
              AND transcript_text != ''
            ORDER BY created_at ASC
            """,
            (exclude_meeting_id,),
        ).fetchall()

    matches: list[tuple[float, MeetingExportRecord]] = []
    for row in rows:
        candidate_path = get_local_meeting_audio_path(data_dir, str(row["id"]))
        if not candidate_path:
            continue
        try:
            if _file_sha256(candidate_path) == target_hash:
                with _connect(data_dir) as conn:
                    coverage_row = conn.execute(
                        "SELECT MAX(end_time) AS max_end FROM transcript_segments WHERE meeting_id = ?",
                        (str(row["id"]),),
                    ).fetchone()
                coverage = float(coverage_row["max_end"] or 0.0)
                # Never reuse a partial transcript for a complete recording.
                # A small tolerance covers encoders that report a little tail
                # padding after the final spoken segment.
                if expected_duration and coverage < expected_duration * 0.98:
                    continue
                meeting = _meeting_export_from_row(row)
                if meeting:
                    matches.append((coverage, meeting))
        except OSError:
            continue
    return max(matches, key=lambda item: item[0])[1] if matches else None


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as audio_file:
        for block in iter(lambda: audio_file.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def get_meeting_speaker_labels(data_dir: Path, meeting_id: str) -> list[str]:
    with _connect(data_dir) as conn:
        rows = conn.execute(
            """
            SELECT speaker_label
            FROM transcript_segments
            WHERE meeting_id = ?
            GROUP BY speaker_label
            ORDER BY CAST(SUBSTR(speaker_label, 9) AS INTEGER), speaker_label
            """,
            (meeting_id,),
        ).fetchall()
    return [str(row["speaker_label"]) for row in rows]


def get_meeting_segments(data_dir: Path, meeting_id: str, user_id: int) -> list[TranscriptSegment]:
    with _connect(data_dir) as conn:
        rows = conn.execute(
            """
            SELECT s.speaker_label, s.start_time, s.end_time, s.text
            FROM transcript_segments AS s
            JOIN meetings AS m ON m.id = s.meeting_id
            WHERE s.meeting_id = ? AND m.user_id = ?
            ORDER BY s.sequence
            """,
            (meeting_id, user_id),
        ).fetchall()

    return [
        TranscriptSegment(
            speaker=str(row["speaker_label"]),
            start=float(row["start_time"] or 0),
            end=float(row["end_time"] or 0),
            text=str(row["text"]),
        )
        for row in rows
    ]


def get_meeting_speaker_samples(data_dir: Path, meeting_id: str) -> list[SpeakerSample]:
    with _connect(data_dir) as conn:
        rows = conn.execute(
            """
            SELECT speaker_label, text
            FROM transcript_segments
            WHERE meeting_id = ?
            ORDER BY sequence
            """,
            (meeting_id,),
        ).fetchall()

    samples: dict[str, str] = {}
    for row in rows:
        speaker_label = str(row["speaker_label"])
        text = str(row["text"]).strip()
        if speaker_label not in samples and text:
            samples[speaker_label] = text

    return [SpeakerSample(speaker_label=label, text=text) for label, text in samples.items()]


def get_latest_meeting_id(data_dir: Path, user_id: int) -> str | None:
    with _connect(data_dir) as conn:
        row = conn.execute(
            """
            SELECT id
            FROM meetings
            WHERE user_id = ?
            ORDER BY created_at DESC
            LIMIT 1
            """,
            (user_id,),
        ).fetchone()
    return str(row["id"]) if row else None


def delete_meeting(data_dir: Path, meeting_id: str, user_id: int) -> bool:
    """Delete one user's meeting and its related database records.

    Foreign-key cascades remove transcript segments and speaker aliases.  The
    user filter is intentional: a meeting ID is not sufficient authority to
    delete another Telegram user's data.
    """
    with _connect(data_dir) as conn:
        result = conn.execute(
            "DELETE FROM meetings WHERE id = ? AND user_id = ?",
            (meeting_id, user_id),
        )
    return result.rowcount == 1


def upsert_speaker_aliases(data_dir: Path, meeting_id: str, user_id: int, aliases: dict[str, str]) -> None:
    with _connect(data_dir) as conn:
        conn.executemany(
            """
            INSERT INTO speaker_aliases (
                meeting_id,
                user_id,
                original_label,
                display_name
            )
            VALUES (?, ?, ?, ?)
            ON CONFLICT(meeting_id, user_id, original_label)
            DO UPDATE SET
                display_name = excluded.display_name,
                updated_at = CURRENT_TIMESTAMP
            """,
            [
                (meeting_id, user_id, original_label, display_name)
                for original_label, display_name in aliases.items()
            ],
        )


def replace_speaker_aliases(data_dir: Path, meeting_id: str, user_id: int, aliases: dict[str, str]) -> None:
    """Replace all aliases for one meeting, allowing a local editor to reset names."""
    with _connect(data_dir) as conn:
        conn.execute(
            "DELETE FROM speaker_aliases WHERE meeting_id = ? AND user_id = ?",
            (meeting_id, user_id),
        )
        conn.executemany(
            """
            INSERT INTO speaker_aliases (meeting_id, user_id, original_label, display_name)
            VALUES (?, ?, ?, ?)
            """,
            [
                (meeting_id, user_id, original_label, display_name)
                for original_label, display_name in aliases.items()
            ],
        )


def get_speaker_aliases(data_dir: Path, meeting_id: str, user_id: int) -> dict[str, str]:
    with _connect(data_dir) as conn:
        rows = conn.execute(
            """
            SELECT original_label, display_name
            FROM speaker_aliases
            WHERE meeting_id = ? AND user_id = ?
            ORDER BY original_label
            """,
            (meeting_id, user_id),
        ).fetchall()
    return {str(row["original_label"]): str(row["display_name"]) for row in rows}


@contextmanager
def _connect(data_dir: Path) -> Iterator[sqlite3.Connection]:
    conn = sqlite3.connect(database_path(data_dir))
    try:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        yield conn
        conn.commit()
    finally:
        conn.close()


def _ensure_column(conn: sqlite3.Connection, table: str, column: str, definition: str) -> None:
    columns = {str(row["name"]) for row in conn.execute(f"PRAGMA table_info({table})")}
    if column not in columns:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")


def _meeting_export_from_row(row: sqlite3.Row | None) -> MeetingExportRecord | None:
    if not row:
        return None
    return MeetingExportRecord(
        id=str(row["id"]),
        user_id=int(row["user_id"]),
        created_at=str(row["created_at"]),
        title=str(row["title"]),
        notes=str(row["notes"]),
        transcript_text=str(row["transcript_text"]),
        summary_text=str(row["summary_text"]),
        action_text=str(row["action_text"]),
        transcript_txt_path=str(row["transcript_txt_path"]),
        transcript_docx_path=str(row["transcript_docx_path"]),
        audio_file_path=str(row["audio_file_path"]),
    )
