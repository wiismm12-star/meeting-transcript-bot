from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
import unittest

from transcript_bot.deepgram import transcribe_with_deepgram


class DeepgramRequestTests(unittest.TestCase):
    def test_uses_latest_diarization_model_for_pre_recorded_audio(self) -> None:
        response = MagicMock(status_code=200)
        response.json.return_value = {
            "results": {
                "utterances": [
                    {"speaker": 0, "start": 0, "end": 1, "transcript": "第一位說話。"},
                    {"speaker": 1, "start": 1, "end": 2, "transcript": "第二位說話。"},
                ]
            }
        }

        with TemporaryDirectory() as temp_dir:
            audio_path = Path(temp_dir) / "meeting.mp3"
            audio_path.write_bytes(b"test audio")
            with (
                patch("transcript_bot.deepgram.settings", SimpleNamespace(deepgram_api_key="test-key", deepgram_keyterms="KKBOX,文湖線")),
                patch("transcript_bot.deepgram.httpx.post", return_value=response) as post,
            ):
                segments = transcribe_with_deepgram(audio_path)

        params = post.call_args.kwargs["params"]
        self.assertEqual(params["diarize_model"], "latest")
        self.assertEqual(params["language"], "zh-TW")
        self.assertEqual(params["keyterm"], ["KKBOX", "文湖線"])
        self.assertNotIn("diarize", params)
        self.assertEqual([segment.speaker for segment in segments], ["SPEAKER_0", "SPEAKER_1"])

    def test_word_timestamp_mode_does_not_use_deepgram_speaker_labels(self) -> None:
        response = MagicMock(status_code=200)
        response.json.return_value = {
            "results": {
                "channels": [
                    {"alternatives": [{"words": [{"speaker": 7, "start": 0, "end": 0.5, "punctuated_word": "測試"}]}]}
                ]
            }
        }

        with TemporaryDirectory() as temp_dir:
            audio_path = Path(temp_dir) / "meeting.mp3"
            audio_path.write_bytes(b"test audio")
            with (
                patch("transcript_bot.deepgram.settings", SimpleNamespace(deepgram_api_key="test-key", deepgram_keyterms="")),
                patch("transcript_bot.deepgram.httpx.post", return_value=response),
            ):
                segments = transcribe_with_deepgram(audio_path, word_timestamps=True)

        self.assertEqual([(segment.speaker, segment.start, segment.end, segment.text) for segment in segments], [("UNASSIGNED", 0.0, 0.5, "測試")])


if __name__ == "__main__":
    unittest.main()
