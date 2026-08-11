from __future__ import annotations

import json
import tempfile
import unittest
from io import BytesIO
from pathlib import Path
from unittest.mock import patch

from transcript_bot.database import (
    MeetingRecord,
    create_meeting,
    get_local_meeting_export,
    get_meeting_segments,
    init_database,
    get_speaker_aliases,
    update_meeting_transcript_text,
)
from transcript_bot.transcription import TranscriptSegment
from transcript_bot.database import save_transcript_segments
from transcript_bot.web import _sync_polished_segments, create_web_app
from transcript_bot.formatting import split_segments_by_sentences
from transcript_bot.job_status import get_job_status, write_job_status


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

    def test_telegram_processing_job_appears_and_web_cancel_requests_stop(self) -> None:
        pending_id = "telegram-processing"
        create_meeting(
            self.data_dir,
            MeetingRecord(
                id=pending_id, user_id=1001, source_platform="telegram",
                audio_file_path=str(self.audio_path), normalized_audio_path="",
                transcript_txt_path="", transcript_docx_path="",
            ),
        )
        write_job_status(
            self.data_dir, pending_id, source="telegram", step="transcribing", pct=45,
            label="transcribing (語音辨識)",
        )

        page = self.client.get("/")
        response = self.client.post(
            f"/meetings/{pending_id}/delete", data={"cancelled": "1"}, follow_redirects=True
        )

        self.assertIn(pending_id.encode(), page.data)
        self.assertIn("45%".encode(), page.data)
        self.assertEqual(response.status_code, 200)
        self.assertTrue(get_job_status(self.data_dir, pending_id)["cancel_requested"])

    def test_active_jobs_endpoint_detects_telegram_work_for_idle_home_page(self) -> None:
        write_job_status(
            self.data_dir, "telegram-new", source="telegram", step="downloading", pct=0,
            label="downloading (從 Telegram 下載音檔)",
        )

        response = self.client.get("/api/jobs/active")

        self.assertEqual(response.status_code, 200)
        self.assertIn("telegram-new", response.get_json()["jobs"])

    def test_index_and_api_show_telegram_bot_status_without_secrets(self) -> None:
        (self.data_dir / "telegram_bot_status.json").write_text(
            json.dumps(
                {
                    "state": "running",
                    "message": "Polling 已啟用，等待 Telegram 訊息。",
                    "updated_at": "2026-08-10T12:00:00+08:00",
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )

        page = self.client.get("/")
        api = self.client.get("/api/telegram/status")

        self.assertIn("Telegram Bot ·".encode(), page.data)
        self.assertIn("運行中".encode(), page.data)
        self.assertEqual(api.get_json()["state"], "running")
        self.assertNotIn(b"not-inspected", page.data)

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

    def test_polished_rows_keep_their_matching_same_speaker_timeline_rows(self) -> None:
        source = [
            TranscriptSegment("Speaker 1", 0, 5, "第一段原文"),
            TranscriptSegment("Speaker 2", 5, 10, "第二段原文"),
            TranscriptSegment("Speaker 1", 10, 15, "第三段原文"),
        ]
        polished = "\n\n".join(
            [
                "Speaker 1：第一段潤稿",
                "Speaker 2：第二段潤稿",
                "Speaker 1：第三段潤稿",
            ]
        )

        synced = _sync_polished_segments(polished, source)

        self.assertEqual([segment.text for segment in synced], ["第一段潤稿", "第二段潤稿", "第三段潤稿"])
        self.assertEqual([(segment.start, segment.end) for segment in synced], [(0, 5), (5, 10), (10, 15)])

    def test_polished_long_row_is_split_before_display(self) -> None:
        source = [TranscriptSegment("Speaker 1", 0, 30, "原始內容")]
        polished = "Speaker 1：" + ("這是一段很長的潤稿內容，" * 20)

        synced = _sync_polished_segments(polished, source)
        display_rows = split_segments_by_sentences(synced)

        self.assertGreater(len(display_rows), 1)
        self.assertTrue(all(len(segment.text) <= 110 for segment in display_rows))
        self.assertEqual("".join(segment.text for segment in display_rows), synced[0].text)

    def test_opening_a_legacy_meeting_repairs_its_overlong_rows(self) -> None:
        long_text = "這是一段舊逐字稿的長句，" * 20
        save_transcript_segments(
            self.data_dir,
            self.meeting_id,
            [TranscriptSegment("Speaker 1", 0, 30, long_text)],
        )
        update_meeting_transcript_text(self.data_dir, self.meeting_id, f"Speaker 1：{long_text}")

        response = self.client.get(f"/meetings/{self.meeting_id}")
        repaired = get_meeting_segments(self.data_dir, self.meeting_id, 1001)

        self.assertEqual(response.status_code, 200)
        self.assertGreater(len(repaired), 1)
        self.assertTrue(all(len(segment.text) <= 110 for segment in repaired))

    def test_delete_meeting_removes_the_local_record(self) -> None:
        response = self.client.post(f"/meetings/{self.meeting_id}/delete", follow_redirects=True)
        self.assertEqual(response.status_code, 200)
        self.assertIsNone(get_local_meeting_export(self.data_dir, self.meeting_id))

    def test_delete_meeting_removes_its_job_directory_when_audio_is_already_missing(self) -> None:
        job_dir = self.data_dir / "jobs" / self.meeting_id
        job_dir.mkdir(parents=True)
        (job_dir / "download-error.txt").write_text("failed", encoding="utf-8")
        self.audio_path.unlink()

        response = self.client.post(f"/meetings/{self.meeting_id}/delete", follow_redirects=True)

        self.assertEqual(response.status_code, 200)
        self.assertFalse(job_dir.exists())
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

    def test_summary_tab_does_not_generate_a_summary_during_page_load(self) -> None:
        with patch(
            "transcript_bot.web.summarize_meeting_with_ollama",
        ) as summarize:
            response = self.client.get(f"/meetings/{self.meeting_id}?tab=summary")

        self.assertEqual(response.status_code, 200)
        summarize.assert_not_called()
        self.assertIn("尚未產生摘要".encode(), response.data)
        self.assertIn("重新產生摘要".encode(), response.data)
