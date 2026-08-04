from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from transcript_bot.database import (
    MeetingRecord,
    create_meeting,
    get_local_meeting_export,
    init_database,
    update_meeting_transcript_text,
)
from transcript_bot.transcription import TranscriptSegment
from transcript_bot.database import save_transcript_segments
from transcript_bot.web import create_web_app


class LocalWebCorrectionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.data_dir = Path(self.temp_dir.name)
        self.meeting_id = "local-web-meeting"
        init_database(self.data_dir)
        self.audio_path = self.data_dir / "input.ogg"
        self.audio_path.write_bytes(b"fake-audio")
        create_meeting(
            self.data_dir,
            MeetingRecord(
                id=self.meeting_id,
                user_id=1001,
                source_platform="telegram",
                audio_file_path=str(self.audio_path),
                normalized_audio_path="normalized.mp3",
                transcript_txt_path="transcript.txt",
                transcript_docx_path="transcript.docx",
            ),
        )
        update_meeting_transcript_text(self.data_dir, self.meeting_id, "Speaker 1：原始內容")
        save_transcript_segments(
            self.data_dir,
            self.meeting_id,
            [TranscriptSegment("Speaker 1", 1.5, 3.0, "可對照的原始段落")],
        )
        self.client = create_web_app(self.data_dir).test_client()

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_index_lists_local_meetings(self) -> None:
        response = self.client.get("/")
        self.assertEqual(response.status_code, 200)
        self.assertIn(self.meeting_id.encode(), response.data)

    def test_edit_page_exposes_local_audio_and_segment_alignment(self) -> None:
        page = self.client.get(f"/meetings/{self.meeting_id}")
        audio = self.client.get(f"/meetings/{self.meeting_id}/audio")
        self.assertEqual(page.status_code, 200)
        self.assertIn(b"meeting-audio", page.data)
        self.assertIn("可對照的原始段落".encode(), page.data)
        self.assertEqual(audio.status_code, 200)
        self.assertEqual(audio.data, b"fake-audio")
        audio.close()

    def test_edit_page_saves_corrected_transcript(self) -> None:
        response = self.client.post(
            f"/meetings/{self.meeting_id}",
            data={"transcript_text": "Speaker 1：已修正內容"},
            follow_redirects=True,
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn("已儲存。" .encode(), response.data)
        meeting = get_local_meeting_export(self.data_dir, self.meeting_id)
        self.assertIsNotNone(meeting)
        assert meeting is not None
        self.assertEqual(meeting.transcript_text, "Speaker 1：已修正內容")

    def test_edit_page_rejects_blank_transcript(self) -> None:
        response = self.client.post(
            f"/meetings/{self.meeting_id}",
            data={"transcript_text": "   "},
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("逐字稿不可留白。".encode(), response.data)
