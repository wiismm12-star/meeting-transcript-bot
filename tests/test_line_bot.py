from __future__ import annotations

import base64
import hashlib
import hmac
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from transcript_bot.line_bot import acknowledgement_for_event, verify_webhook_signature
from transcript_bot.web import create_web_app


class LineBotTests(unittest.TestCase):
    def test_signature_verification_uses_the_original_body(self) -> None:
        body = b'{"events":[]}'
        secret = "line-test-secret"
        signature = base64.b64encode(hmac.new(secret.encode(), body, hashlib.sha256).digest()).decode()
        self.assertTrue(verify_webhook_signature(body, signature, secret))
        self.assertFalse(verify_webhook_signature(body + b" ", signature, secret))

    def test_audio_event_returns_a_testing_acknowledgement(self) -> None:
        self.assertIn("測試連線正常", acknowledgement_for_event({"message": {"type": "audio"}}) or "")

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
