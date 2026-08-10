from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch
import unittest

from transcript_bot.audio import _CREATE_NO_WINDOW, normalize_audio


class AudioNormalizationTests(unittest.TestCase):
    def test_preserves_the_input_channel_count(self) -> None:
        with TemporaryDirectory() as temp_dir:
            input_path = Path(temp_dir) / "input.m4a"
            output_path = Path(temp_dir) / "normalized.mp3"
            input_path.write_bytes(b"test audio")

            with (
                patch("transcript_bot.audio.shutil.which", return_value="ffmpeg"),
                patch("transcript_bot.audio.subprocess.run", return_value=SimpleNamespace(returncode=0)) as run,
            ):
                normalize_audio(input_path, output_path)

        command = run.call_args.args[0]
        self.assertNotIn("-ac", command)
        self.assertIn("-ar", command)
        self.assertEqual(run.call_args.kwargs["creationflags"], _CREATE_NO_WINDOW)


if __name__ == "__main__":
    unittest.main()
