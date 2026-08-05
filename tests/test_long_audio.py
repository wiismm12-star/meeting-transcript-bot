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


if __name__ == "__main__":
    unittest.main()
