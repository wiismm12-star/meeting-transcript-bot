from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from transcript_bot.database import MeetingRecord, create_meeting, init_database, update_meeting_transcript_text
from transcript_bot.web import create_web_app


class LanBypassTests(unittest.TestCase):
    def test_private_host_bypasses_but_public_tunnel_requires_login(self) -> None:
        with TemporaryDirectory() as temporary:
            data_dir = Path(temporary)
            init_database(data_dir)
            create_meeting(data_dir, MeetingRecord(
                id="lan-record", user_id=0, source_platform="local_web",
                audio_file_path="input", normalized_audio_path="normalized",
                transcript_txt_path="text", transcript_docx_path="docx",
            ))
            update_meeting_transcript_text(data_dir, "lan-record", "Speaker 1：內網會議")
            with patch("transcript_bot.web.settings", SimpleNamespace(
                enable_google_login=True, google_oauth_client_id="id",
                google_oauth_client_secret="secret", google_oauth_redirect_uri="https://public.example/auth/google/callback",
                web_session_secret="session", web_host="0.0.0.0",
                web_lan_bypass_google_login=True,
                web_lan_trusted_cidrs="172.16.0.0/12",
                public_web_base_url="https://public.example", web_allow_upload=True,
                enable_line_bot=False,
            )):
                app = create_web_app(data_dir)
                client = app.test_client()
                lan = client.get("/", headers={"Host": "172.16.10.25:8765"}, environ_overrides={"REMOTE_ADDR": "172.16.10.30"})
                public = client.get("/", headers={"Host": "public.example"}, environ_overrides={"REMOTE_ADDR": "127.0.0.1"})
            self.assertEqual(lan.status_code, 200)
            self.assertIn(b"lan-record", lan.data)
            self.assertEqual(public.status_code, 302)
            self.assertTrue(public.headers["Location"].startswith("/login"))
