from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
import unittest

from transcript_bot.gladia import transcribe_with_gladia


class GladiaRequestTests(unittest.TestCase):
    def test_uploads_audio_requests_diarization_and_deletes_completed_job(self) -> None:
        uploaded = MagicMock(status_code=200)
        uploaded.json.return_value = {"audio_url": "https://api.gladia.io/file/example"}
        created = MagicMock(status_code=200)
        created.json.return_value = {"id": "job-123"}
        completed = MagicMock(status_code=200)
        completed.json.return_value = {
            "status": "done",
            "result": {
                "transcription": {
                    "utterances": [
                        {"speaker": 0, "start": 0, "end": 1.2, "text": "KKBOX 風雲榜。"},
                        {"speaker": 1, "start": 1.2, "end": 2.4, "text": "前往文湖線。"},
                    ]
                }
            },
        }

        with TemporaryDirectory() as temp_dir:
            audio_path = Path(temp_dir) / "meeting.mp3"
            audio_path.write_bytes(b"test audio")
            with (
                patch(
                    "transcript_bot.gladia.settings",
                    SimpleNamespace(
                        gladia_api_key="test-key",
                        gladia_num_speakers=4,
                        gladia_vocabulary="KKBOX, 風雲榜, 文湖線",
                    ),
                ),
                patch("transcript_bot.gladia.httpx.post", side_effect=[uploaded, created]) as post,
                patch("transcript_bot.gladia.httpx.get", return_value=completed),
                patch("transcript_bot.gladia.httpx.delete") as delete,
            ):
                segments = transcribe_with_gladia(audio_path)

        request_payload = post.call_args_list[1].kwargs["json"]
        self.assertTrue(request_payload["diarization"])
        self.assertEqual(request_payload["diarization_config"]["number_of_speakers"], 4)
        self.assertEqual(request_payload["language_config"], {"languages": ["zh", "en"], "code_switching": True})
        self.assertEqual(request_payload["custom_vocabulary_config"]["vocabulary"], ["KKBOX", "風雲榜", "文湖線"])
        self.assertEqual([(item.speaker, item.text) for item in segments], [("SPEAKER_0", "KKBOX 風雲榜。"), ("SPEAKER_1", "前往文湖線。")])
        self.assertIn("job-123", delete.call_args.args[0])


if __name__ == "__main__":
    unittest.main()
