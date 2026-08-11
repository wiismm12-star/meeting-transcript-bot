from __future__ import annotations

import base64
import hashlib
import hmac
import json
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from transcript_bot.database import list_local_meeting_exports
from transcript_bot.line_bot import acknowledgement_for_event, verify_webhook_signature
from transcript_bot.line_proxy import create_line_proxy_app
from transcript_bot.web import create_web_app
import transcript_bot.web as web


class LineBotTests(unittest.TestCase):
    def test_signature_verification_uses_the_original_body(self) -> None:
        body = b'{"events":[]}'
        secret = "line-test-secret"
        signature = base64.b64encode(hmac.new(secret.encode(), body, hashlib.sha256).digest()).decode()
        self.assertTrue(verify_webhook_signature(body, signature, secret))
        self.assertFalse(verify_webhook_signature(body + b" ", signature, secret))

    def test_audio_event_confirms_background_transcription(self) -> None:
        self.assertIn("背景轉錄", acknowledgement_for_event({"message": {"type": "audio"}}) or "")

    def test_signed_webhook_replies_to_text_event(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            app = create_web_app(Path(temp_dir))
            client = app.test_client()
            secret = "line-test-secret"
            body = json.dumps(
                {"events": [{"replyToken": "reply-token", "message": {"type": "text"}}]},
                separators=(",", ":"),
            ).encode()
            signature = base64.b64encode(hmac.new(secret.encode(), body, hashlib.sha256).digest()).decode()
            with (
                patch(
                    "transcript_bot.web.settings",
                    SimpleNamespace(
                        enable_line_bot=True,
                        line_channel_secret=secret,
                        line_channel_access_token="access-token",
                    ),
                ),
                patch("transcript_bot.web.reply_to_line") as reply,
            ):
                response = client.post("/line/webhook", data=body, headers={"X-Line-Signature": signature})

        self.assertEqual(response.status_code, 200)
        reply.assert_called_once()

    def test_audio_event_downloads_then_uses_the_shared_background_queue(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            data_dir = Path(temp_dir)
            app = create_web_app(data_dir)
            client = app.test_client()
            secret = "line-test-secret"
            body = json.dumps(
                {
                    "events": [{
                        "replyToken": "reply-token",
                        "source": {"userId": "U-line-user"},
                        "message": {"type": "audio", "id": "line-audio-id", "duration": 5000},
                    }]
                },
                separators=(",", ":"),
            ).encode()
            signature = base64.b64encode(hmac.new(secret.encode(), body, hashlib.sha256).digest()).decode()
            queued = threading.Event()
            with (
                patch(
                    "transcript_bot.web.settings",
                    SimpleNamespace(
                        enable_line_bot=True,
                        line_channel_secret=secret,
                        line_channel_access_token="access-token",
                    ),
                ),
                patch("transcript_bot.web.reply_to_line") as reply,
                patch("transcript_bot.web.download_line_message_content", return_value=b"line-audio"),
                patch("transcript_bot.web._enqueue_or_start", side_effect=lambda *args: queued.set()) as enqueue,
            ):
                response = client.post("/line/webhook", data=body, headers={"X-Line-Signature": signature})
                self.assertTrue(queued.wait(timeout=2), "LINE download was not handed to the queue")

            self.assertEqual(response.status_code, 200)
            self.assertIn("背景轉錄", reply.call_args.args[2])
            queued_paths = enqueue.call_args.args[2]
            self.assertEqual(queued_paths.input_audio.read_bytes(), b"line-audio")
            meetings = list_local_meeting_exports(data_dir)
            self.assertEqual(len(meetings), 1)
            self.assertEqual(meetings[0].title, "LINE 錄音")
            web._active_jobs.pop(queued_paths.job_id, None)
            web._cancel_flags.pop(queued_paths.job_id, None)
            web._job_progress.pop(queued_paths.job_id, None)
            web._line_notifications.pop(queued_paths.job_id, None)

    def test_completed_line_job_pushes_txt_and_word_links_when_configured(self) -> None:
        job_id = "line-complete-job"
        web._line_notifications[job_id] = (
            "U-line-user",
            "access-token",
            "http://192.168.1.10:8765/",
        )
        with patch("transcript_bot.web.push_text_to_line") as push:
            web._notify_line_completion(job_id, succeeded=True)

        self.assertEqual(push.call_args.args[:2], ("U-line-user", "access-token"))
        text = push.call_args.args[2]
        self.assertIn(f"/meetings/{job_id}", text)
        self.assertIn(f"/meetings/{job_id}/download/txt", text)
        self.assertIn(f"/meetings/{job_id}/download/docx", text)

    def test_public_proxy_forwards_only_signed_webhook_payload(self) -> None:
        app = create_line_proxy_app("http://workspace.test/line/webhook")
        client = app.test_client()
        with patch("transcript_bot.line_proxy.httpx.post") as post:
            post.return_value = SimpleNamespace(content=b"OK", status_code=200, headers={"Content-Type": "text/plain"})
            response = client.post(
                "/line/webhook",
                data=b'{"events":[]}',
                headers={"Content-Type": "application/json", "X-Line-Signature": "signature"},
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(post.call_args.args[0], "http://workspace.test/line/webhook")
        self.assertEqual(post.call_args.kwargs["headers"]["X-Line-Signature"], "signature")
        self.assertEqual(client.get("/").status_code, 404)
