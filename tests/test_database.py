from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from transcript_bot.database import (
    MeetingRecord,
    create_meeting,
    delete_meeting,
    get_meeting_export,
    get_meeting_segments,
    get_speaker_aliases,
    init_database,
    save_transcript_segments,
    update_meeting_summary_text,
    update_meeting_transcript_text,
    upsert_speaker_aliases,
)
from transcript_bot.transcription import TranscriptSegment


class DeleteMeetingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.data_dir = Path(self.temp_dir.name)
        init_database(self.data_dir)
        self.meeting_id = "meeting-a"
        create_meeting(
            self.data_dir,
            MeetingRecord(
                id=self.meeting_id,
                user_id=1001,
                source_platform="telegram",
                audio_file_path="input.ogg",
                normalized_audio_path="normalized.mp3",
                transcript_txt_path="transcript.txt",
                transcript_docx_path="transcript.docx",
            ),
        )
        save_transcript_segments(
            self.data_dir,
            self.meeting_id,
            [TranscriptSegment(speaker="Speaker 1", text="測試內容", start=0, end=1)],
        )
        upsert_speaker_aliases(self.data_dir, self.meeting_id, 1001, {"Speaker 1": "主持人"})

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_delete_meeting_removes_owned_meeting_and_related_records(self) -> None:
        self.assertTrue(delete_meeting(self.data_dir, self.meeting_id, 1001))
        self.assertIsNone(get_meeting_export(self.data_dir, self.meeting_id, 1001))
        self.assertEqual(get_speaker_aliases(self.data_dir, self.meeting_id, 1001), {})

    def test_delete_meeting_does_not_allow_another_user(self) -> None:
        self.assertFalse(delete_meeting(self.data_dir, self.meeting_id, 2002))
        self.assertIsNotNone(get_meeting_export(self.data_dir, self.meeting_id, 1001))
        self.assertEqual(get_speaker_aliases(self.data_dir, self.meeting_id, 1001), {"Speaker 1": "主持人"})

    def test_get_meeting_segments_respects_meeting_owner(self) -> None:
        segments = get_meeting_segments(self.data_dir, self.meeting_id, 1001)
        self.assertEqual([(segment.speaker, segment.text) for segment in segments], [("Speaker 1", "測試內容")])
        self.assertEqual(get_meeting_segments(self.data_dir, self.meeting_id, 2002), [])

    def test_export_record_includes_creation_time(self) -> None:
        meeting = get_meeting_export(self.data_dir, self.meeting_id, 1001)
        self.assertIsNotNone(meeting)
        assert meeting is not None
        self.assertRegex(meeting.created_at, r"^\d{4}-\d{2}-\d{2}")

    def test_transcript_change_invalidates_cached_summary(self) -> None:
        update_meeting_summary_text(self.data_dir, self.meeting_id, '{"title":"舊摘要"}')
        update_meeting_transcript_text(self.data_dir, self.meeting_id, "Speaker 1：更新後內容")
        meeting = get_meeting_export(self.data_dir, self.meeting_id, 1001)
        assert meeting is not None
        self.assertEqual(meeting.summary_text, "")


if __name__ == "__main__":
    unittest.main()
