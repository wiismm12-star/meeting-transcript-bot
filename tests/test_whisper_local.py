from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from transcript_bot.whisper_local import _MODEL_CACHE, load_whisper_model, transcribe_with_local_whisper


class LocalWhisperTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        try:
            import faster_whisper  # noqa: F401
        except ImportError:
            raise unittest.SkipTest("faster-whisper not installed")

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
                    "transcript_bot.audio.decode_audio_to_pcm",
                    return_value=(object(), 16000),
                ),
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

    def test_reuses_the_same_loaded_model_for_matching_gpu_settings(self) -> None:
        whisper_model = MagicMock()
        with tempfile.TemporaryDirectory() as temp_dir:
            model_dir = Path(temp_dir) / "models"
            (model_dir / "large-v3").mkdir(parents=True)
            (model_dir / "large-v3" / "model.bin").write_bytes(b"model")
            test_settings = SimpleNamespace(
                whisper_device="cuda",
                whisper_compute_type="float16",
                whisper_initial_prompt="",
                whisper_model="large-v3",
                whisper_model_dir=model_dir,
            )
            _MODEL_CACHE.clear()
            with (
                patch("faster_whisper.WhisperModel", return_value=whisper_model) as model_class,
                patch("transcript_bot.whisper_local.settings", test_settings),
            ):
                first = load_whisper_model()
                second = load_whisper_model()
            _MODEL_CACHE.clear()

        self.assertIs(first, second)
        model_class.assert_called_once()


if __name__ == "__main__":
    unittest.main()
