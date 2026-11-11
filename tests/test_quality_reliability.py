from __future__ import annotations

import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from transcript_bot import ollama_client as ollama
from transcript_bot import web
from transcript_bot.database import (
    MeetingRecord, create_meeting, get_local_meeting_export, get_meeting_segments,
    init_database, save_transcript_segments, update_meeting_summary_text, update_meeting_transcript_text,
)
from transcript_bot.job_status import get_job_status, write_job_status
from transcript_bot.storage import create_job_paths
from transcript_bot.transcription import TranscriptSegment


def response_for(payload):
    response = MagicMock(status_code=200)
    response.json.return_value = {"message": {"content": json.dumps(payload, ensure_ascii=False)}}
    return response


class ModelQualityTests(unittest.TestCase):
    def test_polish_preserves_meaningful_prefixes_and_mixed_language(self):
        sources = ["得到確認後再發版。", "地圖功能下週完成。", "改用C開發，週五交付。", "方案 B 比較快，先做效能測試。"]
        for source in sources:
            for output in (source, "DROP"):
                with self.subTest(source=source, output=output):
                    response = MagicMock(status_code=200)
                    response.json.return_value = {"message": {"content": output}}
                    with patch.object(ollama.httpx, "post", return_value=response), patch.object(ollama, "_load_glossary", return_value=[]):
                        self.assertEqual(ollama.polish_with_ollama(f"Speaker 1：{source}"), f"Speaker 1：{source}")

    def test_summary_rejects_wrong_types_and_empty_highlights(self):
        for highlights in ("不是陣列", {"item": "內容"}, [None], [123], [{}], [], ["  "]):
            with self.subTest(highlights=highlights), patch.object(ollama.httpx, "post", return_value=response_for({
                "title": "標題", "overview": "概述", "highlights": highlights,
            })):
                with self.assertRaises(ollama.OllamaSchemaError):
                    ollama.summarize_meeting_with_ollama("測試逐字稿")

    def test_summary_rejects_invalid_title_and_overview(self):
        for title, overview in ((None, "概述"), (123, "概述"), ("標題", None), ("標題", []), ("標題", " ")):
            with self.subTest(title=title, overview=overview), patch.object(ollama.httpx, "post", return_value=response_for({
                "title": title, "overview": overview, "highlights": ["重點"],
            })):
                with self.assertRaises(ollama.OllamaSchemaError):
                    ollama.summarize_meeting_with_ollama("測試逐字稿")

    def test_summary_removes_exact_duplicate_highlights(self):
        with patch.object(ollama.httpx, "post", return_value=response_for({
            "title": "標題", "overview": "概述", "highlights": ["重點一", " 重點一 ", "重點二"],
        })):
            self.assertEqual(ollama.summarize_meeting_with_ollama("測試逐字稿")["highlights"], ["重點一", "重點二"])

    def test_empty_transcript_makes_no_model_request(self):
        with patch.object(ollama.httpx, "post") as post:
            with self.assertRaises(ollama.OllamaError):
                ollama.summarize_meeting_with_ollama(" \n ")
        post.assert_not_called()

    def test_malformed_service_responses_raise_recoverable_error(self):
        for body in (None, [], {}, {"message": None}, {"message": []}, {"message": {"content": 123}}):
            response = MagicMock(status_code=200)
            response.json.return_value = body
            for operation in (ollama.summarize_meeting_with_ollama, ollama.polish_with_ollama):
                with self.subTest(body=body, operation=operation.__name__), patch.object(ollama.httpx, "post", return_value=response):
                    with self.assertRaises(ollama.OllamaError):
                        operation("Speaker 1：測試逐字稿")
        response.json.side_effect = ValueError("invalid JSON")
        with patch.object(ollama.httpx, "post", return_value=response):
            with self.assertRaises(ollama.OllamaError):
                ollama.summarize_meeting_with_ollama("測試逐字稿")

    def test_failed_middle_chunk_does_not_produce_incomplete_summary(self):
        with patch.object(ollama, "_ollama_chat_json", side_effect=[{"highlights": ["前段重點"]}, ollama.OllamaError("timeout")]) as chat:
            with self.assertRaises(ollama.OllamaError):
                ollama.summarize_meeting_with_ollama("Speaker 1：討論內容。\n\n" * 1000)
        self.assertEqual(chat.call_count, 2)

    def test_invalid_chunk_highlights_do_not_become_character_bullets(self):
        with patch.object(ollama.httpx, "post", return_value=response_for({"highlights": "格式錯誤"})):
            with self.assertRaises(ollama.OllamaSchemaError):
                ollama.summarize_meeting_with_ollama("Speaker 1：討論內容。\n\n" * 1000)


class SummaryRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.data_dir = Path(self.temp.name)
        init_database(self.data_dir)
        with patch.object(web.settings, "enable_google_login", False):
            self.client = web.create_web_app(self.data_dir).test_client()
        self.paths = create_job_paths(self.data_dir, ".wav")
        self.paths.input_audio.write_bytes(b"test audio")
        self.job_id = self.paths.job_id
        create_meeting(self.data_dir, MeetingRecord(
            id=self.job_id, user_id=1001, source_platform="local_web",
            audio_file_path=str(self.paths.input_audio), normalized_audio_path=str(self.paths.normalized_audio),
            transcript_txt_path=str(self.paths.transcript_txt), transcript_docx_path=str(self.paths.transcript_docx),
        ))

    def test_summary_failure_keeps_transcript_timestamps_and_success_status(self):
        self._assert_summary_failure("web")

    def test_line_summary_failure_still_reports_transcription_success(self):
        self._assert_summary_failure("line")

    def _assert_summary_failure(self, source):
        if source == "line":
            write_job_status(self.data_dir, self.job_id, source="line", step="transcribing")

        def normalize(source, target):
            target.write_bytes(b"normalized")

        with (
            patch.object(web, "normalize_audio", side_effect=normalize),
            patch.object(web, "get_audio_duration", return_value=10),
            patch.object(web, "find_matching_local_meeting", return_value=None),
            patch.object(web, "transcribe_audio_smart", return_value=[TranscriptSegment("Speaker 1", 0, 10, "週五確認測試結果。")]),
            patch.object(web.settings, "enable_polish", False),
            patch.object(web, "_generate_meeting_summary", side_effect=ollama.OllamaError("offline")),
            patch.object(web, "_on_job_complete"),
            patch.object(web, "_notify_line_completion") as notify,
        ):
            web._process_job(self.data_dir, self.paths, "test.wav", threading.Event())
        meeting = get_local_meeting_export(self.data_dir, self.job_id)
        self.assertIn("週五確認測試結果", meeting.transcript_text)
        self.assertFalse(meeting.summary_text)
        segments = get_meeting_segments(self.data_dir, self.job_id, 1001)
        self.assertEqual((segments[0].start, segments[-1].end), (0, 10))
        status = get_job_status(self.data_dir, self.job_id)
        self.assertEqual(status["step"], "done")
        self.assertEqual(status["source"], source)
        self.assertTrue(status["summary_failed"])
        self.assertTrue(notify.call_args.kwargs["succeeded"])
        page = self.client.get(f"/meetings/{self.job_id}?tab=summary")
        self.assertIn("摘要暫時無法產生".encode(), page.data)
        self.assertIn("無須重新上傳錄音".encode(), page.data)
        self.assertNotIn(self.job_id, self.client.get("/api/jobs/active").get_json()["jobs"])

    def test_reused_line_transcript_reports_success(self):
        update_meeting_transcript_text(self.data_dir, self.job_id, "Speaker 1：已完成的逐字稿。")
        save_transcript_segments(self.data_dir, self.job_id, [TranscriptSegment("Speaker 1", 0, 10, "已完成的逐字稿。")])
        existing = get_local_meeting_export(self.data_dir, self.job_id)
        write_job_status(self.data_dir, self.job_id, source="line", step="transcribing")
        with (
            patch.object(web, "normalize_audio", side_effect=lambda source, target: target.write_bytes(b"normalized")),
            patch.object(web, "get_audio_duration", return_value=10),
            patch.object(web, "find_matching_local_meeting", return_value=existing),
            patch.object(web, "transcribe_audio_smart") as transcribe,
            patch.object(web, "_on_job_complete"),
            patch.object(web, "_notify_line_completion") as notify,
        ):
            web._process_job(self.data_dir, self.paths, "test.wav", threading.Event())
        transcribe.assert_not_called()
        self.assertTrue(notify.call_args.kwargs["succeeded"])
        self.assertEqual(get_job_status(self.data_dir, self.job_id)["step"], "done")

    def test_retry_clears_failure_notice_and_keeps_unknown_categories_visible(self):
        update_meeting_transcript_text(self.data_dir, self.job_id, "Speaker 1：保留的逐字稿。")
        write_job_status(self.data_dir, self.job_id, source="web", step="done", summary_failed=True)
        payload = {"title": "測試摘要", "overview": "測試概述", "highlights": ["【風險】外部服務延遲", "【決議】保留原文"]}
        with patch.object(web, "summarize_meeting_with_ollama", return_value=payload), patch.object(web, "_meeting_duration_seconds", return_value=10):
            response = self.client.post(f"/meetings/{self.job_id}", data={"form_action": "summary"}, follow_redirects=True)
        self.assertEqual(response.status_code, 200)
        self.assertIn("【風險】外部服務延遲".encode(), response.data)
        self.assertIn("保留原文".encode(), response.data)
        self.assertNotIn("摘要暫時無法產生".encode(), response.data)
        self.assertFalse(get_job_status(self.data_dir, self.job_id).get("summary_failed"))

    def test_bad_model_response_preserves_previous_summary_on_retry(self):
        update_meeting_transcript_text(self.data_dir, self.job_id, "Speaker 1：保留的逐字稿。")
        existing = json.dumps({"title": "原摘要", "overview": "原概述", "highlights": ["原重點"]})
        update_meeting_summary_text(self.data_dir, self.job_id, existing)
        with patch.object(ollama.httpx, "post", return_value=response_for({"highlights": [None]})), patch.object(web, "_meeting_duration_seconds", return_value=10):
            response = self.client.post(f"/meetings/{self.job_id}", data={"form_action": "summary"}, follow_redirects=True)
        self.assertEqual(response.status_code, 200)
        self.assertIn("摘要暫時無法產生".encode(), response.data)
        self.assertIn("原重點".encode(), response.data)
        self.assertEqual(get_local_meeting_export(self.data_dir, self.job_id).summary_text, existing)


if __name__ == "__main__":
    unittest.main()
