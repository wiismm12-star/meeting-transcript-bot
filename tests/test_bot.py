from __future__ import annotations

import asyncio
import threading
import unittest
from types import SimpleNamespace
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from telegram.error import BadRequest, TimedOut

from transcript_bot import bot
from transcript_bot.bot import _local_telegram_file_path, _telegram_download_error_message
from transcript_bot.database import get_local_meeting_export, init_database
from transcript_bot.job_status import get_job_status
from transcript_bot.transcription import TranscriptSegment
from transcript_bot.web import create_web_app


class TelegramDownloadErrorTests(unittest.TestCase):
    def test_file_too_big_has_an_actionable_traditional_chinese_message(self) -> None:
        message = _telegram_download_error_message(BadRequest("File is too big"))

        self.assertIn("大小限制", message)
        self.assertIn("本機 Web 工作台", message)

    def test_other_bad_request_does_not_expose_telegram_details(self) -> None:
        message = _telegram_download_error_message(BadRequest("private API detail"))

        self.assertNotIn("private API detail", message)
        self.assertIn("無法從 Telegram 下載", message)

    def test_timeout_has_a_safe_retry_message(self) -> None:
        message = _telegram_download_error_message(TimedOut())

        self.assertIn("逾時", message)
        self.assertNotIn("http", message.lower())

    def test_local_file_path_supports_docker_desktop_colon_mapping(self) -> None:
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            mapped = root / "12345\uf03aabc" / "music" / "file.mp3"
            mapped.parent.mkdir(parents=True)
            mapped.write_bytes(b"audio")
            with patch("transcript_bot.bot.settings.telegram_local_file_host_root", root):
                actual = _local_telegram_file_path(
                    "/var/lib/telegram-bot-api/12345:abc/music/file.mp3"
                )

        self.assertEqual(actual, mapped)

    def test_local_file_path_falls_back_to_the_mounted_file_name(self) -> None:
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            actual_file = root / "unexpected-layout" / "file_99.mp3"
            actual_file.parent.mkdir(parents=True)
            actual_file.write_bytes(b"audio")
            with patch("transcript_bot.bot.settings.telegram_local_file_host_root", root):
                actual = _local_telegram_file_path(
                    "/var/lib/telegram-bot-api/12345:abc/music/file_99.mp3"
                )

        self.assertEqual(actual, actual_file)


class TelegramWebLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def test_single_file_whisper_progress_advances_from_20_to_80(self) -> None:
        with TemporaryDirectory() as temp_dir:
            data_dir = Path(temp_dir)
            init_database(data_dir)
            progress_updates: list[int] = []

            class FakeMessage:
                chat_id = 123
                voice = None
                document = None
                audio = SimpleNamespace(file_id="file-id", file_size=10, file_name="meeting.mp3")

                async def reply_text(self, _text, **_kwargs):
                    return None

            class FakeBot:
                async def send_chat_action(self, **_kwargs):
                    return None

                async def get_file(self, _file_id):
                    return SimpleNamespace(file_path="/unused")

            async def fake_download(_telegram_file, target):
                target.write_bytes(b"fake-audio")

            def fake_normalize(source, target):
                target.write_bytes(source.read_bytes())

            def fake_transcribe(_audio, progress_callback, **_kwargs):
                for end_time in (25.0, 50.0, 100.0):
                    progress_callback(end_time, end_time)
                return [TranscriptSegment("SPEAKER_0", 0.0, 1.0, "測試內容")]

            original_write_status = bot.write_job_status

            def capture_status(*args, **kwargs):
                if kwargs.get("step") == "transcribing":
                    progress_updates.append(kwargs["pct"])
                original_write_status(*args, **kwargs)

            update = SimpleNamespace(message=FakeMessage(), effective_user=SimpleNamespace(id=999))
            context = SimpleNamespace(bot=FakeBot())
            with (
                patch.object(bot.settings, "data_dir", data_dir),
                patch.object(bot.settings, "enable_polish", False),
                patch("transcript_bot.bot._download_telegram_audio", fake_download),
                patch("transcript_bot.bot.normalize_audio", fake_normalize),
                patch("transcript_bot.bot.get_audio_duration", return_value=100.0),
                patch("transcript_bot.bot.transcribe_audio_smart", fake_transcribe),
                patch("transcript_bot.bot.write_job_status", capture_status),
            ):
                await bot.process_audio(update, context)

        self.assertEqual(progress_updates, [20, 35, 50, 80])

    async def test_telegram_job_is_visible_then_web_cancel_stops_and_cleans_it(self) -> None:
        """Exercise the real Bot → shared status → Web cancel handoff end to end."""
        with TemporaryDirectory() as temp_dir:
            data_dir = Path(temp_dir)
            init_database(data_dir)
            started = threading.Event()
            release = threading.Event()
            replies: list[str] = []

            class FakeMessage:
                chat_id = 123
                voice = None
                document = None
                audio = SimpleNamespace(file_id="file-id", file_size=10, file_name="meeting.mp3")

                async def reply_text(self, text, **_kwargs):
                    replies.append(text)

            class FakeBot:
                async def send_chat_action(self, **_kwargs):
                    return None

                async def get_file(self, _file_id):
                    return SimpleNamespace(file_path="/unused")

            async def fake_download(_telegram_file, target):
                target.write_bytes(b"fake-audio")

            def fake_normalize(source, target):
                target.write_bytes(source.read_bytes())

            def fake_transcribe(_audio, **_kwargs):
                started.set()
                release.wait(timeout=2)
                return [TranscriptSegment("SPEAKER_0", 0.0, 1.0, "測試內容")]

            update = SimpleNamespace(message=FakeMessage(), effective_user=SimpleNamespace(id=999))
            context = SimpleNamespace(bot=FakeBot())
            with (
                patch.object(bot.settings, "data_dir", data_dir),
                patch("transcript_bot.bot._download_telegram_audio", fake_download),
                patch("transcript_bot.bot.normalize_audio", fake_normalize),
                patch("transcript_bot.bot.transcribe_audio_smart", fake_transcribe),
            ):
                task = asyncio.create_task(
                    asyncio.to_thread(lambda: asyncio.run(bot.process_audio(update, context)))
                )
                self.assertTrue(await asyncio.to_thread(started.wait, 1))

                status_files = list((data_dir / "job-status").glob("*.json"))
                self.assertEqual(len(status_files), 1)
                job_id = status_files[0].stem
                self.assertEqual(get_job_status(data_dir, job_id)["step"], "transcribing")

                web = create_web_app(data_dir).test_client()
                self.assertIn(job_id.encode(), web.get("/").data)
                response = web.post(f"/meetings/{job_id}/delete", data={"cancelled": "1"})
                self.assertEqual(response.status_code, 302)
                self.assertTrue(get_job_status(data_dir, job_id)["cancel_requested"])

                release.set()
                await task
                self.assertIsNone(get_local_meeting_export(data_dir, job_id))
                self.assertTrue(any("終止" in reply for reply in replies))


if __name__ == "__main__":
    unittest.main()
