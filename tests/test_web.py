from __future__ import annotations

import tempfile
import unittest
from io import BytesIO
from pathlib import Path
from unittest.mock import patch

from transcript_bot.database import (
    MeetingRecord,
    create_meeting,
    get_local_meeting_export,
    init_database,
    get_speaker_aliases,
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
                transcript_txt_path=str(self.data_dir / "transcript.txt"),
                transcript_docx_path=str(self.data_dir / "transcript.docx"),
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
        self.assertIn("新增一場會議".encode(), response.data)

    def test_recent_meeting_card_is_not_a_navigation_target(self) -> None:
        response = self.client.get("/")
        self.assertIn("開啟工作台".encode(), response.data)
        self.assertNotIn(b"data-href=", response.data)
        self.assertNotIn(b"Card click navigation", response.data)

    def test_upload_rejects_missing_or_unsupported_files_before_transcription(self) -> None:
        missing_file = self.client.post("/upload", data={}, follow_redirects=True)
        unsupported_file = self.client.post(
            "/upload",
            data={"audio_file": (BytesIO(b"not audio"), "notes.txt")},
            content_type="multipart/form-data",
            follow_redirects=True,
        )
        self.assertIn("請選擇一個音檔。".encode(), missing_file.data)
        self.assertIn("請上傳 m4a、mp3、wav、ogg、webm、mp4 或 aac 音檔。".encode(), unsupported_file.data)

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
        self.assertIn("已儲存逐字稿。".encode(), response.data)
        meeting = get_local_meeting_export(self.data_dir, self.meeting_id)
        self.assertIsNotNone(meeting)
        assert meeting is not None
        self.assertEqual(meeting.transcript_text, "Speaker 1：已修正內容")

    def test_inline_segment_edit_updates_the_meeting_transcript(self) -> None:
        response = self.client.post(
            f"/meetings/{self.meeting_id}",
            data={"form_action": "segment", "sequence": "1", "text": "直接修改的內容"},
        )
        meeting = get_local_meeting_export(self.data_dir, self.meeting_id)
        self.assertEqual(response.status_code, 200)
        assert meeting is not None
        self.assertEqual(meeting.transcript_text, "Speaker 1：直接修改的內容")

    def test_delete_meeting_removes_the_local_record(self) -> None:
        response = self.client.post(f"/meetings/{self.meeting_id}/delete", follow_redirects=True)
        self.assertEqual(response.status_code, 200)
        self.assertIsNone(get_local_meeting_export(self.data_dir, self.meeting_id))

    def test_index_can_bulk_delete_selected_meetings(self) -> None:
        response = self.client.post(
            "/meetings/delete-selected",
            data={"meeting_ids": [self.meeting_id]},
            follow_redirects=True,
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn("已刪除 1 場會議。".encode(), response.data)
        self.assertIsNone(get_local_meeting_export(self.data_dir, self.meeting_id))

    def test_edit_page_saves_title_and_notes_in_separate_tabs(self) -> None:
        title_response = self.client.post(
            f"/meetings/{self.meeting_id}",
            data={"form_action": "metadata", "title": "測試會議"},
            follow_redirects=True,
        )
        notes_response = self.client.post(
            f"/meetings/{self.meeting_id}",
            data={"form_action": "notes", "notes": "後續確認測試結果。"},
            follow_redirects=True,
        )
        meeting = get_local_meeting_export(self.data_dir, self.meeting_id)
        self.assertIn("已儲存會議名稱。".encode(), title_response.data)
        self.assertIn("已儲存會議筆記。".encode(), notes_response.data)
        assert meeting is not None
        self.assertEqual((meeting.title, meeting.notes), ("測試會議", "後續確認測試結果。"))

    def test_edit_page_replaces_speaker_aliases_in_one_submission(self) -> None:
        response = self.client.post(
            f"/meetings/{self.meeting_id}",
            data={"form_action": "aliases", "alias_Speaker 1": "主持人"},
            follow_redirects=True,
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn("已儲存主講人名稱。".encode(), response.data)
        self.assertIn("主持人".encode(), response.data)
        self.assertEqual(
            get_speaker_aliases(self.data_dir, self.meeting_id, 1001),
            {"Speaker 1": "主持人"},
        )

    def test_download_endpoints_export_the_current_transcript(self) -> None:
        self.client.post(
            f"/meetings/{self.meeting_id}",
            data={"form_action": "aliases", "alias_Speaker 1": "主持人"},
        )
        txt_response = self.client.get(f"/meetings/{self.meeting_id}/download/txt")
        docx_response = self.client.get(f"/meetings/{self.meeting_id}/download/docx")

        self.assertEqual(txt_response.status_code, 200)
        self.assertIn("attachment;", txt_response.headers["Content-Disposition"])
        self.assertIn("主持人：原始內容".encode(), txt_response.data)
        self.assertEqual(docx_response.status_code, 200)
        self.assertIn("attachment;", docx_response.headers["Content-Disposition"])
        self.assertTrue(docx_response.data.startswith(b"PK"))
        txt_response.close()
        docx_response.close()

    def test_edit_page_rejects_blank_transcript(self) -> None:
        response = self.client.post(
            f"/meetings/{self.meeting_id}",
            data={"transcript_text": "   "},
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("逐字稿不可留白。".encode(), response.data)

    def test_summary_tab_renders_a_structured_summary_instead_of_transcript_lines(self) -> None:
        with patch(
            "transcript_bot.web.summarize_meeting_with_ollama",
            return_value={
                "title": "測試會議重點",
                "overview": "本次會議確認了測試方向與後續安排。",
                "highlights": ["確認測試方向。", "安排後續驗證。"],
            },
        ):
            response = self.client.get(f"/meetings/{self.meeting_id}?tab=summary")

        self.assertEqual(response.status_code, 200)
        self.assertIn("測試會議重點".encode(), response.data)
        self.assertIn("本次會議確認了測試方向與後續安排。".encode(), response.data)
        self.assertIn("重新產生摘要".encode(), response.data)
