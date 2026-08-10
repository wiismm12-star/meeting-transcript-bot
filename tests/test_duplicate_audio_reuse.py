from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from transcript_bot.database import (
    MeetingRecord,
    create_meeting,
    find_matching_local_meeting,
    init_database,
    update_meeting_transcript_text,
)


class DuplicateLocalAudioTests(unittest.TestCase):
    def test_finds_an_earlier_completed_identical_normalized_audio(self) -> None:
        with TemporaryDirectory() as temp_dir:
            data_dir = Path(temp_dir)
            init_database(data_dir)
            first_audio = data_dir / "first.mp3"
            second_audio = data_dir / "second.mp3"
            first_audio.write_bytes(b"same normalized audio")
            second_audio.write_bytes(b"same normalized audio")
            for meeting_id, audio_path in (("first", first_audio), ("second", second_audio)):
                create_meeting(data_dir, MeetingRecord(
                    id=meeting_id, user_id=0, source_platform="local_web",
                    audio_file_path=str(audio_path), normalized_audio_path=str(audio_path),
                    transcript_txt_path="", transcript_docx_path="",
                ))
            update_meeting_transcript_text(data_dir, "first", "Speaker 1：既有內容")

            match = find_matching_local_meeting(data_dir, second_audio, exclude_meeting_id="second")

            self.assertIsNotNone(match)
            assert match is not None
            self.assertEqual(match.id, "first")
