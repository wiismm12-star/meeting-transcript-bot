from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from transcript_bot.whisper_local import transcribe_with_local_whisper


class LocalWhisperTests(unittest.TestCase):
    def test_transcription_uses_traditional_chinese_prompt_and_keeps_timestamps(self) -> None:
        whisper_model = MagicMock()
        whisper_model.transcribe.return_value = (
            [SimpleNamespace(start=1.25, end=3.5, text=" 測試內容 ")],
            SimpleNamespace(),
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            audio_path = Path(temp_dir) / "sample.mp3"
            audio_path.write_bytes(b"audio")
            with (
                patch("faster_whisper.WhisperModel", return_value=whisper_model),
                patch("faster_whisper.utils.download_model") as download_model,
                patch(
                    "transcript_bot.whisper_local.settings",
                    SimpleNamespace(
                        whisper_device="cpu",
                        whisper_compute_type="int8",
                        whisper_initial_prompt="KKBOX,忠孝復興",
                        deepgram_keyterms="",
                        whisper_model="large-v3",
                        whisper_language="zh",
                        whisper_model_dir=Path(temp_dir) / "models",
                    ),
                ),
            ):
                segments = transcribe_with_local_whisper(audio_path)

        self.assertEqual([(item.speaker, item.start, item.end, item.text) for item in segments], [("UNASSIGNED", 1.25, 3.5, "測試內容")])
        kwargs = whisper_model.transcribe.call_args.kwargs
        self.assertEqual(kwargs["language"], "zh")
        self.assertEqual(kwargs["task"], "transcribe")
        self.assertEqual(kwargs["initial_prompt"], "KKBOX,忠孝復興")
        self.assertTrue(kwargs["vad_filter"])
        self.assertEqual(download_model.call_args.kwargs["output_dir"], str(Path(temp_dir) / "models" / "large-v3"))


if __name__ == "__main__":
    unittest.main()
