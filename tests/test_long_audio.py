from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from transcript_bot.audio import AudioChunk, split_audio_at_silence
from transcript_bot.config import settings
from transcript_bot.transcription import (
    TranscriptSegment,
    _shift_and_clamp,
    transcribe_audio_smart,
)


class SplitAudioTests(unittest.TestCase):
    def test_short_audio_is_single_chunk_reusing_normalized_file(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            normalized = Path(temp_dir) / "normalized.mp3"
            normalized.write_bytes(b"audio")
            with patch("transcript_bot.audio.get_audio_duration", return_value=120.0):
                chunks = split_audio_at_silence(normalized, Path(temp_dir) / "chunks")
        self.assertEqual(len(chunks), 1)
        self.assertEqual(chunks[0].audio_path, normalized)
        self.assertEqual(chunks[0].start, 0.0)
        self.assertEqual(chunks[0].end, 120.0)

    def test_long_audio_split_at_silence_with_overlap_and_offset(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            normalized = Path(temp_dir) / "normalized.mp3"
            normalized.write_bytes(b"audio")
            chunks_dir = Path(temp_dir) / "chunks"
            # duration 1900s, chunk_max_seconds 600 -> ~3-4 chunks; one silence near 600s.
            duration = 1900.0
            with (
                patch("transcript_bot.audio.get_audio_duration", return_value=duration),
                patch(
                    "transcript_bot.audio._detect_silences",
                    return_value=[(598.0, 602.0), (1198.0, 1202.0), (1798.0, 1802.0)],
                ),
                patch("transcript_bot.audio._extract_span") as extract_span,
            ):
                chunks = split_audio_at_silence(normalized, chunks_dir)

        # 1900s with 600s cap -> ~4 chunks
        self.assertGreater(len(chunks), 1)
        self.assertEqual(extract_span.call_count, len(chunks))

        # First chunk owns [0, ~600], last owns up to the duration.
        self.assertEqual(chunks[0].start, 0.0)
        self.assertEqual(chunks[-1].end, duration)
        # Interior chunks do NOT overlap into the next chunk's owned span.
        for a, b in zip(chunks, chunks[1:]):
            self.assertAlmostEqual(a.end, b.start, places=3)

    def test_chunk_boundaries_without_silence_fall_back_to_fixed_step(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            normalized = Path(temp_dir) / "normalized.mp3"
            normalized.write_bytes(b"audio")
            with (
                patch("transcript_bot.audio.get_audio_duration", return_value=1300.0),
                patch("transcript_bot.audio._detect_silences", return_value=[]),
                patch("transcript_bot.audio._extract_span"),
            ):
                chunks = split_audio_at_silence(normalized, Path(temp_dir) / "chunks")
        # No silence detected -> fixed ~600s steps, ~3 chunks, contiguous.
        self.assertGreater(len(chunks), 1)
        for a, b in zip(chunks, chunks[1:]):
            self.assertAlmostEqual(a.end, b.start, places=3)
        self.assertEqual(chunks[-1].end, 1300.0)


class ShiftAndClampTests(unittest.TestCase):
    def test_local_times_offset_to_global_timeline_and_overlap_dropped(self) -> None:
        chunk = AudioChunk(
            index=1,
            start=600.0,
            end=1200.0,
            audio_start=598.5,  # lead overlap of 1.5s
            audio_end=1201.5,   # tail overlap of 1.5s
            audio_path=Path("/tmp/chunk_001.mp3"),
        )
        segments = [
            # Inside lead overlap (before chunk.start) -> dropped.
            TranscriptSegment("UNASSIGNED", 0.0, 1.0, "重疊前內容"),
            # Owned span -> kept and offset.
            TranscriptSegment("UNASSIGNED", 1.5, 3.0, "會議中段內容"),
            # Inside tail overlap (past chunk.end) -> dropped.
            TranscriptSegment("UNASSIGNED", 602.0, 602.5, "重疊後內容"),
        ]
        owned = _shift_and_clamp(chunk, segments)
        self.assertEqual(len(owned), 1)
        seg = owned[0]
        self.assertEqual(seg.text, "會議中段內容")
        self.assertAlmostEqual(seg.start, 600.0)
        self.assertAlmostEqual(seg.end, 601.5)
        # UNASSIGNED stays UNASSIGNED (no chunk prefix for undiarized audio).
        self.assertEqual(seg.speaker, "UNASSIGNED")

    def test_untimed_segment_kept_at_chunk_start(self) -> None:
        chunk = AudioChunk(0, 0.0, 600.0, 0.0, 600.0, Path("/tmp/c.mp3"))
        owned = _shift_and_clamp(chunk, [TranscriptSegment("Speaker 1", None, None, "整段無時間")])
        self.assertEqual(len(owned), 1)
        self.assertEqual(owned[0].start, 0.0)
        self.assertEqual(owned[0].end, 600.0)
        # Real speaker labels (not UNASSIGNED) get a chunk prefix to avoid
        # cross-chunk collisions when pyannote/diarization runs per chunk.
        self.assertEqual(owned[0].speaker, "c0_Speaker 1")


class SmartTranscribeMergeTests(unittest.TestCase):
    def test_parallel_chunks_merge_into_one_sorted_timeline(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            normalized = Path(temp_dir) / "normalized.mp3"
            normalized.write_bytes(b"audio")
            chunks_dir = Path(temp_dir) / "chunks"
            duration = 1300.0
            with (
                patch("transcript_bot.audio.get_audio_duration", return_value=duration),
                patch("transcript_bot.audio._detect_silences", return_value=[]),
                patch("transcript_bot.audio._extract_span"),
                patch("transcript_bot.whisper_local.load_whisper_model", return_value=MagicMock()),
                patch(
                    "transcript_bot.whisper_local.transcribe_chunk_with_model",
                    side_effect=lambda model, audio_path, progress_callback=None: [
                        # local 2.0-5.0 sits inside every chunk's owned span
                        # (interior chunks add ~1.5s lead overlap, so global 600.5-603.5).
                        TranscriptSegment("UNASSIGNED", 2.0, 5.0, f"內容@{audio_path}"),
                    ],
                ),
            ):
                segments = transcribe_audio_smart(normalized)

        # All chunks merged into one list, sorted by start time, no duplicates.
        self.assertGreater(len(segments), 1)
        starts = [s.start for s in segments]
        self.assertEqual(starts, sorted(starts))
        # Every segment sits within [0, duration].
        for seg in segments:
            self.assertTrue(seg.start is not None and 0.0 <= seg.start <= duration)


class ChunkedDiarizationTests(unittest.TestCase):
    """The chunked path must diarize ONCE over the whole recording.

    Regression guard: chunked transcription used to skip pyannote entirely, so a
    long multi-speaker meeting came back as a single ``Speaker 1``.
    """

    def _run(self, diarize_mock, enable=True):
        with tempfile.TemporaryDirectory() as temp_dir:
            normalized = Path(temp_dir) / "normalized.mp3"
            normalized.write_bytes(b"audio")
            with (
                patch("transcript_bot.audio.get_audio_duration", return_value=1300.0),
                patch("transcript_bot.audio._detect_silences", return_value=[]),
                patch("transcript_bot.audio._extract_span"),
                patch("transcript_bot.whisper_local.load_whisper_model", return_value=MagicMock()),
                patch(
                    "transcript_bot.whisper_local.transcribe_chunk_with_model",
                    side_effect=lambda model, audio_path, progress_callback=None: [
                        TranscriptSegment("UNASSIGNED", 2.0, 5.0, "內容"),
                    ],
                ),
                patch("transcript_bot.config.settings.enable_pyannote_diarization", enable),
                patch("transcript_bot.config.settings.transcribe_provider", "whisper"),
                patch(
                    "transcript_bot.pyannote_diarization.diarize_with_pyannote",
                    diarize_mock,
                ),
            ):
                return transcribe_audio_smart(normalized), normalized

    def test_diarization_runs_once_on_the_full_recording(self) -> None:
        from transcript_bot.pyannote_diarization import SpeakerTurn

        turns = [
            SpeakerTurn(start=0.0, end=650.0, speaker="SPEAKER_00"),
            SpeakerTurn(start=650.0, end=1300.0, speaker="SPEAKER_01"),
        ]
        diarize = MagicMock(return_value=turns)
        segments, normalized = self._run(diarize)

        # Called exactly once, and on the WHOLE file — never per chunk.
        diarize.assert_called_once()
        self.assertEqual(diarize.call_args[0][0], normalized)
        # Speakers actually landed on the segments.
        speakers = {s.speaker for s in segments}
        self.assertNotEqual(speakers, {"UNASSIGNED"})
        self.assertTrue(speakers & {"SPEAKER_00", "SPEAKER_01"})

    def test_diarization_failure_keeps_the_transcription(self) -> None:
        from transcript_bot.pyannote_diarization import PyannoteDiarizationError

        diarize = MagicMock(side_effect=PyannoteDiarizationError("boom"))
        segments, _ = self._run(diarize)
        # Expensive transcription is preserved, just without speaker labels.
        self.assertGreater(len(segments), 1)
        self.assertEqual({s.speaker for s in segments}, {"UNASSIGNED"})

    def test_diarization_skipped_when_disabled(self) -> None:
        diarize = MagicMock()
        segments, _ = self._run(diarize, enable=False)
        diarize.assert_not_called()
        self.assertGreater(len(segments), 1)


if __name__ == "__main__":
    unittest.main()
