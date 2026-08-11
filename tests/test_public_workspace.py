from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from transcript_bot.database import (
    MeetingRecord,
    claim_meeting,
    create_meeting,
    create_meeting_claim,
    get_meeting_export,
    get_or_create_web_identity,
    init_database,
    list_meeting_exports,
)


class PublicWorkspaceDatabaseTests(unittest.TestCase):
    def test_claim_is_one_time_and_moves_meeting_to_google_identity(self) -> None:
        with TemporaryDirectory() as temporary:
            data_dir = Path(temporary)
            init_database(data_dir)
            create_meeting(data_dir, MeetingRecord(
                id="line-meeting", user_id=123, source_platform="line_bot",
                audio_file_path="input", normalized_audio_path="normalized",
                transcript_txt_path="transcript.txt", transcript_docx_path="transcript.docx",
            ))
            alice = get_or_create_web_identity(data_dir, "google", "alice-sub", "alice@example.com", "Alice")
            bob = get_or_create_web_identity(data_dir, "google", "bob-sub", "bob@example.com", "Bob")
            token = create_meeting_claim(data_dir, "line-meeting")

            self.assertIsNone(get_meeting_export(data_dir, "line-meeting", alice))
            self.assertTrue(claim_meeting(data_dir, "line-meeting", token, alice))
            self.assertIsNotNone(get_meeting_export(data_dir, "line-meeting", alice))
            self.assertIsNone(get_meeting_export(data_dir, "line-meeting", bob))
            self.assertFalse(claim_meeting(data_dir, "line-meeting", token, bob))
            self.assertEqual([meeting.id for meeting in list_meeting_exports(data_dir, alice)], ["line-meeting"])
